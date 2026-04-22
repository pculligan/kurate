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
    stage_label = filesystem_safe_stem(prefix).replace("_", "-")
    if label_source:
        label = filesystem_safe_stem(label_source)
        return reports_dir / f"{label}-{timestamp}-{stage_label}.md"
    return reports_dir / f"{timestamp}-{stage_label}.md"


def report_bullet_lines(items: List[str]) -> List[str]:
    return [f"- {item}" for item in items] if items else ["- None"]


def _report_root(report: dict) -> Path | None:
    for key in ("workspace", "output", "source"):
        value = report.get(key)
        if value:
            try:
                return Path(value).resolve()
            except OSError:
                return Path(value)
    return None


def report_display_path(value: str, report: dict) -> str:
    try:
        path = Path(value).resolve()
    except OSError:
        path = Path(value)
    root = _report_root(report)
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return str(path)


def grouped_downloaded_attachments(report: dict) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = {}
    for entry in report.get("downloaded_attachments", []):
        if isinstance(entry, dict):
            page_path = report_display_path(str(entry.get("page_path", "")), report)
            grouped.setdefault(page_path, []).append(str(entry.get("filename", "")))
        else:
            grouped.setdefault("Unknown", []).append(str(entry))
    return sorted(grouped.items(), key=lambda item: item[0])


def format_unresolved_link_entry(entry: dict) -> list[str]:
    lines: list[str] = []
    kind = str(entry.get("kind", "unknown"))
    category = str(entry.get("category", "unresolved"))
    reason = str(entry.get("reason", "unresolved"))
    lines.append(f"- Type: `{kind}`")
    lines.append(f"- Category: `{category}`")
    lines.append(f"- Reason: `{reason}`")
    if entry.get("source_text"):
        lines.append(f"- Source text: `{entry['source_text']}`")
    if entry.get("target_title"):
        lines.append(f"- Target title: `{entry['target_title']}`")
    if entry.get("target_id"):
        lines.append(f"- Target page id: `{entry['target_id']}`")
    if entry.get("target_space"):
        lines.append(f"- Target space: `{entry['target_space']}`")
    if entry.get("attachment_filename"):
        lines.append(f"- Attachment: `{entry['attachment_filename']}`")
    if entry.get("fallback_url"):
        lines.append(f"- Fallback URL: `{entry['fallback_url']}`")
    if entry.get("detail") and not any(key in entry for key in ("target_title", "target_id", "attachment_filename", "fallback_url")):
        lines.append(f"- Detail: `{entry['detail']}`")
    return lines


