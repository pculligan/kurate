#!/usr/bin/env python3
"""Export Confluence content into Markdown using a project YAML file."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from shared.project_config import PHASE_EXTRACTION, PROJECT_ACTIVITY_EXPORT, load_phase_config
from phases.extraction.providers.confluence.auth import missing_dependency_message
from phases.extraction.providers.confluence.client import ConfluenceClient
from phases.extraction.providers.confluence.conf_io import export_from_confluence
from phases.extraction.providers.confluence.constants import MAP_FILENAME
from phases.extraction.providers.confluence.deps import BeautifulSoup, MISSING_DEPENDENCY_ERROR, NavigableString, Tag, requests

LOG = logging.getLogger("confluence_export")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Confluence-to-repo project file")
    parser.add_argument("--project", required=True, help="Path to an export project YAML file")
    parser.add_argument("--base-url", default=None, help="Optional override for the Confluence base URL")
    parser.add_argument("--email", default=None, help="Optional override for the Confluence auth email")
    parser.add_argument("--identity-config", default=None, help="Path to a YAML identity config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    return parser.parse_args(argv)


def write_page_map(output_dir: Path, root_page_id: str, pages_by_id: Dict[str, dict]) -> Path:
    map_path = output_dir / MAP_FILENAME
    payload = {
        "root_page_id": str(root_page_id),
        "pages": {
            page["markdown_path"].relative_to(output_dir).as_posix(): str(page_id)
            for page_id, page in sorted(pages_by_id.items(), key=lambda item: item[1]["markdown_path"].as_posix())
            if not page.get("export_suppressed")
        },
    }
    map_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return map_path


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"[-\s]+", "-", value).strip("-")
    return value or "page"


def read_attachment_filename(attachment: dict) -> str:
    title = attachment.get("title") or attachment.get("metadata", {}).get("mediaType") or "attachment"
    return Path(title).name


def page_markdown_path(page_info: dict) -> Path:
    return page_info["markdown_path"]


def page_assets_dir(page_info: dict) -> Path:
    markdown_path = page_markdown_path(page_info)
    return markdown_path.parent


def reserved_page_filenames(page_info: dict) -> set[str]:
    names = {
        page_markdown_path(page_info).name,
        page_metadata_path(page_info).name,
    }
    if page_markdown_path(page_info).parent == Path(page_info["report"]["output"]):
        names.add(export_metadata_manifest_path(Path(page_info["report"]["output"])).name)
        names.add(MAP_FILENAME)
    return names


def attachment_local_filename(page_info: dict, filename: str) -> str:
    attachment = attachment_lookup(page_info).get(filename, {})
    cached = attachment.get("local_filename")
    if cached:
        return str(cached)
    cached_attachment = page_info.get("cached_attachments_by_filename", {}).get(filename, {})
    cached_local = cached_attachment.get("local_filename")
    if cached_local:
        attachment["local_filename"] = str(cached_local)
        return str(cached_local)
    candidate = Path(filename).name or "attachment"
    if candidate in reserved_page_filenames(page_info):
        candidate = f"attachment-{candidate}"
    attachment["local_filename"] = candidate
    return candidate


def page_metadata_path(page_info: dict) -> Path:
    markdown_path = page_markdown_path(page_info)
    return markdown_path.with_name(f"{markdown_path.stem}.metadata.json")


def export_metadata_manifest_path(output_dir: Path) -> Path:
    return output_dir / "export.metadata.json"


def shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # Handle February 29 by falling back to February 28 in non-leap years.
        return value.replace(month=2, day=28, year=value.year + years)


def analytics_windows(today: Optional[date] = None) -> Dict[str, date]:
    today = today or date.today()
    return {
        "year_to_date": date(today.year, 1, 1),
        "trailing_year": shift_years(today, -1),
        "all_time_proxy": shift_years(today, -10),
    }


def analytics_are_current(cached_analytics: dict, today: Optional[date] = None) -> bool:
    if not isinstance(cached_analytics, dict) or not cached_analytics:
        return False
    windows = analytics_windows(today)
    for label, from_date in windows.items():
        entry = cached_analytics.get(label)
        if not isinstance(entry, dict):
            return False
        if str(entry.get("from_date")) != from_date.isoformat():
            return False
    return True


def file_modified_on(path: Path, day: date) -> bool:
    try:
        return date.fromtimestamp(path.stat().st_mtime) == day
    except OSError:
        return False


def _load_json_mapping(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def load_existing_metadata_index(output_dir: Path) -> Dict[str, dict]:
    manifest_index: Dict[str, dict] = {}
    manifest_path = export_metadata_manifest_path(output_dir)
    if manifest_path.exists():
        manifest = _load_json_mapping(manifest_path)
        if manifest:
            for entry in manifest.get("pages", []):
                if isinstance(entry, dict) and entry.get("confluence_page_id") is not None:
                    manifest_index[str(entry["confluence_page_id"])] = entry
    return manifest_index


def load_existing_page_map(output_dir: Path) -> Dict[str, Path]:
    path_index: Dict[str, Path] = {}
    map_path = output_dir / MAP_FILENAME
    page_map = _load_json_mapping(map_path)
    if not page_map:
        return path_index
    pages = page_map.get("pages", {})
    if not isinstance(pages, dict):
        return path_index
    for relative_path, page_id in pages.items():
        relative = str(relative_path).strip()
        page_key = str(page_id).strip()
        if not relative or not page_key:
            continue
        candidate = (output_dir / relative).resolve()
        try:
            candidate.relative_to(output_dir.resolve())
        except ValueError:
            continue
        path_index[page_key] = candidate
    return path_index


def ensure_unique_path(path: Path, used_paths: set[Path]) -> Path:
    if path not in used_paths and not path.exists():
        used_paths.add(path)
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if candidate not in used_paths and not candidate.exists():
            used_paths.add(candidate)
            return candidate
        counter += 1


def assign_paths(
    pages_by_id: Dict[str, dict],
    children_by_id: Dict[str, List[str]],
    root_id: str,
    output_dir: Path,
) -> None:
    existing_page_map = load_existing_page_map(output_dir)
    used_paths: set[Path] = {output_dir, output_dir / "readme.md"}
    root = pages_by_id[root_id]
    root["folder_path"] = output_dir
    root["markdown_path"] = output_dir / "readme.md"
    root["slug"] = output_dir.name or slugify(root["title"])

    def walk(parent_id: str) -> None:
        parent = pages_by_id[parent_id]
        parent_folder = parent["folder_path"]
        for child_id in children_by_id.get(parent_id, []):
            child = pages_by_id[child_id]
            slug = slugify(child["title"])
            child["slug"] = slug
            has_children = bool(children_by_id.get(child_id))
            has_attachments = bool(child.get("attachments"))
            existing_markdown_path = existing_page_map.get(str(child_id))
            if existing_markdown_path:
                if has_children or has_attachments or existing_markdown_path.name == "readme.md":
                    folder_path = (
                        existing_markdown_path.parent
                        if existing_markdown_path.name == "readme.md"
                        else existing_markdown_path.with_suffix("")
                    )
                    markdown_path = folder_path / "readme.md"
                    if folder_path not in used_paths and markdown_path not in used_paths:
                        child["folder_path"] = folder_path
                        child["markdown_path"] = markdown_path
                        used_paths.add(folder_path)
                        used_paths.add(markdown_path)
                        walk(child_id)
                        continue
                else:
                    markdown_path = existing_markdown_path
                    if markdown_path not in used_paths:
                        child["folder_path"] = markdown_path.parent
                        child["markdown_path"] = markdown_path
                        used_paths.add(markdown_path)
                        walk(child_id)
                        continue
            if has_children or has_attachments:
                folder_path = ensure_unique_path(parent_folder / slug, used_paths)
                child["folder_path"] = folder_path
                child["markdown_path"] = folder_path / "readme.md"
                used_paths.add(child["markdown_path"])
            else:
                child["folder_path"] = parent_folder
                child["markdown_path"] = ensure_unique_path(parent_folder / f"{slug}.md", used_paths)
            walk(child_id)

    walk(root_id)

def markdown_escape_text(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", text)


def squash_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def element_children(node: Tag) -> List[Tag]:
    return [child for child in node.children if isinstance(child, Tag)]


def macro_name(node: Tag) -> str:
    return str(node.get("ac:name", "macro")).strip() or "macro"


def macro_parameter(node: Tag, name: str) -> str:
    parameter = node.find("ac:parameter", attrs={"ac:name": name})
    return parameter.get_text(" ", strip=True) if parameter else ""


def render_rich_text_children(node: Optional[Tag], context: dict) -> str:
    if node is None:
        return ""
    return "".join(render_block(child, context) for child in node.children).strip()


def render_inline_children(node: Optional[Tag], context: dict) -> str:
    if node is None:
        return ""
    return "".join(inline_text(child, context) for child in node.children).strip()


def render_placeholder(node: Tag) -> str:
    text = node.get_text(" ", strip=True)
    if not text:
        text = str(node.get("ac:placeholder", "")).strip()
    if not text:
        return ""
    return f"_Placeholder: {text}_"


def add_unresolved_link(
    context: dict,
    *,
    kind: str,
    category: str,
    reason: str,
    source_text: Optional[str] = None,
    target_title: Optional[str] = None,
    target_id: Optional[str] = None,
    target_space: Optional[str] = None,
    attachment_filename: Optional[str] = None,
    fallback_url: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    page_info = context["page"]
    entry = {
        "page": page_info["title"],
        "page_id": page_info["id"],
        "kind": kind,
        "category": category,
        "reason": reason,
    }
    if source_text:
        entry["source_text"] = source_text
    if target_title:
        entry["target_title"] = target_title
    if target_id:
        entry["target_id"] = str(target_id)
    if target_space:
        entry["target_space"] = target_space
    if attachment_filename:
        entry["attachment_filename"] = attachment_filename
    if fallback_url:
        entry["fallback_url"] = fallback_url
    if detail:
        entry["detail"] = detail
    context["report"]["unresolved_links"].append(entry)


def rewrite_plain_href(href: str, context: dict) -> str:
    parsed = urlparse(href)
    page_id = None
    if parsed.query:
        page_id = parse_qs(parsed.query).get("pageId", [None])[0]
    if page_id and page_id in context["pages_by_id"]:
        return relative_markdown_link(context["page"], context["pages_by_id"][page_id])
    return href


def inline_text(node, context: dict) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in {"strong", "b"}:
        return f"**{''.join(inline_text(child, context) for child in node.children).strip()}**"
    if name in {"em", "i"}:
        return f"*{''.join(inline_text(child, context) for child in node.children).strip()}*"
    if name == "code":
        return f"`{node.get_text(strip=True)}`"
    if name == "br":
        return "  \n"
    if name == "a":
        href = node.get("href", "").strip()
        text = "".join(inline_text(child, context) for child in node.children).strip() or href
        if href:
            href = rewrite_plain_href(href, context)
            return f"[{text}]({href})"
        return text
    if name == "ac:link":
        return render_confluence_link(node, context)
    if name == "ac:image":
        return render_confluence_image(node, context)
    if name == "ac:emoticon":
        return render_emoticon(node)
    if name == "ac:inline-comment-marker":
        return "".join(inline_text(child, context) for child in node.children)
    if name == "ac:placeholder":
        return render_placeholder(node)
    if name == "ac:task-list":
        return render_task_list(node, context).strip()
    if name == "ac:structured-macro":
        return render_macro(node, context).strip()
    if name.startswith("ac:") or name.startswith("ri:"):
        return render_unsupported_inline(node, context)
    return "".join(inline_text(child, context) for child in node.children)


def render_list(tag: Tag, context: dict, level: int = 0) -> str:
    lines: List[str] = []
    ordered = tag.name.lower() == "ol"
    for index, item in enumerate([child for child in tag.children if isinstance(child, Tag) and child.name.lower() == "li"], start=1):
        marker = f"{index}." if ordered else "-"
        prefix = "  " * level + marker + " "
        parts: List[str] = []
        nested: List[str] = []
        for child in item.children:
            if isinstance(child, Tag) and child.name.lower() in {"ul", "ol"}:
                nested.append(render_list(child, context, level + 1).rstrip())
            else:
                parts.append(inline_text(child, context))
        lines.append(prefix + "".join(parts).strip())
        lines.extend(line for line in nested if line)
    return "\n".join(lines) + "\n\n"


def render_table(tag: Tag, context: dict) -> str:
    rows = []
    for row in tag.find_all("tr"):
        cells = [inline_text(cell, context).strip().replace("\n", " ") for cell in row.find_all(["th", "td"], recursive=False)]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    body = rows[1:] or [[]]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n\n"


def render_block(node, context: dict) -> str:
    if isinstance(node, NavigableString):
        text = str(node)
        return text if text.strip() else ""
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name == "p":
        content = "".join(inline_text(child, context) for child in node.children).strip()
        return content + "\n\n" if content else ""
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        content = "".join(inline_text(child, context) for child in node.children).strip()
        return ("#" * level) + f" {content}\n\n" if content else ""
    if name in {"ul", "ol"}:
        return render_list(node, context)
    if name == "blockquote":
        content = "".join(render_block(child, context) for child in node.children).strip()
        return "\n".join(f"> {line}" if line else ">" for line in content.splitlines()) + "\n\n"
    if name == "pre":
        code = node.get_text()
        language = ""
        code_tag = node.find("code")
        if code_tag:
            classes = code_tag.get("class", [])
            for class_name in classes:
                if class_name.startswith("language-"):
                    language = class_name.split("-", 1)[1]
                    break
            code = code_tag.get_text()
        return f"```{language}\n{code.rstrip()}\n```\n\n"
    if name == "table":
        return render_table(node, context)
    if name == "hr":
        return "---\n\n"
    if name == "ac:image":
        return render_confluence_image(node, context) + "\n\n"
    if name == "ac:emoticon":
        emoticon = render_emoticon(node)
        return emoticon + "\n\n" if emoticon else ""
    if name == "ac:inline-comment-marker":
        return "".join(render_block(child, context) for child in node.children)
    if name == "ac:placeholder":
        placeholder = render_placeholder(node)
        return placeholder + "\n\n" if placeholder else ""
    if name == "ac:structured-macro":
        return render_macro(node, context)
    if name in {"ac:layout", "ac:layout-section", "ac:layout-cell"}:
        return "".join(render_block(child, context) for child in node.children)
    if name == "ac:task-list":
        return render_task_list(node, context)
    if name in {"div", "span", "section", "article", "tbody", "thead", "tr", "td", "th"}:
        return "".join(render_block(child, context) for child in node.children)
    if name == "ac:link":
        return render_confluence_link(node, context) + "\n\n"
    if name.startswith("ac:") or name.startswith("ri:"):
        return render_unsupported_block(node, context)
    return "".join(render_block(child, context) for child in node.children)


def _content_snippet(node: Tag, limit: int = 180) -> str:
    raw = str(node)
    squashed = re.sub(r"\s+", " ", raw).strip()
    if len(squashed) <= limit:
        return squashed
    return squashed[: limit - 3] + "..."


def register_unsupported_content(node: Tag, context: dict, kind: str, content_type: str, detail: str) -> str:
    report = context["report"]
    page_info = context["page"]
    next_index = int(report.get("unsupported_content_counter", 0)) + 1
    report["unsupported_content_counter"] = next_index
    marker_id = f"embed-{next_index}"
    report.setdefault("unsupported_content", []).append(
        {
            "id": marker_id,
            "page": page_info["title"],
            "page_id": page_info["id"],
            "kind": kind,
            "content_type": content_type,
            "detail": detail,
            "snippet": _content_snippet(node),
        }
    )
    return marker_id


def render_unsupported_block(node: Tag, context: dict, kind: str = "embed", content_type: Optional[str] = None, detail: Optional[str] = None) -> str:
    content_type = content_type or node.name.lower()
    detail = detail or f"Unsupported Confluence content `{content_type}`"
    marker_id = register_unsupported_content(node, context, kind, content_type, detail)
    return (
        f"> Unsupported Confluence content `{content_type}` here.\n"
        f">\n"
        f"> See export report item `{marker_id}`.\n\n"
    )


def render_unsupported_inline(node: Tag, context: dict, kind: str = "embed", content_type: Optional[str] = None, detail: Optional[str] = None) -> str:
    content_type = content_type or node.name.lower()
    detail = detail or f"Unsupported Confluence content `{content_type}`"
    marker_id = register_unsupported_content(node, context, kind, content_type, detail)
    return f"`[unsupported {content_type}; see {marker_id}]`"


def render_status_macro(node: Tag) -> str:
    title = macro_parameter(node, "title") or macro_parameter(node, "colour") or "Unknown"
    return f"`Status: {title}`"


def render_labeled_block(label: str, content: str) -> str:
    content = content.strip()
    if not content:
        return f"**{label}:**\n\n"
    if "\n" not in content:
        return f"**{label}:** {content}\n\n"
    return f"**{label}:**\n\n{content}\n\n"


def render_plain_text_macro_body(node: Tag) -> str:
    body = node.find("ac:plain-text-body")
    if body is None:
        return ""
    text = body.get_text()
    if not text.strip():
        return ""
    stripped = text.lstrip()
    language = "json" if stripped.startswith("{") or stripped.startswith("[") else "text"
    return f"```{language}\n{text.rstrip()}\n```"


def render_rich_or_plain_macro_body(node: Tag, context: dict) -> str:
    rich_body = node.find("ac:rich-text-body")
    if rich_body is not None:
        content = render_rich_text_children(rich_body, context).strip()
        if content:
            return content
    return render_plain_text_macro_body(node)


def render_callout_macro(node: Tag, context: dict, label: str) -> str:
    content = render_rich_or_plain_macro_body(node, context)
    return render_labeled_block(label, content) if content else ""


def render_children_macro(node: Tag, context: dict) -> str:
    page_info = context["page"]
    style = macro_parameter(node, "style")
    sort = macro_parameter(node, "sort")
    qualifiers: List[str] = []
    if style:
        qualifiers.append(f"style `{style}`")
    if sort:
        qualifiers.append(f"sort `{sort}`")
    detail = ", ".join(qualifiers)
    suffix = f" ({detail})" if detail else ""
    return f"> Child page listing omitted in export{suffix}.\n\n"


def render_toc_macro(node: Tag) -> str:
    return ""


def render_info_macro(node: Tag, context: dict) -> str:
    return render_callout_macro(node, context, "Info")


def render_tip_macro(node: Tag, context: dict) -> str:
    return render_callout_macro(node, context, "Tip")


def render_warning_macro(node: Tag, context: dict) -> str:
    return render_callout_macro(node, context, "Warning")


def render_excerpt_macro(node: Tag, context: dict) -> str:
    return render_callout_macro(node, context, "Excerpt")


def render_details_macro(node: Tag, context: dict) -> str:
    label = macro_parameter(node, "label") or "Details"
    rich_body = node.find("ac:rich-text-body")
    content = render_rich_text_children(rich_body, context)
    if not content:
        return f"### {label}\n\n"
    return f"### {label}\n\n{content}\n\n"


def render_task_list(node: Tag, context: dict) -> str:
    lines: List[str] = []
    for task in node.find_all("ac:task", recursive=False):
        status = task.find("ac:task-status")
        checked = (status.get_text(strip=True).lower() == "complete") if status else False
        body = task.find("ac:task-body")
        body_text = (render_inline_children(body, context) or body.get_text(" ", strip=True)) if body else ""
        body_text = body_text.strip()
        if body_text:
            lines.append(f"- [{'x' if checked else ' '}] {body_text}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def render_view_file_macro(node: Tag, context: dict) -> str:
    attachment = node.find("ri:attachment")
    page_info = context["page"]
    client = context["client"]
    if attachment:
        filename = attachment.get("ri:filename")
        if filename:
            asset_path = download_attachment_for_page(client, page_info, filename)
            if asset_path:
                href = relative_asset_link(page_info, asset_path)
                return f"[{filename}]({href})"
    return render_unsupported_block(
        node,
        context,
        kind="macro",
        content_type="view-file",
        detail="Unsupported Confluence macro `view-file` without a usable attachment reference",
    )


def render_jira_macro(node: Tag, context: dict) -> str:
    key = macro_parameter(node, "key")
    server = macro_parameter(node, "server")
    base_url = str(context.get("base_url", "")).rstrip("/")
    if key and base_url:
        href = f"{base_url}/browse/{key}"
        label = key if not server else f"{key} ({server})"
        return f"[{label}]({href})"
    return render_unsupported_block(
        node,
        context,
        kind="macro",
        content_type="jira",
        detail="Unsupported Confluence macro `jira` without a usable issue key",
    )


def render_lucidchart_macro(node: Tag, context: dict) -> str:
    title = macro_parameter(node, "title") or macro_parameter(node, "name") or "Lucidchart diagram"
    url = (
        macro_parameter(node, "url")
        or macro_parameter(node, "link")
        or macro_parameter(node, "src")
        or macro_parameter(node, "documentUrl")
    )
    lc_id = macro_parameter(node, "lcId") or macro_parameter(node, "id")
    if url:
        return f"[{title}]({url})"
    if lc_id:
        view_url = f"https://lucid.app/lucidchart/{lc_id}/view"
        edit_url = f"https://lucid.app/lucidchart/{lc_id}/edit"
        return f"{title}: [view]({view_url}) ([edit]({edit_url}))"
    detail = "Unsupported Confluence macro `lucidchart`"
    if lc_id:
        detail += f" with lcId `{lc_id}`"
    return render_unsupported_block(
        node,
        context,
        kind="macro",
        content_type="lucidchart",
        detail=detail,
    )


def render_gliffy_macro(node: Tag, context: dict) -> str:
    title = macro_parameter(node, "name") or macro_parameter(node, "title") or "Gliffy diagram"
    url = (
        macro_parameter(node, "url")
        or macro_parameter(node, "link")
        or macro_parameter(node, "viewerUrl")
        or macro_parameter(node, "editUrl")
    )
    if url:
        return f"[{title}]({url})"
    macro_id = macro_parameter(node, "macroId") or macro_parameter(node, "id")
    detail = "Unsupported Confluence macro `gliffy`"
    if macro_id:
        detail += f" with macroId `{macro_id}`"
    return render_unsupported_block(
        node,
        context,
        kind="macro",
        content_type="gliffy",
        detail=detail,
    )


def _page_stub_from_api_payload(page: dict) -> dict:
    return {
        "id": page["id"],
        "title": page.get("title", f"Page {page['id']}"),
        "body_storage": page.get("body", {}).get("storage", {}).get("value", ""),
        "space_key": page.get("space", {}).get("key"),
        "attachments": [],
        "cached_attachments_by_filename": {},
    }


def _load_page_attachments(client: ConfluenceClient, page_info: dict) -> None:
    if page_info.get("attachments"):
        return
    attachments = []
    for attachment in client.get_attachments(str(page_info["id"])):
        filename = read_attachment_filename(attachment)
        download = attachment.get("_links", {}).get("download")
        attachments.append(
            {
                "filename": filename,
                "download": download,
                "id": attachment.get("id"),
                "version": attachment.get("version", {}).get("number"),
                "media_type": attachment.get("metadata", {}).get("mediaType"),
                "local_filename": filename,
            }
        )
    page_info["attachments"] = attachments


def resolve_excerpt_include_page(node: Tag, context: dict) -> tuple[Optional[str], Optional[dict]]:
    page_ref = node.find("ri:page")
    if page_ref is None:
        return None, None

    target_id = page_ref.get("ri:content-id")
    target_title = page_ref.get("ri:content-title")
    target_space = page_ref.get("ri:space-key") or page_ref.get("ri:spacekey") or context["page"].get("space_key")
    if not target_id:
        if target_title:
            target_id = context["title_to_id"].get(target_title)

    if target_id and target_id in context["pages_by_id"]:
        return str(target_id), context["pages_by_id"][target_id]

    cache = context["report"].setdefault("_excerpt_include_page_cache", {})
    if target_id:
        cached = cache.get(str(target_id))
        if cached is not None:
            return str(target_id), cached
        try:
            page = context["client"].get_page(str(target_id), expand="body.storage,version,ancestors")
        except requests.HTTPError:
            cache[str(target_id)] = None
            return str(target_id), None
        cached_page = _page_stub_from_api_payload(page)
        _load_page_attachments(context["client"], cached_page)
        cache[str(target_id)] = cached_page
        return str(target_id), cached_page

    if target_title and target_space:
        cache_key = f"{target_space}:{target_title}"
        cached = cache.get(cache_key)
        if cached is not None:
            return (str(cached["id"]) if cached else None), cached
        try:
            page = context["client"].find_page(target_space, target_title)
        except requests.HTTPError:
            cache[cache_key] = None
            return None, None
        if not page:
            cache[cache_key] = None
            return None, None
        cached_page = _page_stub_from_api_payload(page)
        _load_page_attachments(context["client"], cached_page)
        cache[cache_key] = cached_page
        return str(cached_page["id"]), cached_page

    return None, None


def find_excerpt_body(page_info: dict, excerpt_name: str = "") -> Optional[Tag]:
    storage_html = page_info.get("body_storage", "")
    if not storage_html:
        return None
    soup = BeautifulSoup(storage_html, "html.parser")
    fallback_excerpt: Optional[Tag] = None
    for macro in soup.find_all("ac:structured-macro"):
        if macro_name(macro) != "excerpt":
            continue
        if fallback_excerpt is None:
            fallback_excerpt = macro
        if excerpt_name:
            name = macro_parameter(macro, "name")
            if name != excerpt_name:
                continue
        rich_body = macro.find("ac:rich-text-body")
        if rich_body and rich_body.get_text(" ", strip=True):
            return rich_body
        plain_body = macro.find("ac:plain-text-body")
        if plain_body and plain_body.get_text(strip=True):
            return plain_body
    if excerpt_name or fallback_excerpt is None:
        return None
    rich_body = fallback_excerpt.find("ac:rich-text-body")
    if rich_body and rich_body.get_text(" ", strip=True):
        return rich_body
    plain_body = fallback_excerpt.find("ac:plain-text-body")
    if plain_body and plain_body.get_text(strip=True):
        return plain_body
    return None


def render_excerpt_include_macro(node: Tag, context: dict) -> str:
    target_id, target_page = resolve_excerpt_include_page(node, context)
    if not target_page:
        detail = "Unsupported Confluence macro `excerpt-include` without a resolvable source page"
        if target_id:
            detail += f" (target page `{target_id}`)"
        return render_unsupported_block(
            node,
            context,
            kind="macro",
            content_type="excerpt-include",
            detail=detail,
        )

    excerpt_name = macro_parameter(node, "name")
    excerpt_stack = context.setdefault("_excerpt_include_stack", [])
    target_page_id = str(target_page.get("id", target_id or "")).strip()
    if target_page_id and target_page_id in excerpt_stack:
        return render_unsupported_block(
            node,
            context,
            kind="macro",
            content_type="excerpt-include",
            detail=f"Unsupported Confluence macro `excerpt-include` due to recursive include on page `{target_page_id}`",
        )

    excerpt_body = find_excerpt_body(target_page, excerpt_name=excerpt_name)
    if excerpt_body is None:
        detail = f"Unsupported Confluence macro `excerpt-include`: no excerpt found on source page `{target_page.get('title', target_page_id)}`"
        if excerpt_name:
            detail += f" for excerpt name `{excerpt_name}`"
        return render_unsupported_block(
            node,
            context,
            kind="macro",
            content_type="excerpt-include",
            detail=detail,
        )

    excerpt_stack.append(target_page_id)
    try:
        nested_context = dict(context)
        nested_page = dict(context["page"])
        nested_page["attachments"] = target_page.get("attachments", [])
        nested_page["cached_attachments_by_filename"] = target_page.get("cached_attachments_by_filename", {})
        nested_context["page"] = nested_page
        nested_context["_excerpt_include_stack"] = excerpt_stack
        if excerpt_body.name == "ac:plain-text-body":
            text = excerpt_body.get_text()
            if not text.strip():
                return ""
            stripped = text.lstrip()
            language = "json" if stripped.startswith("{") or stripped.startswith("[") else "text"
            return f"```{language}\n{text.rstrip()}\n```\n\n"
        content = render_rich_text_children(excerpt_body, nested_context)
        return content.rstrip() + "\n\n" if content.strip() else ""
    finally:
        excerpt_stack.pop()


def render_expand_macro(node: Tag, context: dict) -> str:
    title = macro_parameter(node, "title") or "Expanded Content"
    content = render_rich_or_plain_macro_body(node, context)
    return render_labeled_block(f"Expand: {title}", content) if content else f"**Expand: {title}**\n\n"


def render_attachments_macro(node: Tag, context: dict) -> str:
    page_info = context["page"]
    client = context["client"]
    attachments = sorted(page_info.get("attachments", []), key=lambda item: item.get("filename", "").lower())
    if not attachments:
        return ""
    lines = ["**Attachments:**", ""]
    for attachment in attachments:
        filename = attachment.get("filename")
        if not filename:
            continue
        asset_path = download_attachment_for_page(client, page_info, filename)
        if asset_path:
            lines.append(f"- [{filename}]({relative_asset_link(page_info, asset_path)})")
        else:
            lines.append(f"- {filename}")
    if len(lines) == 2:
        return ""
    return "\n".join(lines) + "\n\n"


def render_macro(node: Tag, context: dict) -> str:
    name = macro_name(node)
    if name in {"code", "noformat"}:
        body = node.find("ac:plain-text-body")
        if body:
            return f"```text\n{body.get_text()}\n```\n\n"
    if name == "status":
        return render_status_macro(node)
    if name == "children":
        return render_children_macro(node, context)
    if name == "toc":
        return render_toc_macro(node)
    if name == "info":
        return render_info_macro(node, context)
    if name == "tip":
        return render_tip_macro(node, context)
    if name == "warning":
        return render_warning_macro(node, context)
    if name == "excerpt":
        return render_excerpt_macro(node, context)
    if name == "excerpt-include":
        return render_excerpt_include_macro(node, context)
    if name == "details":
        return render_details_macro(node, context)
    if name == "expand":
        return render_expand_macro(node, context)
    if name == "attachments":
        return render_attachments_macro(node, context)
    if name in {"view-file", "viewxls"}:
        return render_view_file_macro(node, context)
    if name == "jira":
        return render_jira_macro(node, context)
    if name == "lucidchart":
        return render_lucidchart_macro(node, context)
    if name == "gliffy":
        return render_gliffy_macro(node, context)
    body = node.find("ac:plain-text-body")
    rich_body = node.find("ac:rich-text-body")
    detail = f"Unsupported Confluence macro `{name}`"
    if body and body.get_text(strip=True):
        detail += f" with plain text body `{body.get_text(strip=True)[:80]}`"
    elif rich_body and rich_body.get_text(" ", strip=True):
        detail += f" with rich text body `{rich_body.get_text(' ', strip=True)[:80]}`"
    return render_unsupported_block(node, context, kind="macro", content_type=name, detail=detail)


def relative_markdown_link(from_page: dict, to_page: dict) -> str:
    return os.path.relpath(to_page["markdown_path"], start=from_page["markdown_path"].parent).replace(os.sep, "/")


def relative_asset_link(from_page: dict, asset_path: Path) -> str:
    return os.path.relpath(asset_path, start=from_page["markdown_path"].parent).replace(os.sep, "/")


def attachment_lookup(page_info: dict) -> Dict[str, dict]:
    return {attachment["filename"]: attachment for attachment in page_info.get("attachments", [])}


def attachment_cache_key(attachment: dict) -> tuple[str | None, str | None]:
    attachment_id = attachment.get("id")
    version = attachment.get("version")
    return (
        str(attachment_id) if attachment_id is not None else None,
        str(version) if version is not None else None,
    )


def download_attachment_for_page(client: ConfluenceClient, page_info: dict, filename: str) -> Optional[Path]:
    attachment = attachment_lookup(page_info).get(filename)
    if not attachment:
        return None
    local_filename = attachment_local_filename(page_info, filename)
    destination = page_assets_dir(page_info) / local_filename
    cached_attachment = page_info.get("cached_attachments_by_filename", {}).get(filename, {})
    cached_key = attachment_cache_key(cached_attachment) if cached_attachment else (None, None)
    live_key = attachment_cache_key(attachment)
    should_download = not destination.exists()
    if destination.exists():
        if not cached_attachment:
            should_download = False
        elif live_key != cached_key:
            should_download = True
        else:
            cache_hits = page_info.setdefault("_attachment_cache_hits", set())
            if filename not in cache_hits:
                page_info["report"].setdefault("attachment_cache_hits", []).append(
                    f"{page_info['title']}: {local_filename}"
                )
                cache_hits.add(filename)
    if should_download:
        if not attachment.get("download"):
            page_info["report"]["warnings"].append(
                f"Missing attachment download URL for {page_info['title']}: {filename}"
            )
            return None
        try:
            client.download_attachment(attachment["download"], destination)
            page_info["report"]["downloaded_attachments"].append(
                {
                    "page": page_info["title"],
                    "page_path": str(page_markdown_path(page_info)),
                    "filename": local_filename,
                    "destination": str(destination),
                }
            )
        except requests.HTTPError as exc:
            page_info["report"]["warnings"].append(
                f"Failed to download attachment for {page_info['title']}: {filename} ({exc})"
            )
            return None
    return destination


def render_confluence_image(node: Tag, context: dict) -> str:
    page_info = context["page"]
    client = context["client"]
    attachment = node.find("ri:attachment")
    url_node = node.find("ri:url")
    if attachment:
        filename = attachment.get("ri:filename")
        if filename:
            asset_path = download_attachment_for_page(client, page_info, filename)
            if asset_path:
                alt = node.get("ac:alt") or filename
                return f"![{alt}]({relative_asset_link(page_info, asset_path)})"
            add_unresolved_link(
                context,
                kind="image",
                category="unresolved",
                reason="missing-attachment-image",
                source_text=node.get("ac:alt") or filename,
                attachment_filename=filename,
                detail=f"missing attachment image {filename}",
            )
    if url_node and url_node.get("ri:value"):
        url = url_node["ri:value"]
        alt = node.get("ac:alt") or "image"
        return f"![{alt}]({url})"
    return ""


def render_confluence_link(node: Tag, context: dict) -> str:
    page_info = context["page"]
    export_pages = context["pages_by_id"]
    title_to_id = context["title_to_id"]
    client = context["client"]

    body = node.find("ac:plain-text-link-body")
    if body:
        link_text = body.get_text()
    else:
        link_text = node.get_text(" ", strip=True)

    page_ref = node.find("ri:page")
    attachment_ref = node.find("ri:attachment")
    url_ref = node.find("ri:url")

    if page_ref:
        target_space = page_ref.get("ri:space-key") or page_ref.get("ri:spacekey")
        target_id = page_ref.get("ri:content-id")
        if not target_id:
            title = page_ref.get("ri:content-title")
            if title:
                target_id = title_to_id.get(title)
        if target_id and target_id in export_pages:
            target_page = export_pages[target_id]
            if target_page.get("export_suppressed"):
                href = target_page.get("page_url") or f"{context['base_url']}/wiki/pages/viewpage.action?pageId={target_id}"
                add_unresolved_link(
                    context,
                    kind="page",
                    category="out-of-scope",
                    reason="suppressed-root-page",
                    source_text=link_text or target_page.get("title") or href,
                    target_id=str(target_id),
                    target_title=target_page.get("title"),
                    fallback_url=href,
                    detail=f"{link_text or href} -> suppressed root page {href}",
                )
                return f"[{link_text or target_page.get('title') or href}]({href})"
            href = relative_markdown_link(page_info, target_page)
            return f"[{link_text or target_page['title']}]({href})"
        if target_id:
            href = f"{context['base_url']}/wiki/pages/viewpage.action?pageId={target_id}"
            add_unresolved_link(
                context,
                kind="page",
                category="cross-space" if target_space and target_space != page_info.get("space_key") else "out-of-scope",
                reason="outside-export-scope",
                source_text=link_text or href,
                target_id=str(target_id),
                target_space=target_space,
                fallback_url=href,
                detail=f"{link_text or href} -> {href}",
            )
            return f"[{link_text or href}]({href})"
        title = page_ref.get("ri:content-title")
        if title:
            add_unresolved_link(
                context,
                kind="page",
                category="cross-space" if target_space and target_space != page_info.get("space_key") else "unresolved",
                reason="unresolved-title",
                source_text=link_text or title,
                target_title=title,
                target_space=target_space,
                detail=f"{link_text or title} -> unresolved title {title}",
            )
            return f"[{link_text or title}]({title})"

    if attachment_ref:
        filename = attachment_ref.get("ri:filename")
        if filename:
            asset_path = download_attachment_for_page(client, page_info, filename)
            if asset_path:
                href = relative_asset_link(page_info, asset_path)
                return f"[{link_text or filename}]({href})"
            add_unresolved_link(
                context,
                kind="attachment",
                category="unresolved",
                reason="missing-attachment",
                source_text=link_text or filename,
                attachment_filename=filename,
                detail=f"{link_text or filename} -> missing attachment {filename}",
            )

    if url_ref and url_ref.get("ri:value"):
        url = url_ref["ri:value"]
        return f"[{link_text or url}]({url})"

    href = node.get("href")
    if href:
        return f"[{link_text or href}]({href})"
    return link_text


def render_emoticon(node: Tag) -> str:
    fallback = (
        node.get("ac:emoji-fallback")
        or node.get("ac:emoji-shortname")
        or node.get("ac:name")
        or ""
    )
    return str(fallback).strip()


def storage_to_markdown(storage_html: str, context: dict) -> str:
    soup = BeautifulSoup(storage_html, "html.parser")
    body_parts = [render_block(child, context) for child in soup.contents]
    body = squash_blank_lines("".join(body_parts))
    title = context["page"]["title"]
    return f"# {title}\n\n{body}"


def sync_attachments_from_storage(storage_html: str, context: dict) -> None:
    soup = BeautifulSoup(storage_html, "html.parser")
    seen: set[str] = set()
    for attachment in soup.find_all("ri:attachment"):
        filename = attachment.get("ri:filename")
        if not filename or filename in seen:
            continue
        seen.add(filename)
        download_attachment_for_page(context["client"], context["page"], filename)


def collect_pages(
    client: ConfluenceClient,
    root_id: str,
    recurse: bool,
    excluded_page_ids: set[str],
    default_space_key: Optional[str] = None,
) -> tuple[Dict[str, dict], Dict[str, List[str]], List[str]]:
    pages_by_id: Dict[str, dict] = {}
    children_by_id: Dict[str, List[str]] = {}
    excluded_hits: List[str] = []

    def walk(page_id: str) -> None:
        if page_id in excluded_page_ids and page_id != root_id:
            excluded_hits.append(page_id)
            return
        if page_id in pages_by_id:
            return
        page = client.get_page(page_id, expand="body.storage,version,ancestors,history,metadata.labels")
        pages_by_id[page_id] = {
            "id": page["id"],
            "title": page["title"],
            "body_storage": page.get("body", {}).get("storage", {}).get("value", ""),
            "space_key": page.get("space", {}).get("key") or default_space_key,
            "page_url": f"{client.base_url}/wiki/pages/viewpage.action?pageId={page['id']}",
            "created_at": page.get("history", {}).get("createdDate"),
            "created_by": page.get("history", {}).get("createdBy", {}).get("displayName"),
            "created_by_account_id": page.get("history", {}).get("createdBy", {}).get("accountId"),
            "updated_at": page.get("version", {}).get("when"),
            "updated_by": page.get("version", {}).get("by", {}).get("displayName"),
            "updated_by_account_id": page.get("version", {}).get("by", {}).get("accountId"),
            "version_number": page.get("version", {}).get("number"),
            "ancestor_ids": [ancestor.get("id") for ancestor in page.get("ancestors", []) if ancestor.get("id")],
            "ancestor_titles": [ancestor.get("title") for ancestor in page.get("ancestors", []) if ancestor.get("title")],
            "labels": [
                result.get("name")
                for result in page.get("metadata", {}).get("labels", {}).get("results", [])
                if result.get("name")
            ],
            "export_suppressed": page_id == root_id and page_id in excluded_page_ids,
        }
        children_by_id[page_id] = []
        if not recurse and page_id != root_id:
            return
        if recurse:
            children = client.get_children(page_id)
            for child in children:
                child_id = child["id"]
                if child_id in excluded_page_ids:
                    excluded_hits.append(child_id)
                    continue
                children_by_id[page_id].append(child_id)
                walk(child_id)

    walk(root_id)
    return pages_by_id, children_by_id, sorted(set(excluded_hits))


def enrich_attachments(client: ConfluenceClient, pages_by_id: Dict[str, dict]) -> None:
    for page_id, page_info in pages_by_id.items():
        if page_info.get("export_suppressed"):
            page_info["attachments"] = []
            continue
        attachments = []
        for attachment in client.get_attachments(page_id):
            filename = read_attachment_filename(attachment)
            download = attachment.get("_links", {}).get("download")
            attachments.append(
                {
                    "filename": filename,
                    "download": download,
                    "id": attachment.get("id"),
                    "version": attachment.get("version", {}).get("number"),
                    "media_type": attachment.get("metadata", {}).get("mediaType"),
                    "local_filename": filename,
                }
            )
        page_info["attachments"] = attachments


def enrich_analytics(client: ConfluenceClient, pages_by_id: Dict[str, dict], report: dict) -> None:
    windows = analytics_windows()
    for page_id, page_info in pages_by_id.items():
        if page_info.get("export_suppressed"):
            page_info["analytics"] = {}
            continue
        cached_analytics = page_info.get("cached_metadata_entry", {}).get("analytics", {})
        if (
            not report.get("force")
            and page_info.get("reuse_cached_metadata")
            and analytics_are_current(cached_analytics)
        ):
            page_info["analytics"] = cached_analytics
            report.setdefault("analytics_cache_hits", []).append(f"{page_info['title']} ({page_id})")
            continue
        analytics: Dict[str, dict] = {}
        for label, from_date in windows.items():
            try:
                views = client.get_view_count(page_id, from_date)
                unique_viewers = client.get_unique_viewer_count(page_id, from_date)
            except requests.HTTPError as exc:
                report.setdefault("warnings", []).append(
                    f"Failed to fetch analytics for {page_info['title']} ({page_id}) [{label}]: {exc}"
                )
                analytics[label] = {
                    "from_date": from_date.isoformat(),
                    "views": None,
                    "unique_viewers": None,
                }
                continue
            analytics[label] = {
                "from_date": from_date.isoformat(),
                "views": views,
                "unique_viewers": unique_viewers,
            }
        page_info["analytics"] = analytics


def export_metadata_for_page(page_info: dict, report: dict) -> dict:
    cached_metadata = page_info.get("cached_metadata_entry", {})
    page_id = str(page_info["id"])
    unsupported = [entry for entry in report.get("unsupported_content", []) if str(entry.get("page_id")) == page_id]
    unresolved = [entry for entry in report.get("unresolved_links", []) if entry.get("page") == page_info["title"]]
    return {
        "confluence_page_id": page_id,
        "title": page_info["title"],
        "space_key": page_info.get("space_key"),
        "url": page_info.get("page_url"),
        "created_at": page_info.get("created_at"),
        "created_by": page_info.get("created_by"),
        "created_by_account_id": page_info.get("created_by_account_id"),
        "updated_at": page_info.get("updated_at"),
        "updated_by": page_info.get("updated_by"),
        "updated_by_account_id": page_info.get("updated_by_account_id"),
        "version": page_info.get("version_number"),
        "ancestor_ids": page_info.get("ancestor_ids", []),
        "ancestor_titles": page_info.get("ancestor_titles", []),
        "labels": page_info.get("labels", []),
        "attachments": [attachment["filename"] for attachment in page_info.get("attachments", [])],
        "attachment_details": [
            {
                "filename": attachment["filename"],
                "local_filename": attachment.get("local_filename", attachment["filename"]),
                "id": attachment.get("id"),
                "version": attachment.get("version"),
                "media_type": attachment.get("media_type"),
            }
            for attachment in page_info.get("attachments", [])
        ] or cached_metadata.get("attachment_details", []),
        "analytics": page_info.get("analytics", {}) or cached_metadata.get("analytics", {}),
        "unsupported_content_count": len(unsupported),
        "unsupported_content_ids": [entry["id"] for entry in unsupported],
        "unresolved_link_count": len(unresolved),
        "markdown_path": str(page_info["markdown_path"]),
    }


def export_metadata_manifest(output_dir: Path, pages_by_id: Dict[str, dict], report: dict) -> dict:
    pages = []
    for page_id, page_info in sorted(pages_by_id.items(), key=lambda item: item[1]["markdown_path"].as_posix()):
        if page_info.get("export_suppressed"):
            continue
        entry = export_metadata_for_page(page_info, report)
        entry["markdown_relative_path"] = page_info["markdown_path"].relative_to(output_dir).as_posix()
        pages.append(entry)
    return {
        "root_page_id": report["page"],
        "output": report["output"],
        "metadata_outputs": report.get("metadata_outputs", ["none"]),
        "pages": pages,
    }


def apply_export_cache(pages_by_id: Dict[str, dict], output_dir: Path, report: dict) -> None:
    if report.get("force"):
        return
    manifest_index = load_existing_metadata_index(output_dir)
    today = date.today()
    for page_id, page_info in pages_by_id.items():
        markdown_path = page_markdown_path(page_info)
        sidecar_path = page_metadata_path(page_info)
        cached_entry = None
        sidecar = _load_json_mapping(sidecar_path) if sidecar_path.exists() else None
        if sidecar and str(sidecar.get("confluence_page_id", "")) == str(page_id):
            cached_entry = sidecar
        elif str(page_id) in manifest_index:
            cached_entry = manifest_index[str(page_id)]
        if not cached_entry:
            continue

        cached_version = cached_entry.get("version")
        live_version = page_info.get("version_number")
        if str(cached_version) != str(live_version):
            continue
        page_info["cached_metadata_entry"] = cached_entry
        page_info["reuse_cached_metadata"] = True
        if not markdown_path.exists():
            continue
        if "content-block" in page_info.get("metadata_outputs", []) and not file_modified_on(markdown_path, today):
            continue

        page_info["skip_markdown"] = True
        cached_details = cached_entry.get("attachment_details", [])
        cached_index: Dict[str, dict] = {}
        if isinstance(cached_details, list):
            for attachment in cached_details:
                if not isinstance(attachment, dict):
                    continue
                filename = str(attachment.get("filename", "")).strip()
                if filename:
                    cached_index[filename] = attachment
        if not cached_index:
            cached_attachments = cached_entry.get("attachments", [])
            if isinstance(cached_attachments, list):
                for filename in cached_attachments:
                    cleaned = str(filename).strip()
                    if cleaned:
                        cached_index[cleaned] = {"filename": cleaned, "local_filename": cleaned}
        page_info["cached_attachments_by_filename"] = cached_index


def page_signal_lines(page_info: dict, report: dict, today: Optional[date] = None) -> List[str]:
    today = today or date.today()
    unsupported = [
        entry for entry in report.get("unsupported_content", [])
        if str(entry.get("page_id")) == str(page_info["id"])
    ]
    unresolved = [
        entry for entry in report.get("unresolved_links", [])
        if entry.get("page") == page_info["title"]
    ]
    updated_at = page_info.get("updated_at")
    age_days = None
    if updated_at:
        try:
            age_days = (today - date.fromisoformat(str(updated_at)[:10])).days
        except ValueError:
            age_days = None
    analytics = page_info.get("analytics", {})
    year_to_date = analytics.get("year_to_date", {})
    trailing_year = analytics.get("trailing_year", {})
    all_time_proxy = analytics.get("all_time_proxy", {})
    lines = [
        "---",
        "",
        "## Export Metadata",
        "",
        "Machine-generated export metadata. This section was added during Confluence export and is not part of the original authored content.",
        "",
        f"- Last updated: {updated_at or 'unknown'}",
        f"- Version: {page_info.get('version_number') if page_info.get('version_number') is not None else 'unknown'}",
    ]
    if age_days is not None:
        lines.append(f"- Age at export: {age_days} days")
    lines.extend(
        [
            f"- Views year to date: {year_to_date.get('views', 'unknown')}",
            f"- Unique viewers year to date: {year_to_date.get('unique_viewers', 'unknown')}",
            f"- Views trailing year: {trailing_year.get('views', 'unknown')}",
            f"- Unique viewers trailing year: {trailing_year.get('unique_viewers', 'unknown')}",
            f"- Views long horizon: {all_time_proxy.get('views', 'unknown')}",
            f"- Unique viewers long horizon: {all_time_proxy.get('unique_viewers', 'unknown')}",
            f"- Unsupported embedded content: {len(unsupported)}",
            f"- Unresolved links: {len(unresolved)}",
            f"- Attachments: {len(page_info.get('attachments', []))}",
            "",
        ]
    )
    return lines


def write_pages(client: ConfluenceClient, pages_by_id: Dict[str, dict], output_dir: Path) -> None:
    title_to_id = {page["title"]: page_id for page_id, page in pages_by_id.items()}
    for page_id, page_info in pages_by_id.items():
        markdown_path = page_info["markdown_path"]
        if page_info.get("export_suppressed"):
            page_info["report"].setdefault("skipped_pages", []).append(
                f"{page_info['title']} (page {page_id}, root page content suppressed)"
            )
            continue
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        context = {
            "client": client,
            "page": page_info,
            "pages_by_id": pages_by_id,
            "title_to_id": title_to_id,
            "base_url": client.base_url,
            "report": page_info["report"],
        }
        if page_info.get("skip_markdown") and markdown_path.exists():
            sync_attachments_from_storage(page_info["body_storage"], context)
            page_info["report"].setdefault("skipped_pages", []).append(
                f"{markdown_path} (page {page_id}, version {page_info.get('version_number')})"
            )
        else:
            markdown_text = storage_to_markdown(page_info["body_storage"], context)
            if "content-block" in page_info.get("metadata_outputs", []):
                markdown_text = markdown_text.rstrip() + "\n\n" + "\n".join(page_signal_lines(page_info, page_info["report"]))
                markdown_text = markdown_text.rstrip() + "\n"
            markdown_path.write_text(markdown_text, encoding="utf-8")
            page_info["report"]["written_pages"].append(str(markdown_path))
        if "sidecar" in page_info.get("metadata_outputs", []):
            metadata_path = page_metadata_path(page_info)
            metadata_payload = export_metadata_for_page(page_info, page_info["report"])
            metadata_path.write_text(json.dumps(metadata_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            page_info["report"].setdefault("metadata_files_written", []).append(str(metadata_path))
        LOG.info("Wrote %s", markdown_path.relative_to(output_dir.parent if output_dir.parent != output_dir else output_dir))


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv or sys.argv[1:])

    if MISSING_DEPENDENCY_ERROR is not None:
        raise SystemExit(missing_dependency_message(MISSING_DEPENDENCY_ERROR))

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    try:
        project = load_phase_config(Path(args.project).expanduser().resolve(), PHASE_EXTRACTION)
    except ValueError as exc:
        LOG.error("Could not load project config: %s", exc)
        raise SystemExit(2)

    if project.get("provider") != "confluence":
        LOG.error("Project %s uses provider %s, expected %s", args.project, project.get("provider"), "confluence")
        raise SystemExit(2)

    if project["activity"] != PROJECT_ACTIVITY_EXPORT:
        LOG.error(
            "Project %s has activity %s, expected %s",
            args.project,
            project["activity"],
            PROJECT_ACTIVITY_EXPORT,
        )
        raise SystemExit(2)

    code = export_from_confluence(
        project,
        {
            "identity_config": args.identity_config,
            "base_url": args.base_url,
            "email": args.email,
        },
        helpers=sys.modules[__name__],
    )
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
