"""Replace local network hosts in evidence intended for the repository."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _load_local_env() -> None:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_local_env()


def public_text(value: str) -> str:
    browser = urlsplit(os.getenv("BANKGPT_PLAYWRIGHT_WS_URL", "")).hostname
    public_browser = os.getenv("BANKGPT_PUBLIC_PLAYWRIGHT_HOST", "")
    if browser and public_browser:
        value = value.replace(browser, public_browser)
    allowed = [item.strip() for item in os.getenv("BANKGPT_ALLOWED_ORIGINS", "").split(",")
               if item.strip()]
    published = os.getenv("BANKGPT_ARTIFACT_PUBLIC_ORIGIN", "")
    if len(allowed) == 1 and published:
        value = value.replace(allowed[0].rstrip("/"), published.rstrip("/"))
    return value


def public_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: public_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_data(item) for item in value]
    if isinstance(value, str):
        return public_text(value)
    return value
