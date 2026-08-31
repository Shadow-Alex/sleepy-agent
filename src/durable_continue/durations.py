from __future__ import annotations

import re

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_MULTIPLIERS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int:
    """Parse a positive duration like 30s, 10m, 2h, or 1d into seconds."""
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"invalid duration: {value!r}; use s, m, h, or d")
    number = float(match.group(1))
    unit = match.group(2).lower()
    seconds = int(number * _MULTIPLIERS[unit])
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


def format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
