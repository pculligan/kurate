from __future__ import annotations

from typing import Optional

MISSING_DEPENDENCY_ERROR: Optional[ModuleNotFoundError] = None

try:
    import requests
    from bs4 import BeautifulSoup, NavigableString, Tag
    import markdown
    import yaml
except ModuleNotFoundError as exc:
    requests = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]
    NavigableString = None  # type: ignore[assignment]
    Tag = None  # type: ignore[assignment]
    markdown = None  # type: ignore[assignment]
    yaml = None  # type: ignore[assignment]
    MISSING_DEPENDENCY_ERROR = exc
