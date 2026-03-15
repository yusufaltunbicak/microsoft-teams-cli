from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime

SCHEMA_VERSION = "1.0"


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            if o.year == 1:  # datetime.min sentinel
                return None
            return o.isoformat()
        return super().default(o)


def _normalize(items: list | dict) -> list | dict:
    """Convert dataclass instances to dicts."""
    if isinstance(items, list):
        return [asdict(i) if hasattr(i, "__dataclass_fields__") else i for i in items]
    if hasattr(items, "__dataclass_fields__"):
        return asdict(items)
    return items


def to_json(items: list | dict, pretty: bool = True) -> str:
    """Serialize data to a JSON envelope: {ok, schema_version, data}."""
    data = _normalize(items)
    envelope = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "data": data,
    }
    return json.dumps(envelope, cls=_Encoder, indent=2 if pretty else None, ensure_ascii=False)


def to_json_error(message: str) -> str:
    """Serialize an error to JSON envelope: {ok: false, error}."""
    envelope = {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "error": message,
    }
    return json.dumps(envelope, indent=2, ensure_ascii=False)


def is_piped() -> bool:
    """True when stdout is piped (not a TTY)."""
    return not sys.stdout.isatty()


def save_json(items: list | dict, path: str) -> None:
    with open(path, "w") as f:
        data = _normalize(items)
        f.write(json.dumps(data, cls=_Encoder, indent=2, ensure_ascii=False))
