"""Small formatting helpers shared by the GUI and CLI."""

from __future__ import annotations

import re
from datetime import datetime

_URL_RE = re.compile(r"^(https?|ftp)://\S+$", re.IGNORECASE)
_MAGNET_RE = re.compile(r"^magnet:\?\S+$", re.IGNORECASE)


_HTTP_CODE = re.compile(r"(?:HTTP\s+|status[=:]\s*|\b)([1-5]\d\d)\b(?:,\s*message=|\b)")


def short_error(error: str | None) -> str:
    """'HTTP 403' / 'Forbidden' style summary of an engine error for a narrow column."""
    if not error:
        return ""
    name, _, rest = error.partition(":")
    text = (rest.strip() or name) if name.endswith("Error") else error
    m = re.search(r"\b([1-5]\d\d)\b", text)
    if m and ("HTTP" in text or "message=" in text or "status" in text.lower()):
        reason = re.search(r"message='([^']*)'", text)
        return f"HTTP {m.group(1)}" + (f" {reason.group(1)}" if reason and reason.group(1) else "")
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "Timed out"
    if "cannot connect" in low or "connection refused" in low or "getaddrinfo" in low:
        return "Cannot connect"
    if "no space" in low or "errno 28" in low:
        return "Disk full"
    if "permission" in low or "errno 13" in low:
        return "Access denied"
    return text if len(text) <= 40 else text[:39] + "…"


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
    """True for http(s)/ftp links and magnet links (anything the add dialog can take as one line)."""
    text = text.strip()
    if not text or "\n" in text:
        return False
    return _URL_RE.match(text) is not None or _MAGNET_RE.match(text) is not None


def is_torrent_path(text: str) -> bool:
    """A local .torrent file (typed, dropped, or passed on the command line)."""
    from pathlib import Path

    text = text.strip().strip('"')
    if not text.lower().endswith(".torrent") or "://" in text:
        return False
    try:
        return Path(text).is_file()
    except OSError:
        return False


def link_kind(text: str) -> str:
    """'magnet' | 'mega' | 'torrent' (a .torrent URL or local file) | 'video' (known video site) |
    'url' | '' (not a link)."""
    from ..engine import is_magnet, is_mega_link, is_video_site, looks_like_torrent_file

    text = text.strip()
    if is_torrent_path(text):
        return "torrent"
    if not looks_like_url(text):
        return ""
    if is_magnet(text):
        return "magnet"
    if is_mega_link(text):
        return "mega"
    if looks_like_torrent_file(text):
        return "torrent"
    if is_video_site(text):
        return "video"
    return "url"


def source_label(text: str) -> str:
    """Short human name of what a link is, for status messages."""
    return {"magnet": "magnet link", "mega": "MEGA link", "torrent": "torrent", "video": "video page"}.get(
        link_kind(text), "link"
    )
