"""Loads .env from the project folder into the environment. Imported for its side effect.

The real environment always wins, so `PORTAL_SHOPIFY_API_KEY=… python -m …` still overrides
the file. Deliberately dependency-free and forgiving: this file is hand-edited, and a loader
that raises on a stray line is worse than one that skips it.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> list[str]:
    try:
        text = (path or ROOT / ".env").read_text()
    except OSError:
        return []
    loaded = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:   # blank = "not configured", don't mask a real var
            os.environ[key] = value
            loaded.append(key)
    return loaded


load_dotenv()
