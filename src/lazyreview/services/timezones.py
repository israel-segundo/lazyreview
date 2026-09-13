"""Display timezone helpers.

All timestamps are stored in UTC. The portal converts them for display using
the configured IANA timezone (default: America/Mexico_City).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Curated list shown in Settings → General. Values are IANA names.
DISPLAY_TIMEZONE_CHOICES: list[tuple[str, str]] = [
    ("America/Mexico_City", "Mexico City (CST/CDT)"),
    ("UTC", "UTC"),
    ("America/New_York", "New York (Eastern)"),
    ("America/Chicago", "Chicago (Central)"),
    ("America/Denver", "Denver (Mountain)"),
    ("America/Los_Angeles", "Los Angeles (Pacific)"),
    ("America/Sao_Paulo", "São Paulo"),
    ("Europe/London", "London"),
    ("Europe/Paris", "Paris"),
    ("Europe/Berlin", "Berlin"),
    ("Asia/Tokyo", "Tokyo"),
    ("Asia/Shanghai", "Shanghai"),
    ("Australia/Sydney", "Sydney"),
]

DEFAULT_DISPLAY_TIMEZONE = "America/Mexico_City"
_ALLOWED = {tz for tz, _ in DISPLAY_TIMEZONE_CHOICES}


def is_valid_display_timezone(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    if text in _ALLOWED:
        return True
    try:
        ZoneInfo(text)
        return True
    except ZoneInfoNotFoundError:
        return False


def normalize_display_timezone(name: str | None) -> str:
    text = (name or "").strip()
    if is_valid_display_timezone(text):
        return text
    return DEFAULT_DISPLAY_TIMEZONE
