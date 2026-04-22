from __future__ import annotations

from typing import Optional

def resolve_api_key(inline_value: str | None) -> Optional[str]:
    if inline_value:
        return inline_value.strip()
    return None


def missing_dependency_message(exc: ModuleNotFoundError) -> str:
    missing_name = getattr(exc, "name", "a required package")
    return f"Missing dependency: {missing_name}. Install project dependencies with 'pip install -r requirements.txt' and try again."
