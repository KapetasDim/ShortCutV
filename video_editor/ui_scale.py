"""Loads config.yaml's two UI size profiles ("1080p" / "4k") and exposes the
active one's tokens as attributes. Widgets look this up at the point of
construction (get_scale().font_sm, etc.) rather than caching values, so that
switching the active profile and rebuilding a tab picks up the new sizes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .logging_config import get_logger

log = get_logger()

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
DEFAULT_PROFILE = "1080p"


class UIScale:
    def __init__(self, name: str, tokens: dict[str, Any]):
        self.name = name
        for key, value in tokens.items():
            setattr(self, key, value)


def _load_profiles() -> dict[str, dict[str, Any]]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["profiles"]


_profiles = _load_profiles()
_current = UIScale(DEFAULT_PROFILE, _profiles[DEFAULT_PROFILE])


def get_scale() -> UIScale:
    return _current


def available_profiles() -> list[str]:
    return list(_profiles.keys())


def set_profile(name: str) -> None:
    global _current
    if name not in _profiles:
        log.error("UI scale: unknown profile '%s', ignoring", name)
        return
    _current = UIScale(name, _profiles[name])
    log.info("UI scale: switched to profile '%s'", name)
