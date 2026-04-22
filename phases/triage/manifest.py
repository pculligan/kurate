from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class TriageRow:
    title: str
    markdown_relative_path: str
    confluence_page_id: str
    suggested_action: str
    final_action: str
    confidence: str
    rationale: str
    related_group_id: str
    canonical_candidate: str
    notes: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "markdown_relative_path": self.markdown_relative_path,
            "confluence_page_id": self.confluence_page_id,
            "suggested_action": self.suggested_action,
            "final_action": self.final_action,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "related_group_id": self.related_group_id,
            "canonical_candidate": self.canonical_candidate,
            "notes": self.notes,
        }


def load_analysis_rows(input_dir: Path) -> List[Dict[str, Any]]:
    manifest_path = input_dir / "analysis.metadata.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        pages = payload.get("pages", [])
        if not isinstance(pages, list):
            raise ValueError(f"Analysis manifest has invalid 'pages' structure: {manifest_path}")
        return [page for page in pages if isinstance(page, dict)]

    rows: List[Dict[str, Any]] = []
    for sidecar_path in sorted(input_dir.rglob("*.analysis.json")):
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def triage_row_from_analysis(entry: Dict[str, Any]) -> TriageRow:
    reasons = entry.get("reasons", [])
    if isinstance(reasons, list):
        rationale = "; ".join(str(reason) for reason in reasons if str(reason).strip())
    else:
        rationale = str(reasons or "")
    return TriageRow(
        title=str(entry.get("title", "Untitled")),
        markdown_relative_path=str(entry.get("markdown_relative_path", "")),
        confluence_page_id=str(entry.get("confluence_page_id", "")),
        suggested_action=str(entry.get("recommended_action", "manual_review")),
        final_action="",
        confidence="",
        rationale=rationale,
        related_group_id="",
        canonical_candidate="",
        notes="",
    )


def write_manifest_json(path: Path, rows: List[TriageRow]) -> None:
    payload = {"rows": [row.as_dict() for row in rows]}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_manifest_csv(path: Path, rows: List[TriageRow]) -> None:
    fieldnames = [
        "title",
        "markdown_relative_path",
        "confluence_page_id",
        "suggested_action",
        "final_action",
        "confidence",
        "rationale",
        "related_group_id",
        "canonical_candidate",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())


def write_manifest_markdown(path: Path, rows: List[TriageRow]) -> None:
    lines = [
        "# Triage Manifest",
        "",
        "| Suggested | Final | Title | Path | Rationale |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        rationale = row.rationale.replace("|", "\\|")
        lines.append(
            f"| {row.suggested_action} | {row.final_action or ''} | {row.title.replace('|', '\\|')} | "
            f"`{row.markdown_relative_path}` | {rationale} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
