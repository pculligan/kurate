from __future__ import annotations

import csv
import json
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class PageScore:
    title: str
    markdown_relative_path: str
    confluence_page_id: str
    staleness_score: int
    usefulness_score: int
    archive_score: int
    recommended_action: str
    reasons: List[str]
    source_fingerprint: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "markdown_relative_path": self.markdown_relative_path,
            "confluence_page_id": self.confluence_page_id,
            "staleness_score": self.staleness_score,
            "usefulness_score": self.usefulness_score,
            "archive_score": self.archive_score,
            "recommended_action": self.recommended_action,
            "reasons": self.reasons,
            "source_fingerprint": self.source_fingerprint,
        }


def analysis_sidecar_path(markdown_path: Path) -> Path:
    return markdown_path.with_name(f"{markdown_path.stem}.analysis.json")


def analysis_signal_lines(score: PageScore) -> List[str]:
    return [
        "---",
        "",
        "## Analysis Assessment",
        "",
        "Machine-generated analysis signals. This section was added by kurate during analysis and is not part of the original authored content.",
        "",
        f"- Recommended action: {score.recommended_action}",
        f"- Staleness score: {score.staleness_score}",
        f"- Usefulness score: {score.usefulness_score}",
        f"- Archive score: {score.archive_score}",
        f"- Reasons: {'; '.join(score.reasons) if score.reasons else 'none'}",
        "",
    ]


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


def load_metadata_pages(corpus_root: Path) -> List[Dict[str, Any]]:
    manifest_path = corpus_root / "export.metadata.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        pages = payload.get("pages", [])
        if not isinstance(pages, list):
            raise ValueError(f"Metadata manifest has invalid 'pages' structure: {manifest_path}")
        return [page for page in pages if isinstance(page, dict)]

    pages: List[Dict[str, Any]] = []
    for sidecar_path in sorted(corpus_root.rglob("*.metadata.json")):
        if sidecar_path.name == "export.metadata.json":
            continue
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        if "markdown_relative_path" not in payload:
            try:
                markdown_relative = sidecar_path.relative_to(corpus_root).as_posix().removesuffix(".metadata.json") + ".md"
            except ValueError:
                markdown_relative = sidecar_path.with_suffix("").with_suffix(".md").name
            payload["markdown_relative_path"] = markdown_relative
        pages.append(payload)
    return pages


def load_existing_analysis_index(corpus_root: Path, output_dir: Path) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    manifest_path = output_dir / "analysis.metadata.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in payload.get("pages", []) if isinstance(payload, dict) else []:
            if isinstance(entry, dict) and entry.get("confluence_page_id") is not None:
                index[str(entry["confluence_page_id"])] = entry
    for sidecar_path in sorted(corpus_root.rglob("*.analysis.json")):
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("confluence_page_id") is not None:
            index[str(payload["confluence_page_id"])] = payload
    return index


