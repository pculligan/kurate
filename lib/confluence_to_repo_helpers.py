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

from lib.auth import missing_dependency_message
from lib.client import ConfluenceClient
from lib.conf_io import export_from_confluence
from lib.constants import MAP_FILENAME
from lib.deps import BeautifulSoup, MISSING_DEPENDENCY_ERROR, NavigableString, Tag, requests
from lib.project_config import PROJECT_ACTIVITY_EXPORT, load_project_config

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
    if name == "ac:structured-macro":
        return render_macro(node)
    if name == "ac:layout":
        return "".join(render_block(child, context) for child in node.children)
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


def render_macro(node: Tag) -> str:
    macro_name = node.get("ac:name", "macro")
    if macro_name in {"code", "noformat"}:
        body = node.find("ac:plain-text-body")
        if body:
            return f"```text\n{body.get_text()}\n```\n\n"
    context = getattr(render_macro, "_context", None)
    if context is None:
        return f"> Unsupported Confluence macro `{macro_name}`\n\n"
    body = node.find("ac:plain-text-body")
    rich_body = node.find("ac:rich-text-body")
    detail = f"Unsupported Confluence macro `{macro_name}`"
    if body and body.get_text(strip=True):
        detail += f" with plain text body `{body.get_text(strip=True)[:80]}`"
    elif rich_body and rich_body.get_text(" ", strip=True):
        detail += f" with rich text body `{rich_body.get_text(' ', strip=True)[:80]}`"
    return render_unsupported_block(node, context, kind="macro", content_type=macro_name, detail=detail)


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
            page_info["report"]["downloaded_attachments"].append(f"{page_info['title']}: {local_filename} -> {destination}")
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
            context["report"]["unresolved_links"].append(
                {
                    "page": page_info["title"],
                    "kind": "image",
                    "detail": f"missing attachment image {filename}",
                }
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
        target_id = page_ref.get("ri:content-id")
        if not target_id:
            title = page_ref.get("ri:content-title")
            if title:
                target_id = title_to_id.get(title)
        if target_id and target_id in export_pages:
            href = relative_markdown_link(page_info, export_pages[target_id])
            return f"[{link_text or export_pages[target_id]['title']}]({href})"
        if target_id:
            href = f"{context['base_url']}/wiki/pages/viewpage.action?pageId={target_id}"
            context["report"]["unresolved_links"].append(
                {
                    "page": page_info["title"],
                    "kind": "page",
                    "detail": f"{link_text or href} -> {href}",
                }
            )
            return f"[{link_text or href}]({href})"
        title = page_ref.get("ri:content-title")
        if title:
            context["report"]["unresolved_links"].append(
                {
                    "page": page_info["title"],
                    "kind": "page",
                    "detail": f"{link_text or title} -> unresolved title {title}",
                }
            )
            return f"[{link_text or title}]({title})"

    if attachment_ref:
        filename = attachment_ref.get("ri:filename")
        if filename:
            asset_path = download_attachment_for_page(client, page_info, filename)
            if asset_path:
                href = relative_asset_link(page_info, asset_path)
                return f"[{link_text or filename}]({href})"
            context["report"]["unresolved_links"].append(
                {
                    "page": page_info["title"],
                    "kind": "attachment",
                    "detail": f"{link_text or filename} -> missing attachment {filename}",
                }
            )

    if url_ref and url_ref.get("ri:value"):
        url = url_ref["ri:value"]
        return f"[{link_text or url}]({url})"

    href = node.get("href")
    if href:
        return f"[{link_text or href}]({href})"
    return link_text


def storage_to_markdown(storage_html: str, context: dict) -> str:
    render_macro._context = context
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
) -> tuple[Dict[str, dict], Dict[str, List[str]], List[str]]:
    pages_by_id: Dict[str, dict] = {}
    children_by_id: Dict[str, List[str]] = {}
    excluded_hits: List[str] = []

    def walk(page_id: str) -> None:
        if page_id in excluded_page_ids:
            excluded_hits.append(page_id)
            return
        if page_id in pages_by_id:
            return
        page = client.get_page(page_id, expand="body.storage,version,ancestors,history,metadata.labels")
        pages_by_id[page_id] = {
            "id": page["id"],
            "title": page["title"],
            "body_storage": page.get("body", {}).get("storage", {}).get("value", ""),
            "space_key": page.get("space", {}).get("key"),
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
        ],
        "analytics": page_info.get("analytics", {}),
        "unsupported_content_count": len(unsupported),
        "unsupported_content_ids": [entry["id"] for entry in unsupported],
        "unresolved_link_count": len(unresolved),
        "markdown_path": str(page_info["markdown_path"]),
    }


def export_metadata_manifest(output_dir: Path, pages_by_id: Dict[str, dict], report: dict) -> dict:
    pages = []
    for page_id, page_info in sorted(pages_by_id.items(), key=lambda item: item[1]["markdown_path"].as_posix()):
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
        project = load_project_config(Path(args.project).expanduser().resolve())
    except ValueError as exc:
        LOG.error("Could not load project config: %s", exc)
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
