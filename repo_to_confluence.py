#!/usr/bin/env python3
"""Confluence Markdown tree sync - initial skeleton.

This script will be expanded to implement:
- walking a source folder
- converting markdown to Confluence storage format
- uploading pages and attachments
- translating relative links
- zombie detection

Run: python repo_to_confluence.py /path/to/source --space SPACEKEY --parent PARENT_PAGE_ID
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import List, Optional, Dict

import json
import re
from fnmatch import fnmatch

MISSING_DEPENDENCY_ERROR: Optional[ModuleNotFoundError] = None
try:
    import requests
    from bs4 import BeautifulSoup
    import markdown
except ModuleNotFoundError as exc:
    requests = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]
    markdown = None  # type: ignore[assignment]
    MISSING_DEPENDENCY_ERROR = exc

LOG = logging.getLogger("confluence_sync")
MAP_FILENAME = "confluence-map.json"
REPORTS_DIRNAME = "reports"
MERMAID_IMAGE_WIDTH = "1200"
MERMAID_DEFAULT_VIEWPORT = (1600, 900)
MERMAID_WIDE_VIEWPORT = (2400, 320)
MERMAID_TALL_VIEWPORT = (1600, 1200)
MERMAID_FLOWCHART_PADDING = 8
MERMAID_SVG_TRIM_PADDING = 4
MERMAID_EXPLICIT_SIZE_RATIO_MIN = 0.65


class ConfluenceClient:
    """Confluence REST wrapper for Cloud instances.

    Uses basic auth with email and API token.
    """

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

    def find_page(self, space: str, title: str) -> Optional[dict]:
        """Find a page by title within a space. Returns page dict or None."""
        url = self._api(
            f"/content?title={requests.utils.quote(title)}&spaceKey={requests.utils.quote(space)}"
            "&expand=version,body.storage,ancestors"
        )
        r = requests.get(url, auth=self._auth())
        self._raise_for_status(r)
        data = r.json()
        results = data.get("results", [])
        return results[0] if results else None

    def find_pages(self, space: str, title: str) -> List[dict]:
        """Find all pages by title within a space."""
        url = self._api(
            f"/content?title={requests.utils.quote(title)}&spaceKey={requests.utils.quote(space)}"
            "&expand=version,body.storage,ancestors"
        )
        r = requests.get(url, auth=self._auth())
        self._raise_for_status(r)
        return r.json().get("results", [])

    def create_page(self, space: str, title: str, parent_id: str, body_storage: str) -> dict:
        url = self._api("/content")
        payload = {
            "type": "page",
            "title": title,
            "ancestors": [{"id": str(parent_id)}],
            "space": {"key": space},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
        }
        r = requests.post(url, auth=self._auth(), json=payload)
        self._raise_for_status(r)
        return r.json()

    def update_page(self, page_id: str, title: str, body_storage: str, new_version: int) -> dict:
        url = self._api(f"/content/{page_id}")
        payload = {
            "id": str(page_id),
            "type": "page",
            "title": title,
            "version": {"number": new_version},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
        }
        r = requests.put(url, auth=self._auth(), json=payload)
        self._raise_for_status(r)
        return r.json()

    def get_page(self, page_id: str, expand: str = "body.storage,version,ancestors") -> dict:
        url = self._api(f"/content/{page_id}?expand={expand}")
        r = requests.get(url, auth=self._auth())
        self._raise_for_status(r)
        return r.json()

    def get_children(self, page_id: str) -> List[dict]:
        url = self._api(f"/content/{page_id}/child/page?limit=200&expand=version")
        r = requests.get(url, auth=self._auth())
        self._raise_for_status(r)
        return r.json().get("results", [])

    def list_all_descendants(self, root_id: str) -> List[dict]:
        # naive BFS over children
        out = []
        queue = [root_id]
        while queue:
            pid = queue.pop(0)
            children = self.get_children(pid)
            for c in children:
                out.append(c)
                queue.append(c["id"])
        return out

    def upload_attachment(self, page_id: str, file_path: Path) -> dict:
        url = self._api(f"/content/{page_id}/child/attachment")
        headers = {"X-Atlassian-Token": "nocheck"}
        with open(file_path, "rb") as fh:
            files = {"file": (file_path.name, fh, "application/octet-stream")}
            r = requests.post(url, auth=self._auth(), files=files, headers=headers)
        self._raise_for_status(r)
        return r.json()

    def find_attachment(self, page_id: str, filename: str) -> Optional[dict]:
        url = self._api(
            f"/content/{page_id}/child/attachment?filename={requests.utils.quote(filename)}&expand=version"
        )
        r = requests.get(url, auth=self._auth())
        self._raise_for_status(r)
        results = r.json().get("results", [])
        return results[0] if results else None

    def update_attachment(self, page_id: str, attachment_id: str, file_path: Path) -> dict:
        url = self._api(f"/content/{page_id}/child/attachment/{attachment_id}/data")
        headers = {"X-Atlassian-Token": "nocheck"}
        with open(file_path, "rb") as fh:
            files = {"file": (file_path.name, fh, "application/octet-stream")}
            data = {"minorEdit": "true"}
            r = requests.post(url, auth=self._auth(), files=files, data=data, headers=headers)
        self._raise_for_status(r)
        return r.json()

    def upsert_attachment(self, page_id: str, file_path: Path) -> dict:
        existing = self.find_attachment(page_id, file_path.name)
        if existing:
            return self.update_attachment(page_id, existing["id"], file_path)
        return self.upload_attachment(page_id, file_path)


def read_api_key(path: Path) -> Optional[str]:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    env = os.environ.get("CONFLUENCE_API_KEY")
    if env:
        return env.strip()
    return None


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync local markdown tree to Confluence")
    p.add_argument("source", help="Source folder to sync")
    p.add_argument("--space", required=True, help="Confluence space key")
    p.add_argument("--parent", required=True, help="Parent page id to attach the tree under")
    p.add_argument("--base-url", required=False, default="https://your-domain.atlassian.net", help="Confluence base URL")
    p.add_argument("--email", required=False, help="Confluence account email for API auth")
    p.add_argument("--exclude", required=False, help="Path to a confluenceignore file")
    p.add_argument("--dry-run", action="store_true", help="Run without making changes")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def load_ignore_patterns(exclude_path: Path) -> List[str]:
    defaults = [".DS_Store", ".git", ".gitignore"]
    patterns = list(defaults)
    conf: Optional[Path] = None
    if exclude_path.is_file():
        conf = exclude_path
    else:
        candidate = exclude_path / "confluenceignore"
        if candidate.exists():
            conf = candidate
    if conf and conf.exists():
        for line in conf.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            patterns.append(s)
    return patterns


def collect_markdown_files(source: Path, ignore_patterns: List[str]) -> List[Path]:
    out = []
    for root, dirs, files in os.walk(source):
        # filter dirs in place
        dirs[:] = [d for d in dirs if not any(fnmatch(d, pat) for pat in ignore_patterns)]
        for f in files:
            if any(fnmatch(f, pat) for pat in ignore_patterns):
                continue
            if f.lower().endswith(".md"):
                out.append(Path(root) / f)
    return sorted(out)


def parse_title(md_path: Path) -> Optional[str]:
    text = md_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("# "):
            return line.strip()[2:].strip()
    return None


def strip_leading_title_heading(markdown_text: str, title: str) -> str:
    lines = markdown_text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == f"# {title}":
            remaining = lines[idx + 1 :]
            while remaining and not remaining[0].strip():
                remaining = remaining[1:]
            return "\n".join(remaining)
        return markdown_text
    return markdown_text


def is_remote(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://") or url.startswith("//")


def normalize_html(s: str) -> str:
    # strip surrounding whitespace and normalize multiple spaces
    return re.sub(r"\s+", " ", s.strip())


def relative_source_path(path: Path, source: Path) -> str:
    return path.relative_to(source).as_posix()


def filesystem_safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "diagram"


def load_page_map(source: Path) -> Dict[str, dict]:
    map_path = source / MAP_FILENAME
    if not map_path.exists():
        return {}
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    pages = payload.get("pages", {})
    out: Dict[str, dict] = {}
    for key, value in pages.items():
        if isinstance(value, dict):
            out[str(key)] = {
                "page_id": str(value.get("page_id", "")),
                "content_hash": str(value.get("content_hash", "")),
                "confluence_version": value.get("confluence_version"),
            }
        else:
            out[str(key)] = {"page_id": str(value), "content_hash": "", "confluence_version": None}
    return out


def write_page_map(source: Path, space: str, root_page_id: str, path_map: Dict[str, dict]) -> None:
    map_path = source / MAP_FILENAME
    payload = {
        "space": space,
        "root_page_id": str(root_page_id),
        "pages": dict(sorted(path_map.items())),
    }
    map_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def find_git_repo_root(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return Path(result.stdout.strip())


def file_has_uncommitted_changes(repo_root: Path, file_path: Path) -> bool:
    relative_path = file_path.relative_to(repo_root)
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", str(relative_path)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def commit_page_map(source: Path) -> tuple[bool, str]:
    repo_root = find_git_repo_root(source)
    map_path = source / MAP_FILENAME
    if repo_root is None:
        return False, f"No git repository found for {source}; commit {map_path.name} manually if you want it tracked."

    try:
        relative_map_path = map_path.relative_to(repo_root)
    except ValueError:
        return False, f"{map_path} is outside git repo {repo_root}; commit it manually if needed."

    if not file_has_uncommitted_changes(repo_root, map_path):
        return False, f"{relative_map_path} is already up to date in git."

    add_result = subprocess.run(
        ["git", "add", "--", str(relative_map_path)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        detail = add_result.stderr.strip() or add_result.stdout.strip() or "git add failed"
        return False, f"Could not stage {relative_map_path}: {detail}"

    commit_result = subprocess.run(
        ["git", "commit", "-m", f"Update {MAP_FILENAME}", "--", str(relative_map_path)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        detail = commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed"
        return False, f"Staged {relative_map_path}, but could not commit it automatically: {detail}"

    sha_result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "new commit"
    return True, f"Committed {relative_map_path} in {repo_root} at {sha}. Remember to push your branch."


def content_fingerprint(body_storage: str) -> str:
    normalized = normalize_html(body_storage)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def page_is_under_parent(page: dict, parent_id: str) -> bool:
    return any(str(ancestor.get("id")) == str(parent_id) for ancestor in page.get("ancestors", []))


def choose_page_for_parent(matches: List[dict], parent_id: str) -> Optional[dict]:
    parent_id = str(parent_id)
    for page in matches:
        ancestors = page.get("ancestors", [])
        if ancestors and str(ancestors[-1].get("id")) == parent_id:
            return page
    for page in matches:
        if page_is_under_parent(page, parent_id):
            return page
    return None


def format_page_conflict(title: str, matches: List[dict], expected_parent: str) -> str:
    lines = [
        f"Cannot sync page '{title}': a page with this title already exists elsewhere in the space.",
        f"Expected parent page id: {expected_parent}",
        "Conflicting Confluence pages:",
    ]
    for page in matches:
        ancestors = " > ".join(str(ancestor.get("id")) for ancestor in page.get("ancestors", [])) or "(no ancestors returned)"
        lines.append(f"- page id={page.get('id')} ancestor_ids={ancestors}")
    return "\n".join(lines)


def find_local_title_conflicts(pages: Dict[Path, dict]) -> Dict[str, List[Path]]:
    by_title: Dict[str, List[Path]] = {}
    for path, meta in pages.items():
        by_title.setdefault(meta["title"], []).append(path)
    return {title: paths for title, paths in by_title.items() if len(paths) > 1}


def format_local_title_conflicts(conflicts: Dict[str, List[Path]]) -> str:
    lines = [
        "Cannot sync: duplicate local page titles would collide in Confluence.",
        "Each Markdown file title must be unique within the target Confluence space.",
    ]
    for title, paths in sorted(conflicts.items()):
        lines.append(f"- '{title}' appears in:")
        for path in sorted(paths):
            lines.append(f"  {path}")
    return "\n".join(lines)


def report_bullet_lines(items: List[str]) -> List[str]:
    return [f"- {item}" for item in items] if items else ["- None"]


def default_report_path(prefix: str) -> Path:
    reports_dir = Path(__file__).resolve().parent / REPORTS_DIRNAME
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return reports_dir / f"{prefix}_{timestamp}.md"


def write_repo_to_confluence_report(report_path: Path, report: dict) -> None:
    lines = [
        "# Repo To Confluence Report",
        "",
        "## Summary",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Report file: `{report_path}`",
        f"- Mode: {'dry-run' if report['dry_run'] else 'live'}",
        f"- Source: `{report['source']}`",
        f"- Space: `{report['space']}`",
        f"- Parent page id: `{report['parent']}`",
        f"- Pages created: {len(report['created_pages'])}",
        f"- Pages updated: {len(report['updated_pages'])}",
        f"- Pages skipped: {len(report['skipped_pages'])}",
        f"- Attachments uploaded: {len(report['uploaded_attachments'])}",
        f"- Bad links: {len(report['bad_links'])}",
        f"- Zombie pages: {len(report['zombies'])}",
        f"- Conflicts: {len(report['conflicts'])}",
        f"- Warnings: {len(report['warnings'])}",
        "",
        "## Created Pages",
        "",
        *report_bullet_lines(report["created_pages"]),
        "",
        "## Updated Pages",
        "",
        *report_bullet_lines(report["updated_pages"]),
        "",
        "## Skipped Pages",
        "",
        *report_bullet_lines(report["skipped_pages"]),
        "",
        "## Uploaded Attachments",
        "",
        *report_bullet_lines(report["uploaded_attachments"]),
        "",
        "## Bad Links",
        "",
    ]
    for entry in report["bad_links"]:
        lines.append(f"### {entry['source']}:{entry['line']}")
        lines.append("")
        lines.append(f"- Link text: `{entry['text']}`")
        lines.append(f"- Href: `{entry['href']}`")
        lines.append(f"- Resolved target: `{entry['resolved_target']}`")
        lines.append("")
    if not report["bad_links"]:
        lines.append("- None")
        lines.append("")
    lines.extend(
        [
            "## Zombie Pages",
            "",
        ]
    )
    if report["zombies"]:
        for entry in report["zombies"]:
            lines.append(f"- `{entry['title']}`: {entry['url']}")
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Conflicts",
            "",
            *report_bullet_lines(report["conflicts"]),
            "",
            "## Warnings",
            "",
            *report_bullet_lines(report["warnings"]),
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")


def extract_markdown_links(text: str) -> List[dict]:
    links: List[dict] = []
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in pattern.finditer(line):
            links.append(
                {
                    "line": line_no,
                    "text": match.group(1).strip(),
                    "href": match.group(2).strip(),
                }
            )
    return links


def consume_link_line(link_refs: List[dict], href: str, text: str) -> int:
    for idx, ref in enumerate(link_refs):
        if ref["href"] == href and ref["text"] == text:
            return link_refs.pop(idx)["line"]
    for idx, ref in enumerate(link_refs):
        if ref["href"] == href:
            return link_refs.pop(idx)["line"]
    return 0


def build_confluence_page_link(soup: BeautifulSoup, link_text: str, title: str, space: Optional[str] = None):
    link_tag = soup.new_tag("ac:link")
    page_tag = soup.new_tag("ri:page")
    page_tag.attrs["ri:content-title"] = title
    if space:
        page_tag.attrs["ri:space-key"] = space
    link_tag.append(page_tag)
    body_tag = soup.new_tag("ac:plain-text-link-body")
    body_tag.string = link_text or title
    link_tag.append(body_tag)
    return link_tag


def find_mermaid_cli() -> Optional[List[str]]:
    mmdc = shutil.which("mmdc")
    if mmdc:
        return [mmdc]
    return None


def find_mermaid_cli_package_root() -> Optional[Path]:
    mmdc = shutil.which("mmdc")
    if not mmdc:
        return None
    resolved = Path(mmdc).resolve()
    if resolved.name == "cli.js" and resolved.parent.name == "src":
        return resolved.parent.parent
    return None


def infer_mermaid_viewport(diagram_source: str) -> tuple[int, int]:
    for line in diagram_source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^(?:flowchart|graph)\s+(TB|TD|BT|LR|RL)\b", stripped, flags=re.IGNORECASE)
        if not match:
            break
        direction = match.group(1).upper()
        if direction in {"LR", "RL"}:
            return MERMAID_WIDE_VIEWPORT
        if direction in {"TB", "TD", "BT"}:
            return MERMAID_TALL_VIEWPORT
    return MERMAID_DEFAULT_VIEWPORT


def normalize_mermaid_svg_canvas(svg_path: Path) -> tuple[bool, str]:
    text = svg_path.read_text(encoding="utf-8")
    viewbox_match = re.search(r'viewBox="([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)"', text)
    height_match = re.search(r'height="([0-9.]+)"', text)
    max_width_match = re.search(r'max-width:\s*([0-9.]+)px', text)
    if not viewbox_match or not height_match:
        return False, "SVG did not expose a normalizable viewBox/height pair"

    x, y, viewbox_width, viewbox_height = [float(part) for part in viewbox_match.groups()]
    explicit_height = float(height_match.group(1))
    explicit_width = float(max_width_match.group(1)) if max_width_match else viewbox_width
    height_ratio = explicit_height / viewbox_height if viewbox_height else 0.0

    if explicit_height <= 0 or explicit_height >= viewbox_height:
        return False, "SVG canvas already appears tight enough"
    if height_ratio < MERMAID_EXPLICIT_SIZE_RATIO_MIN:
        return False, (
            f"Explicit SVG height ratio {height_ratio:.2f} is too small to trust for trimming"
        )

    text = re.sub(r'\sdata-trimmed-from="[^"]*"', "", text, count=1)
    text, viewbox_count = re.subn(
        r'viewBox="[^"]+"',
        f'viewBox="{x:g} {y:g} {explicit_width:g} {explicit_height:g}"',
        text,
        count=1,
    )
    text, width_count = re.subn(r'width="[^"]+"', f'width="{explicit_width:g}"', text, count=1)
    text, height_count = re.subn(r'height="[^"]+"', f'height="{explicit_height:g}"', text, count=1)
    if viewbox_count != 1 or width_count != 1 or height_count != 1:
        return False, "Could not rewrite root SVG size attributes safely"
    if 'preserveAspectRatio="' in text:
        text = re.sub(r'preserveAspectRatio="[^"]+"', 'preserveAspectRatio="xMinYMin meet"', text, count=1)
    else:
        text = text.replace("<svg ", '<svg preserveAspectRatio="xMinYMin meet" ', 1)
    text = text.replace("<svg ", '<svg data-trimmed-from="svg-explicit-size" ', 1)
    svg_path.write_text(text, encoding="utf-8")
    return True, ""


def render_mermaid_diagram(diagram_source: str, output_path: Path) -> tuple[bool, str]:
    cli = find_mermaid_cli()
    if cli is None:
        return False, "Mermaid CLI not found on PATH. Install `mmdc` to render Mermaid diagrams."

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        input_path = temp_dir / "diagram.mmd"
        config_path = temp_dir / "mermaid-config.json"
        input_path.write_text(diagram_source, encoding="utf-8")
        config_path.write_text(
            json.dumps({"flowchart": {"diagramPadding": MERMAID_FLOWCHART_PADDING}}),
            encoding="utf-8",
        )
        viewport_width, viewport_height = infer_mermaid_viewport(diagram_source)
        command = [
            *cli,
            "-i",
            str(input_path),
            "-o",
            str(output_path),
            "-b",
            "transparent",
            "-w",
            str(viewport_width),
            "-H",
            str(viewport_height),
            "-c",
            str(config_path),
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Mermaid render failed"
            return False, detail

    if not output_path.exists():
        return False, f"Mermaid renderer reported success but did not create {output_path.name}"

    normalized, normalize_detail = normalize_mermaid_svg_canvas(output_path)
    if normalized:
        return True, ""

    trimmed, trim_detail = trim_mermaid_svg_whitespace(output_path)
    if not trimmed and trim_detail:
        detail = trim_detail if not normalize_detail else f"{normalize_detail}; {trim_detail}"
        return True, f"Rendered Mermaid SVG but could not trim whitespace: {detail}"
    return True, ""


def trim_mermaid_svg_whitespace(svg_path: Path) -> tuple[bool, str]:
    package_root = find_mermaid_cli_package_root()
    node = shutil.which("node")
    if package_root is None or node is None:
        return False, "Node or Mermaid CLI package root not available"

    trim_script = """
