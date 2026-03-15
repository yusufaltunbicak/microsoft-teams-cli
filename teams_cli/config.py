from __future__ import annotations

from pathlib import Path

import yaml

from .constants import CONFIG_FILE

DEFAULTS = {
    "max_messages": 25,
    "max_chats": 25,
    "region": "emea",
    "browser": {
        "headless": False,
        "timeout": 120,
    },
    "timeout": 30,
    "output_format": "table",
    "jitter": {
        "read_base": 0.3,
        "write_base": 2.0,
    },
}


def load_config(path: Path | None = None) -> dict:
    """Load YAML config, falling back to defaults for missing keys."""
    cfg = dict(DEFAULTS)
    p = path or CONFIG_FILE
    if p.exists():
        with open(p) as f:
            user = yaml.safe_load(f) or {}
        _deep_merge(cfg, user)
    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
