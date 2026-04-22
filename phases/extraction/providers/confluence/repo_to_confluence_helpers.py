#!/usr/bin/env python3
"""Sync a local Markdown tree to Confluence using a project YAML file."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from fnmatch import fnmatch
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional

from shared.project_config import PHASE_EXTRACTION, PROJECT_ACTIVITY_PUBLISH, load_phase_config
from phases.extraction.providers.confluence.auth import missing_dependency_message
from phases.extraction.providers.confluence.client import ConfluenceClient
from phases.extraction.providers.confluence.conf_io import sync_to_confluence
from phases.extraction.providers.confluence.constants import MAP_FILENAME
from phases.extraction.providers.confluence.deps import BeautifulSoup, MISSING_DEPENDENCY_ERROR, markdown, requests

LOG = logging.getLogger("confluence_sync")
MERMAID_IMAGE_WIDTH = "1200"
MERMAID_DEFAULT_VIEWPORT = (1600, 900)
MERMAID_WIDE_VIEWPORT = (2400, 320)
MERMAID_TALL_VIEWPORT = (1600, 1200)
MERMAID_FLOWCHART_PADDING = 8
MERMAID_SVG_TRIM_PADDING = 4
MERMAID_EXPLICIT_SIZE_RATIO_MIN = 0.65


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a publish project file")
    p.add_argument("--project", required=True, help="Path to a publish project YAML file")
    p.add_argument("--base-url", required=False, default=None, help="Optional override for the Confluence base URL")
    p.add_argument("--email", required=False, default=None, help="Optional override for the Confluence auth email")
    p.add_argument("--identity-config", required=False, default=None, help="Path to a YAML identity config file")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def load_ignore_patterns(extra_patterns: Optional[List[str]] = None) -> List[str]:
    defaults = [".DS_Store", ".git", ".gitignore", "*.metadata.json", "*.analysis.json"]
    patterns = list(defaults)
    if extra_patterns:
        patterns.extend(pattern.strip() for pattern in extra_patterns if pattern and pattern.strip())
    return patterns


def should_ignore_path(path: Path, source: Path, ignore_patterns: List[str], is_dir: bool = False) -> bool:
    rel = path.relative_to(source).as_posix()
    name = path.name
    dir_rel = f"{rel}/" if is_dir else rel
    for pattern in ignore_patterns:
        if fnmatch(name, pattern) or fnmatch(rel, pattern) or (is_dir and fnmatch(dir_rel, pattern)):
            return True
    return False


def collect_markdown_files(source: Path, ignore_patterns: List[str]) -> List[Path]:
    out = []
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        dirs[:] = [
            directory
            for directory in dirs
            if not should_ignore_path(root_path / directory, source, ignore_patterns, is_dir=True)
        ]
        for f in files:
            file_path = root_path / f
            if should_ignore_path(file_path, source, ignore_patterns):
                continue
            if f.lower().endswith(".md"):
                out.append(file_path)
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


def strip_generated_metadata_block(markdown_text: str) -> str:
    pattern = re.compile(
        r"\n*---\n\n## Export Metadata\n\n"
        r"Machine-generated export metadata\. This section was added during Confluence export and is not part of the original authored content\.\n"
        r"(?:\n- .*)+\n*\Z",
        re.DOTALL,
    )
    stripped = pattern.sub("", markdown_text)
    if stripped == markdown_text:
        return markdown_text
    return stripped.rstrip() + "\n"


def strip_generated_analysis_block(markdown_text: str) -> str:
    pattern = re.compile(
        r"\n*---\n\n## Analysis Assessment\n\n"
        r"Machine-generated analysis signals\. This section was added by kurate during analysis and is not part of the original authored content\.\n"
        r"(?:\n- .*)+\n*\Z",
        re.DOTALL,
    )
    stripped = pattern.sub("", markdown_text)
    if stripped == markdown_text:
        return markdown_text
    return stripped.rstrip() + "\n"


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

    if project["activity"] != PROJECT_ACTIVITY_PUBLISH:
        LOG.error(
            "Project %s has activity %s, expected %s",
            args.project,
            project["activity"],
            PROJECT_ACTIVITY_PUBLISH,
        )
        raise SystemExit(2)

    code = sync_to_confluence(
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
