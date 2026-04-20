from __future__ import annotations

from typing import Dict

from .constants import DEFAULT_BASE_URL, IDENTITY_CONFIG_FILENAME, PROJECT_ROOT
from .deps import yaml


def identity_config_path() -> Path:
    return PROJECT_ROOT / IDENTITY_CONFIG_FILENAME


def load_identity_config(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    if yaml is None:
        raise ModuleNotFoundError("yaml")

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - defensive parse guard
        raise ValueError(f"Could not parse identity config {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"Identity config {path} must contain a YAML mapping")

    confluence = loaded.get("confluence", loaded)
    if not isinstance(confluence, dict):
        raise ValueError(f"Identity config {path} must define a 'confluence' mapping")

    normalized: Dict[str, str] = {}
    for key in ("base_url", "email", "api_key"):
        value = confluence.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"Identity config field '{key}' must be a string")
        stripped = value.strip()
        if stripped:
            normalized[key] = stripped
    return normalized


def resolve_identity_settings(base_url_arg: str | None, email_arg: str | None, config_path: Path) -> Dict[str, object]:
    config = load_identity_config(config_path)

    base_url = (base_url_arg or config.get("base_url") or DEFAULT_BASE_URL).strip()
    email = (email_arg or config.get("email") or "").strip()
    api_key = (config.get("api_key") or "").strip()

    return {
        "base_url": base_url,
        "email": email,
        "api_key": api_key,
        "config_path": config_path,
        "config_loaded": config_path.exists(),
    }