def source_fingerprint(page: Dict[str, Any]) -> str:
    normalized = json.dumps(page, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _age_days(updated_at: Any, today: date) -> Optional[int]:
    if not updated_at:
        return None
    try:
        return (today - date.fromisoformat(str(updated_at)[:10])).days
    except ValueError:
        return None


def score_page(page: Dict[str, Any], today: Optional[date] = None) -> PageScore:
    today = today or date.today()
    reasons: List[str] = []

    analytics = page.get("analytics", {}) if isinstance(page.get("analytics"), dict) else {}
    year_to_date = analytics.get("year_to_date", {}) if isinstance(analytics.get("year_to_date"), dict) else {}
    trailing_year = analytics.get("trailing_year", {}) if isinstance(analytics.get("trailing_year"), dict) else {}
    all_time_proxy = analytics.get("all_time_proxy", {}) if isinstance(analytics.get("all_time_proxy"), dict) else {}

    views_ytd = _safe_int(year_to_date.get("views")) or 0
    unique_ytd = _safe_int(year_to_date.get("unique_viewers")) or 0
    views_trailing = _safe_int(trailing_year.get("views")) or 0
    unique_trailing = _safe_int(trailing_year.get("unique_viewers")) or 0
    views_long = _safe_int(all_time_proxy.get("views")) or 0
    unique_long = _safe_int(all_time_proxy.get("unique_viewers")) or 0

    attachments = page.get("attachments", [])
    attachment_count = len(attachments) if isinstance(attachments, list) else 0
    unsupported_count = _safe_int(page.get("unsupported_content_count")) or 0
    unresolved_count = _safe_int(page.get("unresolved_link_count")) or 0
    version = _safe_int(page.get("version")) or 0
    age_days = _age_days(page.get("updated_at"), today)

    staleness = 0
    if age_days is None:
        staleness += 35
        reasons.append("missing last-updated timestamp")
    elif age_days > 1095:
        staleness += 80
        reasons.append("not updated in more than 3 years")
    elif age_days > 730:
        staleness += 65
        reasons.append("not updated in more than 2 years")
    elif age_days > 365:
        staleness += 45
        reasons.append("not updated in more than 1 year")
    elif age_days > 180:
        staleness += 25

    usefulness = 0
    usefulness += min(views_ytd * 2, 20)
    usefulness += min(unique_ytd * 3, 24)
    usefulness += min(views_trailing // 5, 20)
    usefulness += min(unique_trailing * 2, 20)
    usefulness += min(version * 2, 10)
    usefulness += min(attachment_count * 2, 6)
    usefulness += min(unique_long // 2, 10)
    usefulness = min(usefulness, 100)

    if unique_trailing >= 10:
        reasons.append("has meaningful trailing-year readership")
    elif unique_trailing == 0 and views_trailing == 0:
        reasons.append("shows no trailing-year readership")

    if version >= 5:
        reasons.append("has been revised multiple times")

    archive = 0
    archive += int(staleness * 0.7)
    archive += max(0, 40 - usefulness // 2)
    archive += min(unsupported_count * 8, 24)
    archive += min(unresolved_count * 6, 18)
    archive = max(0, min(archive, 100))

    if unsupported_count:
        reasons.append("contains unsupported embedded content")
    if unresolved_count:
        reasons.append("contains unresolved links")

    if archive >= 75 and usefulness < 35:
        action = "archive"
    elif unsupported_count or unresolved_count:
        action = "manual_review"
    elif usefulness >= 60 and staleness <= 35:
        action = "keep"
    elif usefulness >= 40:
        action = "curate"
    else:
        action = "manual_review"

    if action == "archive" and views_long > 50:
        action = "manual_review"
        reasons.append("historically used despite archive-like signals")

    return PageScore(
        title=str(page.get("title", "Untitled")),
        markdown_relative_path=str(page.get("markdown_relative_path", page.get("markdown_path", ""))),
        confluence_page_id=str(page.get("confluence_page_id", "")),
        staleness_score=staleness,
        usefulness_score=usefulness,
        archive_score=archive,
        recommended_action=action,
        reasons=list(dict.fromkeys(reasons)),
        source_fingerprint=source_fingerprint(page),
    )


def score_pages(pages: Iterable[Dict[str, Any]], today: Optional[date] = None) -> List[PageScore]:
    today = today or date.today()
    return [score_page(page, today=today) for page in pages]


def page_score_from_dict(payload: Dict[str, Any]) -> PageScore:
    return PageScore(
        title=str(payload.get("title", "Untitled")),
        markdown_relative_path=str(payload.get("markdown_relative_path", "")),
        confluence_page_id=str(payload.get("confluence_page_id", "")),
        staleness_score=int(payload.get("staleness_score", 0)),
        usefulness_score=int(payload.get("usefulness_score", 0)),
        archive_score=int(payload.get("archive_score", 0)),
        recommended_action=str(payload.get("recommended_action", "manual_review")),
        reasons=[str(reason) for reason in payload.get("reasons", []) if str(reason).strip()],
        source_fingerprint=str(payload.get("source_fingerprint", "")),
    )


def write_scores_json(path: Path, scores: List[PageScore], *, generated_at: Optional[str] = None, corpus: Optional[str] = None) -> None:
    payload = {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus": corpus,
        "pages": [score.as_dict() for score in scores],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_scores_csv(path: Path, scores: List[PageScore]) -> None:
    fieldnames = [
        "title",
        "markdown_relative_path",
        "confluence_page_id",
        "staleness_score",
        "usefulness_score",
        "archive_score",
        "recommended_action",
        "reasons",
        "source_fingerprint",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for score in scores:
            row = score.as_dict()
            row["reasons"] = "; ".join(score.reasons)
            writer.writerow(row)


def write_scores_sidecars(corpus_root: Path, scores: List[PageScore]) -> List[str]:
    written: List[str] = []
    for score in scores:
        markdown_path = corpus_root / score.markdown_relative_path
        sidecar_path = analysis_sidecar_path(markdown_path)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(score.as_dict(), indent=2, sort_keys=True) + "\n"
        if sidecar_path.exists() and sidecar_path.read_text(encoding="utf-8") == payload:
            continue
        sidecar_path.write_text(payload, encoding="utf-8")
        written.append(str(sidecar_path))
    return written


def write_scores_content_blocks(corpus_root: Path, scores: List[PageScore]) -> List[str]:
    updated: List[str] = []
    for score in scores:
        markdown_path = corpus_root / score.markdown_relative_path
        if not markdown_path.exists():
            continue
        existing = markdown_path.read_text(encoding="utf-8")
        base = strip_generated_analysis_block(existing).rstrip()
        updated_text = base + "\n\n" + "\n".join(analysis_signal_lines(score))
        updated_text = updated_text.rstrip() + "\n"
        if existing == updated_text:
            continue
        markdown_path.write_text(updated_text, encoding="utf-8")
        updated.append(str(markdown_path))
    return updated
