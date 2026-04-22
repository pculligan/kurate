from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from shared.reports import project_report_path, write_triage_report

from .manifest import (
    load_analysis_rows,
    triage_row_from_analysis,
    write_manifest_csv,
    write_manifest_json,
    write_manifest_markdown,
)


def run_triage(project: Dict[str, Any]) -> int:
    manifest = project["manifest"]
    input_dir = Path(manifest["input"]).resolve()
    output_dir = Path(manifest["output"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = project_report_path("triage", project.get("name"), project.get("project_path"))
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": project["project_path"],
        "project_name": project.get("name"),
        "phase": "triage",
        "input": str(input_dir),
        "output": str(output_dir),
        "force": bool(project.get("force", False)),
        "manifest_json": "",
        "manifest_csv": "",
        "manifest_markdown": "",
        "row_count": 0,
        "suggested_action_counts": {},
        "warnings": [],
    }

    try:
        analysis_rows = load_analysis_rows(input_dir)
    except FileNotFoundError as exc:
        report["warnings"].append(str(exc))
        write_triage_report(report_path, report)
        return 2
    except ValueError as exc:
        report["warnings"].append(f"Could not load analysis data: {exc}")
        write_triage_report(report_path, report)
        return 2

    if not analysis_rows:
        report["warnings"].append(f"No analysis metadata files found under {input_dir}")
        write_triage_report(report_path, report)
        return 2

    rows = [triage_row_from_analysis(entry) for entry in analysis_rows]
    manifest_json = output_dir / "triage-manifest.json"
    manifest_csv = output_dir / "triage-manifest.csv"
    manifest_md = output_dir / "triage-manifest.md"
    write_manifest_json(manifest_json, rows)
    write_manifest_csv(manifest_csv, rows)
    write_manifest_markdown(manifest_md, rows)

    action_counts: Dict[str, int] = {}
    for row in rows:
        action_counts[row.suggested_action] = action_counts.get(row.suggested_action, 0) + 1

    report["manifest_json"] = str(manifest_json)
    report["manifest_csv"] = str(manifest_csv)
    report["manifest_markdown"] = str(manifest_md)
    report["row_count"] = len(rows)
    report["suggested_action_counts"] = action_counts
    write_triage_report(report_path, report)
    return 0
