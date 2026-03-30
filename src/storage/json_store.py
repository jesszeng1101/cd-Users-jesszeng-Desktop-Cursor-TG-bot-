"""Small JSON persistence helpers for local state files."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def read_json(path: Path, default_obj: dict[str, Any]) -> dict[str, Any]:
    try:
        if not path.exists():
            path.write_text(json.dumps(default_obj, indent=2), encoding="utf-8")
            return json.loads(json.dumps(default_obj))
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return json.loads(json.dumps(default_obj))


def write_json(path: Path, data: dict[str, Any]) -> None:
    try:
        data["last_updated"] = now_iso()
    except Exception:
        pass
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
