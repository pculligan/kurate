#!/usr/bin/env python3
"""Top-level entry point for the kurate suite."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from shared.project_config import (
    PHASE_EXTRACTION,
    load_phase_config,
    PROJECT_ACTIVITY_EXPORT,
    PROJECT_ACTIVITY_PUBLISH,
)
from phases.analysis.runner import run_analysis
from phases.extraction.providers.confluence.auth import missing_dependency_message
from phases.extraction.providers.confluence.conf_io import export_from_confluence, sync_to_confluence
from phases.extraction.providers.confluence.deps import MISSING_DEPENDENCY_ERROR
from phases.triage.runner import run_triage
import phases.extraction.providers.confluence.confluence_to_repo_helpers as from_helpers
import phases.extraction.providers.confluence.repo_to_confluence_helpers as to_helpers

LOG = logging.getLogger("kurate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kurate",
        description="Run kurate suite workflows from a project YAML file",
    )
    parser.add_argument("--project", required=True, help="Path to a project YAML file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--force", action="store_true", help="Bypass cache hints and refresh work for this phase")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser(
        "extract",
        help="Run an extraction-stage project",
        description="Run an extraction-stage project through the configured provider",
    )
    extract.add_argument("--base-url", default=None, help="Optional override for the provider base URL")
    extract.add_argument("--email", default=None, help="Optional override for the provider auth email")
    extract.add_argument("--identity-config", default=None, help="Path to a YAML identity config file")

    subparsers.add_parser(
        "analyze",
        help="Run an analysis-stage project",
        description="Run an analysis-stage project",
    )
    subparsers.add_parser(
        "triage",
        help="Run a triage-stage project",
        description="Run a triage-stage project",
    )

    return parser


def run_extract(args: argparse.Namespace) -> int:
    if MISSING_DEPENDENCY_ERROR is not None:
        raise SystemExit(missing_dependency_message(MISSING_DEPENDENCY_ERROR))

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    try:
        project = load_phase_config(Path(args.project).expanduser().resolve(), PHASE_EXTRACTION)
    except ValueError as exc:
        LOG.error("Could not load project config: %s", exc)
        return 2

    if project.get("provider") != "confluence":
        LOG.error("Unsupported extraction provider: %s", project.get("provider"))
        return 2
    project["force"] = bool(args.force)

    identity_overrides = {
        "identity_config": args.identity_config,
        "base_url": args.base_url,
        "email": args.email,
    }

    if project["activity"] == PROJECT_ACTIVITY_PUBLISH:
        return sync_to_confluence(project, identity_overrides, helpers=to_helpers)
    if project["activity"] == PROJECT_ACTIVITY_EXPORT:
        return export_from_confluence(project, identity_overrides, helpers=from_helpers)

    LOG.error("Unsupported extraction activity: %s", project["activity"])
    return 2


def run_analyze(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    try:
        project = load_phase_config(Path(args.project).expanduser().resolve(), "analysis")
    except ValueError as exc:
        LOG.error("Could not load project config: %s", exc)
        return 2
    project["force"] = bool(args.force)
    return run_analysis(project)


def run_triage_phase(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    try:
        project = load_phase_config(Path(args.project).expanduser().resolve(), "triage")
    except ValueError as exc:
        LOG.error("Could not load project config: %s", exc)
        return 2
    project["force"] = bool(args.force)
    return run_triage(project)


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv or sys.argv[1:])

    if args.command == "extract":
        code = run_extract(args)
    elif args.command == "analyze":
        code = run_analyze(args)
    elif args.command == "triage":
        code = run_triage_phase(args)
    else:  # pragma: no cover - argparse constrains this
        parser.error(f"Unsupported command: {args.command}")
        return

    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