def grouped_link_diagnostics(report: dict) -> dict[str, list[dict]]:
    groups = {
        "not-exported": [],
        "out-of-scope": [],
        "cross-space": [],
        "external": [],
        "unresolved": [],
    }
    for entry in report.get("unresolved_links", []):
        category = str(entry.get("category", "unresolved"))
        if category == "unresolved" and entry.get("kind") == "page":
            category = "not-exported"
        groups.setdefault(category, []).append(entry)
    return groups


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
        f"- Run notes: {len(report['warnings'])}",
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
    lines.extend(["", "## Conflicts", "", *report_bullet_lines(report["conflicts"]), "", "## Run Notes", "", *report_bullet_lines(report["warnings"]), ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_confluence_to_repo_report(report_path: Path, report: dict) -> None:
    written_pages = [report_display_path(path, report) for path in report["written_pages"]]
    metadata_files = [report_display_path(path, report) for path in report.get("metadata_files_written", [])]
    skipped_pages = [report_display_path(path, report) if path.startswith("/") else path for path in report.get("skipped_pages", [])]
    link_groups = grouped_link_diagnostics(report)
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
        f"- Force refresh: {report.get('force', False)}",
        f"- Excluded page ids: {len(report.get('excluded_pages', []))}",
        f"- Pages skipped from cache: {len(report.get('skipped_pages', []))}",
        f"- Pages written: {len(report['written_pages'])}",
        f"- Metadata files written: {len(report.get('metadata_files_written', []))}",
        f"- Attachments downloaded: {len(report['downloaded_attachments'])}",
        f"- Attachment cache hits: {len(report.get('attachment_cache_hits', []))}",
        f"- Analytics cache hits: {len(report.get('analytics_cache_hits', []))}",
        f"- Link diagnostics: {len(report['unresolved_links'])}",
        f"- Links to content not exported: {len(link_groups.get('not-exported', []))}",
        f"- Out-of-scope internal links: {len(link_groups.get('out-of-scope', []))}",
        f"- Cross-space links: {len(link_groups.get('cross-space', []))}",
        f"- External links: {len(link_groups.get('external', []))}",
        f"- Truly unresolved links: {len(link_groups.get('unresolved', []))}",
        f"- Unsupported content items: {len(report.get('unsupported_content', []))}",
        f"- Run notes: {len(report['warnings'])}",
        "",
        "## Excluded Page Ids",
        "",
        *report_bullet_lines(report.get("excluded_pages", [])),
        "",
        "## Skipped Pages",
        "",
        *report_bullet_lines(skipped_pages),
        "",
        "## Written Pages",
        "",
    ]
    if written_pages:
        downloads_by_page = dict(grouped_downloaded_attachments(report))
        for page_path in written_pages:
            lines.append(f"- `{page_path}`")
            for filename in downloads_by_page.get(page_path, []):
                lines.append(f"  - asset: `{filename}`")
    else:
        lines.append("- None")
    lines.extend([
        "",
        "## Metadata Files",
        "",
        *report_bullet_lines(metadata_files),
    ])
    lines.extend([
        "",
        "## Attachment Cache Hits",
        "",
        *report_bullet_lines(report.get("attachment_cache_hits", [])),
        "",
        "## Analytics Cache Hits",
        "",
        *report_bullet_lines(report.get("analytics_cache_hits", [])),
        "",
        "## Unsupported Content",
        "",
    ])
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
        "## Link Diagnostics",
        "",
    ])
    section_titles = [
        ("not-exported", "Links to Content Not Exported"),
        ("out-of-scope", "Out-of-Scope Internal Links"),
        ("cross-space", "Cross-Space Links"),
        ("external", "External Links"),
        ("unresolved", "Truly Unresolved Links"),
    ]
    has_links = False
    for category, heading in section_titles:
        entries = link_groups.get(category, [])
        lines.append(f"### {heading}")
        lines.append("")
        if not entries:
            lines.append("- None")
            lines.append("")
            continue
        has_links = True
        for entry in entries:
            lines.append(f"#### {entry['page']}")
            lines.append("")
            lines.extend(format_unresolved_link_entry(entry))
            lines.append("")
    if not has_links and not any(link_groups.values()):
        lines.append("- None")
        lines.append("")
    lines.extend(["## Run Notes", "", *report_bullet_lines(report["warnings"]), ""])
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
        f"- Run notes: {len(report['warnings'])}",
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

    lines.extend(["", "## Run Notes", "", *report_bullet_lines(report["warnings"]), ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_analysis_report(report_path: Path, report: dict) -> None:
    action_counts = report.get("action_counts", {})
    action_lines = [f"- `{action}`: {count}" for action, count in sorted(action_counts.items())] or ["- None"]
    lines = [
        "# Analysis Report",
        "",
        "## Summary",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Report file: `{report_path}`",
        f"- Project file: `{report['project']}`",
        f"- Project name: `{report['project_name'] or 'unnamed'}`",
        f"- Phase: `{report['phase']}`",
        f"- Corpus: `{report['corpus']}`",
        f"- Output: `{report['output']}`",
        f"- Force refresh: {report.get('force', False)}",
        f"- Analysis outputs: `{', '.join(report.get('outputs', ['file']))}`",
        f"- Pages scored: {report.get('page_count', 0)}",
        f"- Score cache hits: {len(report.get('score_cache_hits', []))}",
        f"- Analysis metadata: `{report.get('analysis_metadata', '') or 'not written'}`",
        f"- Scores CSV: `{report.get('scores_csv', '') or 'not written'}`",
        f"- Sidecars written: {len(report.get('sidecars_written', []))}",
        f"- Content blocks updated: {len(report.get('content_blocks_updated', []))}",
        f"- Run notes: {len(report.get('warnings', []))}",
        "",
        "## Recommended Actions",
        "",
        *action_lines,
        "",
        "## Score Cache Hits",
        "",
        *report_bullet_lines(report.get("score_cache_hits", [])),
        "",
        "## Sidecars Written",
        "",
        *report_bullet_lines(report.get("sidecars_written", [])),
        "",
        "## Content Blocks Updated",
        "",
        *report_bullet_lines(report.get("content_blocks_updated", [])),
        "",
        "## Run Notes",
        "",
        *report_bullet_lines(report.get("warnings", [])),
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_triage_report(report_path: Path, report: dict) -> None:
    action_counts = report.get("suggested_action_counts", {})
    action_lines = [f"- `{action}`: {count}" for action, count in sorted(action_counts.items())] or ["- None"]
    lines = [
        "# Triage Report",
        "",
        "## Summary",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Report file: `{report_path}`",
        f"- Project file: `{report['project']}`",
        f"- Project name: `{report['project_name'] or 'unnamed'}`",
        f"- Phase: `{report['phase']}`",
        f"- Input: `{report['input']}`",
        f"- Output: `{report['output']}`",
        f"- Force refresh: {report.get('force', False)}",
        f"- Rows written: {report.get('row_count', 0)}",
        f"- Manifest JSON: `{report.get('manifest_json', '') or 'not written'}`",
        f"- Manifest CSV: `{report.get('manifest_csv', '') or 'not written'}`",
        f"- Manifest Markdown: `{report.get('manifest_markdown', '') or 'not written'}`",
        f"- Run notes: {len(report.get('warnings', []))}",
        "",
        "## Suggested Actions",
        "",
        *action_lines,
        "",
        "## Run Notes",
        "",
        *report_bullet_lines(report.get("warnings", [])),
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
