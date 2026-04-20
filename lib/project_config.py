from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .constants import PROJECT_ROOT
from .deps import yaml

PROJECT_ACTIVITY_PUBLISH = "publish"
PROJECT_ACTIVITY_EXPORT = "export"
SUPPORTED_ACTIVITIES = {
    PROJECT_ACTIVITY_PUBLISH,
    PROJECT_ACTIVITY_EXPORT,
}
SUPPORTED_METADATA_MODES = {"none", "sidecar", "file", "content-block"}


def _expect_yaml_available() -> None:
    if yaml is None:
        raise ModuleNotFoundError("yaml")


def _load_yaml_mapping(path: Path, label: str) -> Dict[str, Any]:
    _expect_yaml_available()
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - defensive parse guard
        raise ValueError(f"Could not parse {label} {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{label.capitalize()} {path} must contain a YAML mapping")
    return loaded


def _reject_unknown_keys(mapping: Dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        names = ", ".join(sorted(repr(key) for key in unknown))
        raise ValueError(f"{label} has unexpected fields: {names}")


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Field '{field_name}' must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Field '{field_name}' must not be empty")
    return stripped


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Field '{field_name}' must be a boolean")
    return value


def _require_string_or_int(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"Field '{field_name}' must be a string or integer")
    return str(value).strip()


def _resolve_path(value: str, project_path: Path, field_name: str) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (PROJECT_ROOT / candidate).resolve()
    return str(candidate)


def load_project_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Project file does not exist: {path}")

    payload = _load_yaml_mapping(path, "project file")
    _reject_unknown_keys(
        payload,
        {"name", "activity", "source", "space", "parent", "excludes", "dry_run", "spaces", "metadata"},
        "Project file",
    )

    activity = _require_string(payload.get("activity"), "activity")
    if activity not in SUPPORTED_ACTIVITIES:
        raise ValueError(
            "Field 'activity' must be one of: "
            f"{', '.join(sorted(SUPPORTED_ACTIVITIES))}"
        )

    normalized: Dict[str, Any] = {
        "project_path": str(path),
        "name": None,
        "activity": activity,
    }

    if "name" in payload and payload["name"] is not None:
        normalized["name"] = _require_string(payload["name"], "name")

    if activity == PROJECT_ACTIVITY_PUBLISH:
        normalized.update(_normalize_repo_to_confluence_project(payload, path))
    else:
        normalized.update(_normalize_confluence_to_repo_project(payload, path))

    return normalized


def _normalize_repo_to_confluence_project(payload: Dict[str, Any], path: Path) -> Dict[str, Any]:
    allowed = {"name", "activity", "source", "space", "parent", "excludes", "dry_run"}
    _reject_unknown_keys(payload, allowed, "Repo-to-Confluence project")

    missing = [key for key in ("source", "space", "parent") if key not in payload]
    if missing:
        raise ValueError(
            f"Repo-to-Confluence project is missing required fields: {', '.join(sorted(missing))}"
        )

    source = _require_string(payload["source"], "source")
    space = _require_string(payload["space"], "space")
    parent = _require_string_or_int(payload["parent"], "parent")

    normalized: Dict[str, Any] = {
        "source": _resolve_path(source, path, "source"),
        "space": space,
        "parent": parent,
        "excludes": [],
        "dry_run": False,
    }

    if "excludes" in payload:
        if not isinstance(payload["excludes"], list):
            raise ValueError("Field 'excludes' must be a list")
        normalized["excludes"] = [
            _require_string(value, f"excludes[{index}]")
            for index, value in enumerate(payload["excludes"], start=1)
        ]
    if "dry_run" in payload:
        normalized["dry_run"] = _require_bool(payload["dry_run"], "dry_run")

    return normalized


def _normalize_confluence_to_repo_project(payload: Dict[str, Any], path: Path) -> Dict[str, Any]:
    allowed = {"name", "activity", "spaces", "metadata"}
    _reject_unknown_keys(payload, allowed, "Confluence-to-repo project")

    if "spaces" not in payload:
        raise ValueError("Confluence-to-repo project is missing required field: spaces")
    spaces = payload["spaces"]
    if not isinstance(spaces, dict) or not spaces:
        raise ValueError("Field 'spaces' must be a non-empty mapping")

    metadata_modes: List[str] = ["none"]
    if "metadata" in payload:
        metadata_value = payload["metadata"]
        if isinstance(metadata_value, list):
            metadata_modes = [
                _require_string(value, f"metadata[{index}]")
                for index, value in enumerate(metadata_value, start=1)
            ]
        else:
            metadata_modes = [_require_string(metadata_value, "metadata")]
        invalid = [mode for mode in metadata_modes if mode not in SUPPORTED_METADATA_MODES]
        if invalid:
            raise ValueError(
                f"Field 'metadata' must contain only: {', '.join(sorted(SUPPORTED_METADATA_MODES))}"
            )
        if "none" in metadata_modes and len(metadata_modes) > 1:
            raise ValueError("Field 'metadata' may not combine 'none' with other values")
        metadata_modes = list(dict.fromkeys(metadata_modes))
    normalized_spaces: List[Dict[str, Any]] = []
    for space_key, space_value in spaces.items():
        if not isinstance(space_key, str) or not space_key.strip():
            raise ValueError("Each key under 'spaces' must be a non-empty string")
        if not isinstance(space_value, dict):
            raise ValueError(f"Space '{space_key}' must contain a mapping")
        _reject_unknown_keys(space_value, {"pages"}, f"Space '{space_key}'")
        pages = space_value.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError(f"Space '{space_key}' must define a non-empty 'pages' list")

        normalized_pages: List[Dict[str, Any]] = []
        for index, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                raise ValueError(f"Space '{space_key}' page entry #{index} must be a mapping")
            _reject_unknown_keys(page, {"id", "output", "recurse", "excludes"}, f"Space '{space_key}' page entry #{index}")
            missing = [key for key in ("id", "output") if key not in page]
            if missing:
                raise ValueError(
                    f"Space '{space_key}' page entry #{index} is missing required fields: {', '.join(sorted(missing))}"
                )
            page_id = _require_string_or_int(page["id"], f"spaces.{space_key}.pages[{index}].id")
            output = _require_string(page["output"], f"spaces.{space_key}.pages[{index}].output")
            recurse = False
            excludes: List[str] = []
            if "recurse" in page:
                recurse = _require_bool(page["recurse"], f"spaces.{space_key}.pages[{index}].recurse")
            if "excludes" in page:
                if not isinstance(page["excludes"], list):
                    raise ValueError(f"Field 'spaces.{space_key}.pages[{index}].excludes' must be a list")
                excludes = [
                    _require_string_or_int(value, f"spaces.{space_key}.pages[{index}].excludes[{exclude_index}]")
                    for exclude_index, value in enumerate(page["excludes"], start=1)
                ]
            normalized_pages.append(
                {
                    "id": page_id,
                    "output": _resolve_path(output, path, f"spaces.{space_key}.pages[{index}].output"),
                    "recurse": recurse,
                    "excludes": excludes,
                }
            )
        normalized_spaces.append({"space": space_key.strip(), "pages": normalized_pages})

    return {"spaces": normalized_spaces, "metadata": metadata_modes}
