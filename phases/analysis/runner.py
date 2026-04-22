from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from shared.reports import project_report_path, write_analysis_report

from .scoring import (
    load_existing_analysis_index,
    load_metadata_pages,
    page_score_from_dict,
    score_pages,
    score_page,
    source_fingerprint,
    write_scores_content_blocks,
    write_scores_csv,
    write_scores_json,
    write_scores_sidecars,
)


def run_analysis(project: Dict[str, Any]) -> int:
    scoring = project["scoring"]
    corpus_root = Path(scoring["corpus"]).resolve()
    output_dir = Path(scoring["output"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = project_report_path("analysis", project.get("name"), project.get("project_path"))
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": project["project_path"],
        "project_name": project.get("name"),
        "phase": "analysis",
        "corpus": str(corpus_root),
        "output": str(output_dir),
        "force": bool(project.get("force", False)),
        "analysis_metadata": "",
        "scores_csv": "",
        "outputs": list(scoring.get("outputs", ["file"])),
        "sidecars_written": [],
        "content_blocks_updated": [],
        "score_cache_hits": [],
        "page_count": 0,
        "action_counts": {},
        "warnings": [],
    }

    try:
        pages = load_metadata_pages(corpus_root)
    except FileNotFoundError as exc:
        report["warnings"].append(str(exc))
        write_analysis_report(report_path, report)
        return 2
    except ValueError as exc:
        report["warnings"].append(f"Could not load metadata: {exc}")
        write_analysis_report(report_path, report)
        return 2

    if not pages:
        report["warnings"].append(f"No export metadata files found under {corpus_root}")
        write_analysis_report(report_path, report)
        return 2

    existing_index = {} if project.get("force") else load_existing_analysis_index(corpus_root, output_dir)
    scores = []
    for page in pages:
        page_id = str(page.get("confluence_page_id", ""))
        fingerprint = source_fingerprint(page)
        cached = existing_index.get(page_id)
        if cached and str(cached.get("source_fingerprint", "")) == fingerprint:
            scores.append(page_score_from_dict(cached))
            report["score_cache_hits"].append(
                f"{cached.get('title', page.get('title', 'Untitled'))} ({page_id})"
            )
        else:
            scores.append(score_page(page))
    metadata_path = output_dir / "analysis.metadata.json"
    scores_csv_path = output_dir / "page-scores.csv"
    write_scores_csv(scores_csv_path, scores)
    outputs = set(scoring.get("outputs", ["file"]))
    if "file" in outputs:
        write_scores_json(metadata_path, scores, corpus=str(corpus_root))
        report["analysis_metadata"] = str(metadata_path)
    if "sidecar" in outputs:
        report["sidecars_written"] = write_scores_sidecars(corpus_root, scores)
    if "content-block" in outputs:
        report["content_blocks_updated"] = write_scores_content_blocks(corpus_root, scores)

    action_counts: Dict[str, int] = {}
    for score in scores:
        action_counts[score.recommended_action] = action_counts.get(score.recommended_action, 0) + 1

    report["scores_csv"] = str(scores_csv_path)
    report["page_count"] = len(scores)
    report["action_counts"] = action_counts
    write_analysis_report(report_path, report)
    return 0