const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

async function main() {
  const [packageRoot, svgPath, trimPadding] = process.argv.slice(1);
  const req = createRequire(path.join(packageRoot, 'package.json'));
  const puppeteer = req('puppeteer');
  const rawSvg = fs.readFileSync(svgPath, 'utf8');
  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 3200, height: 3200, deviceScaleFactor: 1 });
    await page.setContent(`<html><body style="margin:0;padding:0;">${rawSvg}</body></html>`, {
      waitUntil: 'load'
    });
    await new Promise(resolve => setTimeout(resolve, 100));
    const result = await page.evaluate((padding) => {
      const svg = document.querySelector('svg');
      if (!svg) {
        throw new Error('No <svg> element found for trimming');
      }
      const currentViewBox = svg.viewBox && svg.viewBox.baseVal
        ? {
            x: svg.viewBox.baseVal.x,
            y: svg.viewBox.baseVal.y,
            width: svg.viewBox.baseVal.width,
            height: svg.viewBox.baseVal.height
          }
        : null;
      if (currentViewBox && currentViewBox.width > 0 && currentViewBox.height > 0) {
        const scale = 2;
        const screenshotWidth = Math.max(1, Math.ceil(currentViewBox.width * scale));
        const screenshotHeight = Math.max(1, Math.ceil(currentViewBox.height * scale));
        const previousWidth = svg.getAttribute('width');
        const previousHeight = svg.getAttribute('height');
        const previousStyle = svg.getAttribute('style') || '';
        svg.setAttribute('width', String(screenshotWidth));
        svg.setAttribute('height', String(screenshotHeight));
        svg.setAttribute('style', previousStyle.replace(/max-width:\\s*[^;]+;?/g, ''));
        return {
          mode: 'png-bounds',
          viewBox: currentViewBox,
          screenshotWidth,
          screenshotHeight,
          padding,
          restore: {
            width: previousWidth,
            height: previousHeight,
            style: previousStyle
          }
        };
      }
      const candidates = [
        svg.querySelector('#my-svg'),
        svg.querySelector('g.output'),
        svg.querySelector('g.root'),
        svg.querySelector('g'),
        svg
      ].filter(Boolean);
      let bbox = null;
      let picked = null;
      for (const candidate of candidates) {
        const nextBox = candidate.getBBox();
        if (!Number.isFinite(nextBox.width) || !Number.isFinite(nextBox.height) || nextBox.width <= 0 || nextBox.height <= 0) {
          continue;
        }
        if (!bbox || (nextBox.width * nextBox.height) > (bbox.width * bbox.height)) {
          bbox = nextBox;
          picked = candidate;
        }
      }
      if (!bbox) {
        throw new Error('Could not find a non-empty rendered Mermaid bounding box');
      }
      const x = bbox.x - padding;
      const y = bbox.y - padding;
      const width = bbox.width + (padding * 2);
      const height = bbox.height + (padding * 2);
      svg.setAttribute('viewBox', `${x} ${y} ${width} ${height}`);
      svg.setAttribute('width', String(width));
      svg.setAttribute('height', String(height));
      svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');
      if (picked && picked !== svg && !svg.hasAttribute('data-trimmed-from')) {
        svg.setAttribute('data-trimmed-from', picked.tagName.toLowerCase());
      }
      return { mode: 'svg', svg: svg.outerHTML };
    }, Number(trimPadding));
    if (result && result.mode === 'png-bounds') {
      const handle = await page.$('svg');
      if (!handle) {
        throw new Error('Could not find SVG element for screenshot trimming');
      }
      const pngBase64 = await handle.screenshot({ encoding: 'base64', omitBackground: true });
      await page.evaluate((restore) => {
        const svg = document.querySelector('svg');
        if (!svg) return;
        if (restore.width !== null) svg.setAttribute('width', restore.width); else svg.removeAttribute('width');
        if (restore.height !== null) svg.setAttribute('height', restore.height); else svg.removeAttribute('height');
        if (restore.style) svg.setAttribute('style', restore.style); else svg.removeAttribute('style');
      }, result.restore);
      const pngBytes = Buffer.from(pngBase64, 'base64');
      const pngSignature = '89504e470d0a1a0a';
      if (pngBytes.subarray(0, 8).toString('hex') !== pngSignature) {
        throw new Error('Unexpected screenshot output while trimming SVG');
      }
      let offset = 8;
      let width = 0;
      let height = 0;
      while (offset + 8 <= pngBytes.length) {
        const length = pngBytes.readUInt32BE(offset);
        const type = pngBytes.subarray(offset + 4, offset + 8).toString('ascii');
        if (type === 'IHDR') {
          width = pngBytes.readUInt32BE(offset + 8);
          height = pngBytes.readUInt32BE(offset + 12);
          break;
        }
        offset += 12 + length;
      }
      if (!width || !height) {
        throw new Error('Could not read PNG dimensions while trimming SVG');
      }
      const zlib = require('zlib');
      const idatChunks = [];
      offset = 8;
      while (offset + 8 <= pngBytes.length) {
        const length = pngBytes.readUInt32BE(offset);
        const type = pngBytes.subarray(offset + 4, offset + 8).toString('ascii');
        if (type === 'IDAT') {
          idatChunks.push(pngBytes.subarray(offset + 8, offset + 8 + length));
        }
        offset += 12 + length;
      }
      const raw = zlib.inflateSync(Buffer.concat(idatChunks));
      const stride = (width * 4) + 1;
      let minX = width;
      let minY = height;
      let maxX = -1;
      let maxY = -1;
      const prev = Buffer.alloc(width * 4);
      const curr = Buffer.alloc(width * 4);
      for (let y = 0; y < height; y++) {
        const filter = raw[y * stride];
        raw.copy(curr, 0, y * stride + 1, y * stride + stride);
        if (filter === 1) {
          for (let i = 0; i < curr.length; i++) curr[i] = (curr[i] + (i >= 4 ? curr[i - 4] : 0)) & 255;
        } else if (filter === 2) {
          for (let i = 0; i < curr.length; i++) curr[i] = (curr[i] + prev[i]) & 255;
        } else if (filter === 3) {
          for (let i = 0; i < curr.length; i++) curr[i] = (curr[i] + Math.floor(((i >= 4 ? curr[i - 4] : 0) + prev[i]) / 2)) & 255;
        } else if (filter === 4) {
          const paeth = (a, b, c) => {
            const p = a + b - c;
            const pa = Math.abs(p - a);
            const pb = Math.abs(p - b);
            const pc = Math.abs(p - c);
            if (pa <= pb && pa <= pc) return a;
            if (pb <= pc) return b;
            return c;
          };
          for (let i = 0; i < curr.length; i++) {
            const a = i >= 4 ? curr[i - 4] : 0;
            const b = prev[i];
            const c = i >= 4 ? prev[i - 4] : 0;
            curr[i] = (curr[i] + paeth(a, b, c)) & 255;
          }
        }
        for (let x = 0; x < width; x++) {
          const alpha = curr[x * 4 + 3];
          if (alpha > 0) {
            if (x < minX) minX = x;
            if (y < minY) minY = y;
            if (x > maxX) maxX = x;
            if (y > maxY) maxY = y;
          }
        }
        curr.copy(prev);
      }
      if (maxX >= minX && maxY >= minY) {
        const scaleX = result.viewBox.width / result.screenshotWidth;
        const scaleY = result.viewBox.height / result.screenshotHeight;
        const x = result.viewBox.x + (minX * scaleX) - result.padding;
        const y = result.viewBox.y + (minY * scaleY) - result.padding;
        const trimWidth = ((maxX - minX + 1) * scaleX) + (result.padding * 2);
        const trimHeight = ((maxY - minY + 1) * scaleY) + (result.padding * 2);
        const svgText = rawSvg
          .replace(/viewBox="[^"]+"/, `viewBox="${x} ${y} ${trimWidth} ${trimHeight}"`)
          .replace(/width="[^"]+"/, `width="${trimWidth}"`)
          .replace(/height="[^"]+"/, `height="${trimHeight}"`);
        const finalSvg = svgText.includes('preserveAspectRatio=')
          ? svgText.replace(/preserveAspectRatio="[^"]+"/, 'preserveAspectRatio="xMinYMin meet"')
          : svgText.replace('<svg ', '<svg preserveAspectRatio="xMinYMin meet" ', 1);
        fs.writeFileSync(
          svgPath,
          finalSvg.includes('data-trimmed-from=')
            ? finalSvg.replace(/data-trimmed-from="[^"]+"/, 'data-trimmed-from="svg-raster-bounds"')
            : finalSvg.replace('<svg ', '<svg data-trimmed-from="svg-raster-bounds" ', 1),
          'utf8'
        );
        return;
      }
      throw new Error('Could not find non-transparent raster bounds while trimming SVG');
    }
    if (result && result.mode === 'svg' && result.svg) {
      fs.writeFileSync(svgPath, result.svg, 'utf8');
      return;
    }
    throw new Error('Unexpected Mermaid trim result');
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
""".strip()

    result = subprocess.run(
        [node, "-e", trim_script, str(package_root), str(svg_path), str(MERMAID_SVG_TRIM_PADDING)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "SVG trim failed"
        return False, detail
    return True, ""


def replace_mermaid_blocks(
    soup: BeautifulSoup,
    page_path: Path,
    page_title: str,
    page_id: str,
    client: ConfluenceClient,
    report: dict,
    dry_run: bool,
) -> None:
    mermaid_blocks = []
    for code_tag in soup.find_all("code"):
        classes = code_tag.get("class", [])
        if "language-mermaid" in classes:
            pre_tag = code_tag.parent if getattr(code_tag.parent, "name", None) == "pre" else code_tag
            mermaid_blocks.append((pre_tag, code_tag))

    if not mermaid_blocks:
        return

    base_name = filesystem_safe_stem(page_path.stem if page_path.stem else page_title)
    for index, (block_tag, code_tag) in enumerate(mermaid_blocks, start=1):
        diagram_source = unescape(code_tag.get_text())
        digest = hashlib.sha256(diagram_source.encode("utf-8")).hexdigest()[:10]
        filename = f"{base_name}-mermaid-{index}-{digest}.svg"

        if dry_run:
            placeholder = soup.new_tag("p")
            placeholder.string = f"[mermaid:{filename}]"
            block_tag.replace_with(placeholder)
            report["uploaded_attachments"].append(f"{filename} -> {page_title} ({page_id})")
            continue

        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_path = Path(temp_dir_name) / filename
            rendered, detail = render_mermaid_diagram(diagram_source, output_path)
            if not rendered:
                report["warnings"].append(
                    f"Could not render Mermaid diagram in {page_path}: {detail}. Leaving the Mermaid code block unchanged."
                )
                continue

            client.upsert_attachment(page_id, output_path)
            report["uploaded_attachments"].append(f"{filename} -> {page_title} ({page_id})")
            new_tag = soup.new_tag("ac:image")
            new_tag.attrs["ac:width"] = MERMAID_IMAGE_WIDTH
            ri = soup.new_tag("ri:attachment")
            ri.attrs["ri:filename"] = filename
            new_tag.append(ri)
            block_tag.replace_with(new_tag)


def main(argv: Optional[List[str]] = None):
    argv = argv or sys.argv[1:]
    args = parse_args(argv)

    if MISSING_DEPENDENCY_ERROR is not None:
        missing_name = getattr(MISSING_DEPENDENCY_ERROR, "name", "a required package")
        raise SystemExit(
            f"Missing dependency: {missing_name}. Install project dependencies with "
            f"'pip install -r requirements.txt' and try again."
        )

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    source = Path(args.source).resolve()
    report_path = default_report_path("repo_to_confluence_report")
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "source": str(source),
        "space": args.space,
        "parent": str(args.parent),
        "created_pages": [],
        "updated_pages": [],
        "skipped_pages": [],
        "uploaded_attachments": [],
        "bad_links": [],
        "zombies": [],
        "conflicts": [],
        "warnings": [],
    }

    def finalize_and_exit(code: int = 0) -> None:
        write_repo_to_confluence_report(report_path, report)
        LOG.info("Wrote run report to %s", report_path)
        if code:
            sys.exit(code)

    if not source.exists() or not source.is_dir():
        LOG.error("Source folder does not exist or is not a directory: %s", source)
        report["conflicts"].append(f"Source folder does not exist or is not a directory: {source}")
        finalize_and_exit(2)

    api_key_path = Path(__file__).parent / "conf-api-key.txt"
    token = read_api_key(api_key_path)
    if not token:
        LOG.error("Confluence API key not found in %s; fallback is CONFLUENCE_API_KEY", api_key_path)
        report["conflicts"].append(f"Confluence API key not found in {api_key_path}; fallback is CONFLUENCE_API_KEY")
        finalize_and_exit(3)

    if not args.email:
        LOG.warning("No email provided; you should pass --email for Confluence API auth")
        report["warnings"].append("No email provided; pass --email for Confluence API auth")

    client = ConfluenceClient(base_url=args.base_url, email=args.email or "", api_token=token)

    def narr(message: str, /):
        if args.dry_run:
            print("DRY-RUN: " + message)

    # Load ignore patterns
    ignore = load_ignore_patterns(Path(args.exclude).resolve() if args.exclude else source)
    existing_page_map = load_page_map(source)
    if existing_page_map:
        report["warnings"].append(f"Loaded {len(existing_page_map)} page-id mappings from {source / MAP_FILENAME}")

    # Collect markdown files
    md_files = collect_markdown_files(source, ignore)
    if not md_files:
        LOG.info("No markdown files found under %s", source)
        report["warnings"].append(f"No markdown files found under {source}")
        finalize_and_exit(0)

    # Parse titles and build mapping
    pages = {}
    for p in md_files:
        title = parse_title(p)
        if not title:
            LOG.warning("Skipping %s: no leading H1 title found", p)
            report["warnings"].append(f"Skipped {p}: no leading H1 title found")
            continue
        pages[p] = {"title": title, "path": p}

    local_title_conflicts = find_local_title_conflicts(pages)
    if local_title_conflicts:
        conflict_message = format_local_title_conflicts(local_title_conflicts)
        LOG.error(conflict_message)
        report["conflicts"].append(conflict_message)
        finalize_and_exit(4)

    # The provided parent page is treated as the root of the local tree.
    # A source-root readme.md updates that target page directly.
    root_page_id = str(args.parent)
    try:
        target_root_page = client.get_page(root_page_id)
    except requests.HTTPError as exc:
        conflict_message = f"Cannot access target parent/root page {root_page_id}: {exc}"
        LOG.error(conflict_message)
        report["conflicts"].append(conflict_message)
        finalize_and_exit(4)
    root_page_title = target_root_page.get("title", f"page-{root_page_id}")

    dir_readmes = {}
    for p in list(pages.keys()):
        d = p.parent
        readme = d / "readme.md"
        if readme in pages:
            dir_readmes[d] = readme
        else:
            # if a dir has no readme but contains files, create synthetic title from dirname
            if d not in dir_readmes:
                dir_readmes[d] = None

    # Two-pass: ensure page existence to obtain page ids
    local_to_pageid = {}

    # cache of directory -> page id (so files under dir attach to dir's readme page)
    dir_pageid: Dict[Path, str] = {}
    dir_pageid[source] = root_page_id

    # Ensure root-level directories attach to provided parent
    # Sort directories by depth to create parents before children
    dirs = sorted(set(p.parent for p in pages.keys()), key=lambda x: len(str(x)) )

    for d in dirs:
        if d == source:
            continue
        readme = d / "readme.md"
        if readme in pages:
            title = pages[readme]["title"]
        else:
            # fallback title
            title = d.name if d.name else "root"

        # find existing page by title in space
        chosen_parent = root_page_id
        # If this directory is nested, try to set parent to its ancestor's page id
        parent_dir = d.parent
        if parent_dir in dir_pageid:
            chosen_parent = dir_pageid[parent_dir]
        mapped_page_id = None
        if readme in pages:
            mapped_entry = existing_page_map.get(relative_source_path(readme, source), {})
            mapped_page_id = mapped_entry.get("page_id")
        existing = None
        if mapped_page_id:
            try:
                existing = client.get_page(mapped_page_id)
            except requests.HTTPError as exc:
                report["warnings"].append(
                    f"Mapped page id {mapped_page_id} for {relative_source_path(readme, source)} was not usable: {exc}"
                )
        matches = client.find_pages(args.space, title) if existing is None else []
        if existing is None:
            existing = choose_page_for_parent(matches, chosen_parent)

        if existing:
            page_id = existing["id"]
        elif matches:
            conflict_message = format_page_conflict(title, matches, chosen_parent)
            LOG.error(conflict_message)
            report["conflicts"].append(conflict_message)
            finalize_and_exit(4)
        else:
            LOG.info("Creating page '%s' under parent %s", title, chosen_parent)
            if not args.dry_run:
                created = client.create_page(args.space, title, chosen_parent, "<p>Placeholder</p>")
                page_id = created["id"]
                report["created_pages"].append(f"{title} (parent {chosen_parent}, id={page_id})")
            else:
                page_id = "dryrun-" + title
                report["created_pages"].append(f"Would create {title} under parent {chosen_parent}")

        dir_pageid[d] = page_id

    # Map each file to its parent page id
    for p in pages.keys():
        parent_dir = p.parent
        parent_page_id = dir_pageid.get(parent_dir, args.parent)
        # If the file itself is the readme, it is the directory page
        if p.name.lower() == "readme.md":
            local_to_pageid[p] = dir_pageid[parent_dir]
        else:
            # Create/find child page under the directory page with file's title
            title = pages[p]["title"]
            mapped_entry = existing_page_map.get(relative_source_path(p, source), {})
            mapped_page_id = mapped_entry.get("page_id")
            existing = None
            if mapped_page_id:
                try:
                    existing = client.get_page(mapped_page_id)
                except requests.HTTPError as exc:
                    report["warnings"].append(
                        f"Mapped page id {mapped_page_id} for {relative_source_path(p, source)} was not usable: {exc}"
                    )
            matches = client.find_pages(args.space, title) if existing is None else []
            if existing is None:
                existing = choose_page_for_parent(matches, parent_page_id)
            page_id = existing["id"] if existing else None
            if page_id:
                local_to_pageid[p] = page_id
                continue
            if matches:
                conflict_message = format_page_conflict(title, matches, parent_page_id)
                LOG.error(conflict_message)
                report["conflicts"].append(conflict_message)
                finalize_and_exit(4)
            if not page_id:
                LOG.info("Creating child page '%s' under %s", title, parent_page_id)
                if not args.dry_run:
                    created = client.create_page(args.space, title, parent_page_id, "<p>Placeholder</p>")
                    page_id = created["id"]
                    report["created_pages"].append(f"{title} (child of {parent_page_id}, id={page_id})")
                else:
                    page_id = "dryrun-" + title
                    report["created_pages"].append(f"Would create {title} under parent {parent_page_id}")
            local_to_pageid[p] = page_id

    # Second pass: convert and upload content, attachments, and translate links
    bad_links: List[dict] = []

    # Build title -> page_id map for link translation
    title_to_id = {pages[p]["title"]: pid for p, pid in local_to_pageid.items() if p.name.lower() == "readme.md"}
    # include non-readme pages too
    for p, pid in local_to_pageid.items():
        title_to_id[pages[p]["title"]] = pid

    for p, meta in pages.items():
        page_id = local_to_pageid[p]
        title = meta["title"]
        LOG.info("Processing %s -> page %s", p, page_id)
        markdown_text = p.read_text(encoding="utf-8")
        markdown_links = extract_markdown_links(markdown_text)
        body_markdown = strip_leading_title_heading(markdown_text, title)
        html = markdown.markdown(body_markdown, extensions=["fenced_code", "tables"]) 
        # post-process images and links
        soup = BeautifulSoup(html, "html.parser")

        replace_mermaid_blocks(
            soup=soup,
            page_path=p,
            page_title=title,
            page_id=page_id,
            client=client,
            report=report,
            dry_run=args.dry_run,
        )

        # Images: upload attachments for relative image sources
        for img in soup.find_all("img"):
            src = img.get("src")
            if not src:
                continue
            if not is_remote(src):
                img_path = (p.parent / src).resolve()
                if img_path.exists():
                    if not args.dry_run:
                        client.upsert_attachment(page_id, img_path)
                        report["uploaded_attachments"].append(f"{img_path.name} -> {title} ({page_id})")
                        # replace with Confluence storage-format image
                        new_tag = soup.new_tag("ac:image")
                        ri = soup.new_tag("ri:attachment")
                        ri.attrs["ri:filename"] = img_path.name
                        new_tag.append(ri)
                        img.replace_with(new_tag)
                    else:
                        img.replace_with(f"[image:{img_path.name}]")

        # Links: translate local markdown links to Confluence page URLs
        for a in soup.find_all("a"):
            href = a.get("href")
            if not href:
                continue
            # handle fragment links
            if href.endswith(".md") or ".md#" in href:
                parts = href.split("#", 1)
                filepart = parts[0]
                frag = parts[1] if len(parts) > 1 else None
                target = (p.parent / filepart).resolve()
                target_id = local_to_pageid.get(target)
                if target_id:
                    if frag:
                        new_url = f"{args.base_url}/wiki/pages/viewpage.action?pageId={target_id}#{frag}"
                        a["href"] = new_url
                    else:
                        link_text = a.get_text(" ", strip=True)
                        new_tag = build_confluence_page_link(
                            soup,
                            link_text=link_text,
                            title=pages[target]["title"],
                            space=args.space,
                        )
                        a.replace_with(new_tag)
                else:
                    link_text = a.get_text(" ", strip=True)
                    bad_links.append(
                        {
                            "source": str(p),
                            "line": consume_link_line(markdown_links, href, link_text),
                            "text": link_text,
                            "href": href,
                            "resolved_target": str(target),
                        }
                    )
                    report["bad_links"] = bad_links

        body_storage = str(soup)
        body_hash = content_fingerprint(body_storage)
        manifest_key = relative_source_path(p, source)
        mapped_entry = existing_page_map.setdefault(manifest_key, {})
        mapped_entry["page_id"] = str(page_id)

        if not args.dry_run:
            existing = client.get_page(page_id)
            existing_version = existing.get("version", {}).get("number", 1)
            page_title_for_update = title
            if p == source / "readme.md":
                page_title_for_update = existing.get("title", root_page_title)
            if (
                mapped_entry.get("page_id") == str(page_id)
                and mapped_entry.get("content_hash") == body_hash
                and mapped_entry.get("confluence_version") == existing_version
            ):
                LOG.info("Skipping update for %s (manifest hash and version unchanged)", title)
                report["skipped_pages"].append(f"{title} ({page_id}) unchanged via manifest hash/version")
                mapped_entry["content_hash"] = body_hash
                mapped_entry["confluence_version"] = existing_version
                continue
            existing_body = existing.get("body", {}).get("storage", {}).get("value", "")
            if normalize_html(existing_body) == normalize_html(body_storage):
                LOG.info("Skipping update for %s (unchanged)", title)
                report["skipped_pages"].append(f"{title} ({page_id}) unchanged")
                mapped_entry["content_hash"] = body_hash
                mapped_entry["confluence_version"] = existing_version
            else:
                LOG.info("Updating page %s (id=%s) to version %s", title, page_id, existing_version + 1)
                client.update_page(page_id, page_title_for_update, body_storage, existing_version + 1)
                report["updated_pages"].append(f"{page_title_for_update} ({page_id}) -> version {existing_version + 1}")
                mapped_entry["content_hash"] = body_hash
                mapped_entry["confluence_version"] = existing_version + 1
        else:
            LOG.info("Dry-run: would update page %s (id=%s)", title, page_id)
            report["updated_pages"].append(f"Would update {title} ({page_id})")

    # Zombie detection: list all descendants under args.parent and find pages not in local_to_pageid
    all_desc = client.list_all_descendants(args.parent)
    local_ids = set(v for v in local_to_pageid.values())
    zombies = []
    for p in all_desc:
        if p["id"] not in local_ids:
            zombies.append(p)
    report["zombies"] = [
        {
            "title": z.get("title"),
            "url": f"{args.base_url}/wiki/pages/viewpage.action?pageId={z['id']}",
        }
        for z in zombies
    ]
    report["bad_links"] = bad_links

    if not args.dry_run:
        path_map = {}
        for path, page_id in local_to_pageid.items():
            key = relative_source_path(path, source)
            entry = existing_page_map.get(key, {})
            path_map[key] = {
                "page_id": str(page_id),
                "content_hash": str(entry.get("content_hash", "")),
                "confluence_version": entry.get("confluence_version"),
            }
        write_page_map(source, args.space, root_page_id, path_map)
        report["warnings"].append(f"Wrote page-id mapping manifest to {source / MAP_FILENAME}")
        committed, git_message = commit_page_map(source)
        if committed:
            LOG.info(git_message)
            report["warnings"].append(git_message)
        else:
            LOG.warning(git_message)
            report["warnings"].append(git_message)
    else:
        report["warnings"].append("Dry-run mode: did not update page-id mapping manifest")

    if zombies:
        LOG.info("Found %d zombie pages", len(zombies))
    else:
        LOG.info("No zombie pages detected")

    if bad_links:
        LOG.warning("Found %d unresolved markdown links", len(bad_links))
    else:
        LOG.info("No unresolved markdown links detected")

    finalize_and_exit(0)



if __name__ == "__main__":
    main()
