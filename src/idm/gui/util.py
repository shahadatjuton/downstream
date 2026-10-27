"""Small formatting helpers shared by the GUI and CLI."""

from __future__ import annotations

import re
from datetime import datetime

_URL_RE = re.compile(r"^(https?|ftp)://\S+$", re.IGNORECASE)


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def fmt_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    return fmt_bytes(int(bps)) + "/s"


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m:02d}m"
    return f"{h // 24}d {h % 24}h"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def looks_like_url(text: str) -> bool:
    text = text.strip()
    return bool(text) and "\n" not in text and _URL_RE.match(text) is not None
