from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import List

from .constants import PROJECT_ROOT, REPORTS_DIRNAME


def default_report_path(prefix: str) -> Path:
    reports_dir = PROJECT_ROOT / REPORTS_DIRNAME
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return reports_dir / f"{prefix}_{timestamp}.md"


def filesystem_safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "report"


def project_report_path(prefix: str, project_name: str | None = None, project_path: str | None = None) -> Path:
    reports_dir = PROJECT_ROOT / REPORTS_DIRNAME
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    label_source = project_name
    if not label_source and project_path:
        label_source = Path(project_path).stem
    if label_source:
        label = filesystem_safe_stem(label_source)
        return reports_dir / f"{label}_{prefix}_{timestamp}.md"
    return reports_dir / f"{prefix}_{timestamp}.md"


def report_bullet_lines(items: List[str]) -> List[str]:
    return [f"- {item}" for item in items] if items else ["- None"]


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
    lines.extend(["## Zombie Pages", ""])
    if report["zombies"]:
        for entry in report["zombies"]:
            lines.append(f"- `{entry['title']}`: {entry['url']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Conflicts", "", *report_bullet_lines(report["conflicts"]), "", "## Warnings", "", *report_bullet_lines(report["warnings"]), ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_confluence_to_repo_report(report_path: Path, report: dict) -> None:
    lines = [
        "# Confluence To Repo Report",
        "",
        "## Summary",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Report file: `{report_path}`",
        f"- Output: `{report['output']}`",
        f"- Root page id: `{report['page']}`",
        f"- Recurse: {report['recurse']}",
        f"- Metadata outputs: `{', '.join(report.get('metadata_outputs', ['none']))}`",
        f"- Excluded page ids: {len(report.get('excluded_pages', []))}",
        f"- Pages skipped from cache: {len(report.get('skipped_pages', []))}",
        f"- Pages written: {len(report['written_pages'])}",
        f"- Metadata files written: {len(report.get('metadata_files_written', []))}",
        f"- Attachments downloaded: {len(report['downloaded_attachments'])}",
        f"- Attachment cache hits: {len(report.get('attachment_cache_hits', []))}",
        f"- Unresolved links: {len(report['unresolved_links'])}",
        f"- Unsupported content items: {len(report.get('unsupported_content', []))}",
        f"- Warnings: {len(report['warnings'])}",
        "",
        "## Excluded Page Ids",
        "",
        *report_bullet_lines(report.get("excluded_pages", [])),
        "",
        "## Skipped Pages",
        "",
        *report_bullet_lines(report.get("skipped_pages", [])),
        "",
        "## Written Pages",
        "",
        *report_bullet_lines(report["written_pages"]),
        "",
        "## Metadata Files",
        "",
        *report_bullet_lines(report.get("metadata_files_written", [])),
        "",
        "## Downloaded Attachments",
        "",
        *report_bullet_lines(report["downloaded_attachments"]),
        "",
        "## Attachment Cache Hits",
        "",
        *report_bullet_lines(report.get("attachment_cache_hits", [])),
        "",
        "## Unsupported Content",
        "",
    ]
    if report.get("unsupported_content"):
        for entry in report["unsupported_content"]:
            lines.append(f"### {entry['id']} on {entry['page']}")
            lines.append("")
            lines.append(f"- Page id: `{entry['page_id']}`")
            lines.append(f"- Kind: `{entry['kind']}`")
            lines.append(f"- Type: `{entry['content_type']}`")
            lines.append(f"- Detail: `{entry['detail']}`")
            lines.append(f"- Snippet: `{entry['snippet']}`")
            lines.append("")
    else:
        lines.append("- None")
        lines.append("")
    lines.extend([
        "## Unresolved Links",
        "",
    ])
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
    lines.extend(["## Warnings", "", *report_bullet_lines(report["warnings"]), ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_project_report(report_path: Path, report: dict) -> None:
    lines = [
        "# Confluence Project Report",
        "",
        "## Summary",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Report file: `{report_path}`",
        f"- Project file: `{report['project']}`",
        f"- Project name: `{report['project_name'] or 'unnamed'}`",
        f"- Activity: `{report['activity']}`",
        f"- Successful targets: {len(report['successful_targets'])}",
        f"- Failed targets: {len(report['failed_targets'])}",
        f"- Warnings: {len(report['warnings'])}",
        "",
        "## Successful Targets",
        "",
    ]
    if report["successful_targets"]:
        for target in report["successful_targets"]:
            lines.append(f"- `{target['label']}`: {target['detail']} (exit `{target['exit_code']}`)")
    else:
        lines.append("- None")

    lines.extend(["", "## Failed Targets", ""])
    if report["failed_targets"]:
        for target in report["failed_targets"]:
            lines.append(f"- `{target['label']}`: {target['detail']} (exit `{target['exit_code']}`)")
    else:
        lines.append("- None")

    lines.extend(["", "## Warnings", "", *report_bullet_lines(report["warnings"]), ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
