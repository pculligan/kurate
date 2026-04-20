#!/usr/bin/env python3
"""Run a Confluence project YAML file."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from lib.auth import missing_dependency_message
from lib.conf_io import export_from_confluence, sync_to_confluence
from lib.deps import MISSING_DEPENDENCY_ERROR
from lib.project_config import (
    PROJECT_ACTIVITY_EXPORT,
    PROJECT_ACTIVITY_PUBLISH,
    load_project_config,
)
import lib.confluence_to_repo_helpers as from_helpers
import lib.repo_to_confluence_helpers as to_helpers

LOG = logging.getLogger("conf_io_runner")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Confluence project YAML file")
    parser.add_argument("--project", required=True, help="Path to a project YAML file")
    parser.add_argument("--base-url", default=None, help="Optional override for the Confluence base URL")
    parser.add_argument("--email", default=None, help="Optional override for the Confluence auth email")
    parser.add_argument("--identity-config", default=None, help="Path to a YAML identity config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv or sys.argv[1:])

    if MISSING_DEPENDENCY_ERROR is not None:
        raise SystemExit(missing_dependency_message(MISSING_DEPENDENCY_ERROR))

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    try:
        project = load_project_config(Path(args.project).expanduser().resolve())
    except ValueError as exc:
        LOG.error("Could not load project config: %s", exc)
        raise SystemExit(2)

    identity_overrides = {
        "identity_config": args.identity_config,
        "base_url": args.base_url,
        "email": args.email,
    }

    if project["activity"] == PROJECT_ACTIVITY_PUBLISH:
        code = sync_to_confluence(project, identity_overrides, helpers=to_helpers)
    elif project["activity"] == PROJECT_ACTIVITY_EXPORT:
        code = export_from_confluence(project, identity_overrides, helpers=from_helpers)
    else:  # pragma: no cover - load_project_config already validates this
        LOG.error("Unsupported project activity: %s", project["activity"])
        raise SystemExit(2)

    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
