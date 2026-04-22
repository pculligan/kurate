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
SUPPORTED_ANALYSIS_OUTPUTS = {"none", "sidecar", "file", "content-block"}
PHASE_EXTRACTION = "extraction"
KNOWN_PHASES = {
    PHASE_EXTRACTION,
    "analysis",
    "triage",
    "refactoring",
    "warehousing",
}
SUPPORTED_EXTRACTION_PROVIDERS = {"confluence"}


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


def _require_mapping(value: Any, field_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Field '{field_name}' must be a mapping")
    return value


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


def _resolve_path(value: str, project_path: Path, field_name: str, workspace_root: Path | None = None) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        base = workspace_root if workspace_root is not None else PROJECT_ROOT
        candidate = (base / candidate).resolve()
    return str(candidate)


def load_project_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Project file does not exist: {path}")

    payload = _load_yaml_mapping(path, "project file")
    _reject_unknown_keys(payload, {"name", "workspace", "phases"}, "Project file")

    if "phases" not in payload:
        raise ValueError("Project file is missing required field: phases")

    phases_payload = _require_mapping(payload["phases"], "phases")
    unknown_phases = sorted(set(phases_payload) - KNOWN_PHASES)
    if unknown_phases:
        names = ", ".join(sorted(repr(name) for name in unknown_phases))
        raise ValueError(f"Field 'phases' has unexpected entries: {names}")

    normalized: Dict[str, Any] = {
        "project_path": str(path),
        "name": None,
        "workspace": None,
        "phases": {},
    }

    if "name" in payload and payload["name"] is not None:
        normalized["name"] = _require_string(payload["name"], "name")
    workspace_root: Path | None = None
    if "workspace" in payload and payload["workspace"] is not None:
        workspace = _require_string(payload["workspace"], "workspace")
        normalized["workspace"] = _resolve_path(workspace, path, "workspace")
        workspace_root = Path(normalized["workspace"])

    for phase_name, phase_value in phases_payload.items():
        phase_mapping = _require_mapping(phase_value, f"phases.{phase_name}")
        if phase_name == PHASE_EXTRACTION:
            normalized["phases"][phase_name] = _normalize_extraction_phase(phase_mapping, path, workspace_root)
        elif phase_name == "analysis":
            normalized["phases"][phase_name] = _normalize_analysis_phase(phase_mapping, path, workspace_root)
        elif phase_name == "triage":
            normalized["phases"][phase_name] = _normalize_triage_phase(phase_mapping, path, workspace_root)
        else:
            normalized["phases"][phase_name] = phase_mapping

    return normalized


def load_phase_config(path: Path, phase_name: str) -> Dict[str, Any]:
    project = load_project_config(path)
    phases = project.get("phases", {})
    if phase_name not in phases:
        raise ValueError(f"Project file does not define phase: {phase_name}")

    phase_config = dict(phases[phase_name])
    phase_config["project_path"] = project["project_path"]
    phase_config["name"] = project.get("name")
    phase_config["workspace"] = project.get("workspace")
    phase_config["phase"] = phase_name
    return phase_config


def _normalize_extraction_phase(payload: Dict[str, Any], path: Path, workspace_root: Path | None) -> Dict[str, Any]:
    _reject_unknown_keys(
        payload,
        {"provider", "activity", "source", "space", "parent", "excludes", "dry_run", "spaces", "metadata"},
        "Extraction phase",
    )

    provider = _require_string(payload.get("provider"), "phases.extraction.provider")
    if provider not in SUPPORTED_EXTRACTION_PROVIDERS:
        raise ValueError(
            "Field 'phases.extraction.provider' must be one of: "
            f"{', '.join(sorted(SUPPORTED_EXTRACTION_PROVIDERS))}"
        )

    activity = _require_string(payload.get("activity"), "phases.extraction.activity")
    if activity not in SUPPORTED_ACTIVITIES:
        raise ValueError(
            "Field 'phases.extraction.activity' must be one of: "
            f"{', '.join(sorted(SUPPORTED_ACTIVITIES))}"
        )

    normalized: Dict[str, Any] = {
        "provider": provider,
        "activity": activity,
    }
    if activity == PROJECT_ACTIVITY_PUBLISH:
        normalized.update(_normalize_repo_to_confluence_project(payload, path, workspace_root))
    else:
        normalized.update(_normalize_confluence_to_repo_project(payload, path, workspace_root))
    return normalized


def _normalize_repo_to_confluence_project(payload: Dict[str, Any], path: Path, workspace_root: Path | None) -> Dict[str, Any]:
    allowed = {"provider", "activity", "source", "space", "parent", "excludes", "dry_run"}
    _reject_unknown_keys(payload, allowed, "Extraction publish phase")

    missing = [key for key in ("source", "space", "parent") if key not in payload]
    if missing:
        raise ValueError(
            f"Extraction publish phase is missing required fields: {', '.join(sorted(missing))}"
        )

    source = _require_string(payload["source"], "phases.extraction.source")
    space = _require_string(payload["space"], "phases.extraction.space")
    parent = _require_string_or_int(payload["parent"], "phases.extraction.parent")

    normalized: Dict[str, Any] = {
        "source": _resolve_path(source, path, "phases.extraction.source", workspace_root),
        "space": space,
        "parent": parent,
        "excludes": [],
        "dry_run": False,
    }

    if "excludes" in payload:
        if not isinstance(payload["excludes"], list):
            raise ValueError("Field 'phases.extraction.excludes' must be a list")
        normalized["excludes"] = [
            _require_string(value, f"phases.extraction.excludes[{index}]")
            for index, value in enumerate(payload["excludes"], start=1)
        ]
    if "dry_run" in payload:
        normalized["dry_run"] = _require_bool(payload["dry_run"], "phases.extraction.dry_run")

    return normalized


def _normalize_confluence_to_repo_project(payload: Dict[str, Any], path: Path, workspace_root: Path | None) -> Dict[str, Any]:
    allowed = {"provider", "activity", "spaces", "metadata"}
    _reject_unknown_keys(payload, allowed, "Extraction export phase")

    if "spaces" not in payload:
        raise ValueError("Extraction export phase is missing required field: spaces")
    spaces = payload["spaces"]
    if not isinstance(spaces, dict) or not spaces:
        raise ValueError("Field 'phases.extraction.spaces' must be a non-empty mapping")

    metadata_modes: List[str] = ["none"]
    if "metadata" in payload:
        metadata_value = payload["metadata"]
        if isinstance(metadata_value, list):
            metadata_modes = [
                _require_string(value, f"phases.extraction.metadata[{index}]")
                for index, value in enumerate(metadata_value, start=1)
            ]
        else:
            metadata_modes = [_require_string(metadata_value, "phases.extraction.metadata")]
        invalid = [mode for mode in metadata_modes if mode not in SUPPORTED_METADATA_MODES]
        if invalid:
            raise ValueError(
                "Field 'phases.extraction.metadata' must contain only: "
                f"{', '.join(sorted(SUPPORTED_METADATA_MODES))}"
            )
        if "none" in metadata_modes and len(metadata_modes) > 1:
            raise ValueError("Field 'phases.extraction.metadata' may not combine 'none' with other values")
        metadata_modes = list(dict.fromkeys(metadata_modes))

    normalized_spaces: List[Dict[str, Any]] = []
    for space_key, space_value in spaces.items():
        if not isinstance(space_key, str) or not space_key.strip():
            raise ValueError("Each key under 'phases.extraction.spaces' must be a non-empty string")
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
            _reject_unknown_keys(
                page,
                {"id", "output", "recurse", "excludes"},
                f"Space '{space_key}' page entry #{index}",
            )
            missing = [key for key in ("id", "output") if key not in page]
            if missing:
                raise ValueError(
                    f"Space '{space_key}' page entry #{index} is missing required fields: {', '.join(sorted(missing))}"
                )
            page_id = _require_string_or_int(page["id"], f"phases.extraction.spaces.{space_key}.pages[{index}].id")
            output = _require_string(page["output"], f"phases.extraction.spaces.{space_key}.pages[{index}].output")
            recurse = False
            excludes: List[str] = []
            if "recurse" in page:
                recurse = _require_bool(
                    page["recurse"],
                    f"phases.extraction.spaces.{space_key}.pages[{index}].recurse",
                )
            if "excludes" in page:
                if not isinstance(page["excludes"], list):
                    raise ValueError(
                        f"Field 'phases.extraction.spaces.{space_key}.pages[{index}].excludes' must be a list"
                    )
                excludes = [
                    _require_string_or_int(
                        value,
                        f"phases.extraction.spaces.{space_key}.pages[{index}].excludes[{exclude_index}]",
                    )
                    for exclude_index, value in enumerate(page["excludes"], start=1)
                ]
            normalized_pages.append(
                {
                    "id": page_id,
                    "output": _resolve_path(
                        output,
                        path,
                        f"phases.extraction.spaces.{space_key}.pages[{index}].output",
                        workspace_root,
                    ),
                    "recurse": recurse,
                    "excludes": excludes,
                }
            )
        normalized_spaces.append({"space": space_key.strip(), "pages": normalized_pages})

    return {"spaces": normalized_spaces, "metadata": metadata_modes}


def _normalize_analysis_phase(payload: Dict[str, Any], path: Path, workspace_root: Path | None) -> Dict[str, Any]:
    _reject_unknown_keys(payload, {"scoring"}, "Analysis phase")
    if "scoring" not in payload:
        raise ValueError("Analysis phase is missing required field: scoring")

    scoring = _require_mapping(payload["scoring"], "phases.analysis.scoring")
    _reject_unknown_keys(scoring, {"corpus", "output", "outputs"}, "Analysis scoring block")
    if workspace_root is None and ("corpus" not in scoring or "output" not in scoring):
        raise ValueError(
            "Analysis scoring block must define 'corpus' and 'output' unless the project defines a top-level 'workspace'"
        )

    corpus = _require_string(scoring["corpus"], "phases.analysis.scoring.corpus") if "corpus" in scoring else "."
    output = _require_string(scoring["output"], "phases.analysis.scoring.output") if "output" in scoring else "."
    outputs: List[str] = ["file"]
    if "outputs" in scoring:
        outputs_value = scoring["outputs"]
        if isinstance(outputs_value, list):
            outputs = [
                _require_string(value, f"phases.analysis.scoring.outputs[{index}]")
                for index, value in enumerate(outputs_value, start=1)
            ]
        else:
            outputs = [_require_string(outputs_value, "phases.analysis.scoring.outputs")]
        invalid = [mode for mode in outputs if mode not in SUPPORTED_ANALYSIS_OUTPUTS]
        if invalid:
            raise ValueError(
                "Field 'phases.analysis.scoring.outputs' must contain only: "
                f"{', '.join(sorted(SUPPORTED_ANALYSIS_OUTPUTS))}"
            )
        if "none" in outputs and len(outputs) > 1:
            raise ValueError("Field 'phases.analysis.scoring.outputs' may not combine 'none' with other values")
        outputs = list(dict.fromkeys(outputs))

    return {
        "scoring": {
            "corpus": _resolve_path(corpus, path, "phases.analysis.scoring.corpus", workspace_root),
            "output": _resolve_path(output, path, "phases.analysis.scoring.output", workspace_root),
            "outputs": outputs,
        }
    }


def _normalize_triage_phase(payload: Dict[str, Any], path: Path, workspace_root: Path | None) -> Dict[str, Any]:
    _reject_unknown_keys(payload, {"manifest"}, "Triage phase")
    if "manifest" not in payload:
        raise ValueError("Triage phase is missing required field: manifest")

    manifest = _require_mapping(payload["manifest"], "phases.triage.manifest")
    _reject_unknown_keys(manifest, {"input", "output"}, "Triage manifest block")

    if workspace_root is None and ("input" not in manifest or "output" not in manifest):
        raise ValueError(
            "Triage manifest block must define 'input' and 'output' unless the project defines a top-level 'workspace'"
        )

    input_path = _require_string(manifest["input"], "phases.triage.manifest.input") if "input" in manifest else "."
    output_path = _require_string(manifest["output"], "phases.triage.manifest.output") if "output" in manifest else "."
    return {
        "manifest": {
            "input": _resolve_path(input_path, path, "phases.triage.manifest.input", workspace_root),
            "output": _resolve_path(output_path, path, "phases.triage.manifest.output", workspace_root),
        }
    }
