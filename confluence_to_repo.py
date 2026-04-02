#!/usr/bin/env python3
"""Export a Confluence page tree into a local Markdown folder structure.

Run:
python confluence_to_repo.py /path/to/output --page PAGE_ID [--recurse]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

LOG = logging.getLogger("confluence_export")


class ConfluenceClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token

    def _auth(self):
        return (self.email, self.api_token)

    def _api(self, path: str) -> str:
        return f"{self.base_url}/wiki/rest/api{path}"

    def _raise_for_status(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text.strip()
            if detail:
                raise requests.HTTPError(f"{exc}\nResponse body: {detail}", response=response) from exc
            raise

    def get_page(self, page_id: str, expand: str = "body.storage,version,space") -> dict:
        url = self._api(f"/content/{page_id}?expand={expand}")
        response = requests.get(url, auth=self._auth())
        self._raise_for_status(response)
        return response.json()

    def get_children(self, page_id: str) -> List[dict]:
        results: List[dict] = []
        start = 0
        while True:
            url = self._api(f"/content/{page_id}/child/page?limit=200&start={start}&expand=body.storage,space")
            response = requests.get(url, auth=self._auth())
            self._raise_for_status(response)
            payload = response.json()
            batch = payload.get("results", [])
            results.extend(batch)
            if payload.get("size", 0) + payload.get("start", 0) >= payload.get("totalSize", 0):
                break
            start += payload.get("size", 0)
        return results

    def get_attachments(self, page_id: str) -> List[dict]:
        results: List[dict] = []
        start = 0
        while True:
            url = self._api(f"/content/{page_id}/child/attachment?limit=200&start={start}")
            response = requests.get(url, auth=self._auth())
            self._raise_for_status(response)
            payload = response.json()
            batch = payload.get("results", [])
            results.extend(batch)
            if payload.get("size", 0) + payload.get("start", 0) >= payload.get("totalSize", 0):
                break
            start += payload.get("size", 0)
        return results

    def download_attachment(self, download_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = urljoin(f"{self.base_url}/wiki/", download_path.lstrip("/"))
        response = requests.get(url, auth=self._auth(), stream=True)
        self._raise_for_status(response)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)


def read_api_key(path: Path) -> Optional[str]:
    env = os.environ.get("CONFLUENCE_API_KEY")
    if env:
        return env.strip()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a Confluence page tree to a local Markdown folder structure")
    parser.add_argument("output", help="Output folder to write the exported tree into")
    parser.add_argument("--page", required=True, help="Root Confluence page id to export")
    parser.add_argument("--base-url", default="https://your-domain.atlassian.net", help="Confluence base URL")
    parser.add_argument("--email", help="Confluence account email for API auth")
    parser.add_argument("--recurse", action="store_true", help="Export child pages recursively")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    return parser.parse_args(argv)


def report_bullet_lines(items: List[str]) -> List[str]:
    return [f"- {item}" for item in items] if items else ["- None"]


def write_confluence_to_repo_report(report_path: Path, report: dict) -> None:
    lines = [
        "# Confluence To Repo Report",
        "",
        "## Summary",
        "",
        f"- Output: `{report['output']}`",
        f"- Root page id: `{report['page']}`",
        f"- Recurse: {report['recurse']}",
        f"- Pages written: {len(report['written_pages'])}",
        f"- Attachments downloaded: {len(report['downloaded_attachments'])}",
        f"- Unresolved links: {len(report['unresolved_links'])}",
        f"- Warnings: {len(report['warnings'])}",
        "",
        "## Written Pages",
        "",
        *report_bullet_lines(report["written_pages"]),
        "",
        "## Downloaded Attachments",
        "",
        *report_bullet_lines(report["downloaded_attachments"]),
        "",
        "## Unresolved Links",
        "",
    ]
    if report["unresolved_links"]:
        for entry in report["unresolved_links"]:
            lines.append(f"### {entry['page']}")
            lines.append("")
            lines.append(f"- Kind: `{entry['kind']}`")
            lines.append(f"- Detail: `{entry['detail']}`")
            lines.append("")
    else:
        lines.append("- None")
        lines.append("")
    lines.extend(
        [
            "## Warnings",
            "",
            *report_bullet_lines(report["warnings"]),
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")


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
    if markdown_path.name == "readme.md":
        return markdown_path.parent / "assets"
    return markdown_path.parent / f"{markdown_path.stem}-assets"


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
            if has_children:
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
    return "".join(render_block(child, context) for child in node.children)


def render_macro(node: Tag) -> str:
    macro_name = node.get("ac:name", "macro")
    body = node.find("ac:plain-text-body")
    if body:
        return f"```text\n{body.get_text()}\n```\n\n"
    rich_body = node.find("ac:rich-text-body")
    if rich_body:
        content = rich_body.get_text("\n", strip=True)
        return f"> Confluence macro `{macro_name}`\n>\n> {content}\n\n"
    return f"> Confluence macro `{macro_name}`\n\n"


def relative_markdown_link(from_page: dict, to_page: dict) -> str:
    return os.path.relpath(to_page["markdown_path"], start=from_page["markdown_path"].parent).replace(os.sep, "/")


def relative_asset_link(from_page: dict, asset_path: Path) -> str:
    return os.path.relpath(asset_path, start=from_page["markdown_path"].parent).replace(os.sep, "/")


def attachment_lookup(page_info: dict) -> Dict[str, dict]:
    return {attachment["filename"]: attachment for attachment in page_info.get("attachments", [])}


def download_attachment_for_page(client: ConfluenceClient, page_info: dict, filename: str) -> Optional[Path]:
    attachment = attachment_lookup(page_info).get(filename)
    if not attachment:
        return None
    destination = page_assets_dir(page_info) / filename
    if not destination.exists():
        try:
            client.download_attachment(attachment["download"], destination)
            page_info["report"]["downloaded_attachments"].append(f"{page_info['title']}: {filename} -> {destination}")
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
    soup = BeautifulSoup(storage_html, "html.parser")
    body_parts = [render_block(child, context) for child in soup.contents]
    body = squash_blank_lines("".join(body_parts))
    title = context["page"]["title"]
    return f"# {title}\n\n{body}"


def collect_pages(client: ConfluenceClient, root_id: str, recurse: bool) -> tuple[Dict[str, dict], Dict[str, List[str]]]:
    pages_by_id: Dict[str, dict] = {}
    children_by_id: Dict[str, List[str]] = {}

    def walk(page_id: str) -> None:
        if page_id in pages_by_id:
            return
        page = client.get_page(page_id)
        pages_by_id[page_id] = {
            "id": page["id"],
            "title": page["title"],
            "body_storage": page.get("body", {}).get("storage", {}).get("value", ""),
            "space_key": page.get("space", {}).get("key"),
        }
        children_by_id[page_id] = []
        if not recurse and page_id != root_id:
            return
        if recurse:
            children = client.get_children(page_id)
            for child in children:
                child_id = child["id"]
                children_by_id[page_id].append(child_id)
                walk(child_id)

    walk(root_id)
    return pages_by_id, children_by_id


def enrich_attachments(client: ConfluenceClient, pages_by_id: Dict[str, dict]) -> None:
    for page_id, page_info in pages_by_id.items():
        attachments = []
        for attachment in client.get_attachments(page_id):
            filename = read_attachment_filename(attachment)
            download = attachment.get("_links", {}).get("download")
            if download:
                attachments.append({"filename": filename, "download": download})
        page_info["attachments"] = attachments


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
        markdown_text = storage_to_markdown(page_info["body_storage"], context)
        markdown_path.write_text(markdown_text, encoding="utf-8")
        page_info["report"]["written_pages"].append(str(markdown_path))
        LOG.info("Wrote %s", markdown_path.relative_to(output_dir.parent if output_dir.parent != output_dir else output_dir))


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path.cwd() / "confluence_to_repo_report.md"
    report = {
        "output": str(output_dir),
        "page": str(args.page),
        "recurse": bool(args.recurse),
        "written_pages": [],
        "downloaded_attachments": [],
        "unresolved_links": [],
        "warnings": [],
    }

    def finalize_and_exit(code: int = 0) -> None:
        write_confluence_to_repo_report(report_path, report)
        if code:
            sys.exit(code)

    api_key_path = Path(__file__).parent / "conf-api-key.txt"
    token = read_api_key(api_key_path)
    if not token:
        LOG.error("Confluence API key not found in %s or CONFLUENCE_API_KEY env var", api_key_path)
        report["warnings"].append(f"Confluence API key not found in {api_key_path} or CONFLUENCE_API_KEY env var")
        finalize_and_exit(3)
    if not args.email:
        LOG.warning("No email provided; you should pass --email for Confluence API auth")
        report["warnings"].append("No email provided; pass --email for Confluence API auth")

    client = ConfluenceClient(args.base_url, args.email or "", token)
    pages_by_id, children_by_id = collect_pages(client, args.page, args.recurse)
    for page_info in pages_by_id.values():
        page_info["report"] = report
    enrich_attachments(client, pages_by_id)
    assign_paths(pages_by_id, children_by_id, args.page, output_dir)
    write_pages(client, pages_by_id, output_dir)
    finalize_and_exit(0)


if __name__ == "__main__":
    main()
