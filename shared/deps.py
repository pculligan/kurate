from __future__ import annotations

from typing import Optional

try:
    import yaml
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]
