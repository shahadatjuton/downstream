"""Keep yt-dlp current without shipping a new app build.

Sites change weekly and yt-dlp follows within days, but the copy frozen into the installer only
changes when the app is released. So the app can fetch the official zipapp release asset
(`yt-dlp`, a zip holding the `yt_dlp` package) into app data and import from there in preference
to the bundled copy. If the downloaded copy fails to import, it is removed and the bundled one is
used again.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
import zipfile
from pathlib import Path

import aiohttp

from . import settings as _settings

log = logging.getLogger(__name__)

LATEST_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest"
DOWNLOAD_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
ZIP_NAME = "yt-dlp.zip"


def managed_dir() -> Path:
    return _settings.app_data_dir() / "yt-dlp"


def managed_zip() -> Path:
    return managed_dir() / ZIP_NAME


def parse_version(text: str) -> tuple[int, ...]:
    """'2026.10.26' or '2026.10.26.232215' -> comparable tuple; unknown -> ()."""
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def validate_zip(path: Path) -> str:
    """Make sure `path` is a yt-dlp zipapp and return the version it declares."""
    if not zipfile.is_zipfile(path):
        raise ValueError("not a zip file")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "yt_dlp/__init__.py" not in names or "yt_dlp/version.py" not in names:
            raise ValueError("archive does not contain the yt_dlp package")
        source = zf.read("yt_dlp/version.py").decode("utf-8", "replace")
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", source)
    if not m:
        raise ValueError("no version in archive")
    return m.group(1)


def _purge_modules() -> None:
    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[name]


def activate_managed() -> str | None:
    """Put the downloaded zip first on sys.path (if present and importable). Returns its version,
    or None when the bundled yt-dlp stays in charge. Safe to call again after an update."""
    path = managed_zip()
    if not path.exists():
        return None
    try:
        version = validate_zip(path)
    except (ValueError, OSError) as exc:
        log.warning("managed yt-dlp archive is unusable (%s); removing it", exc)
        _discard(path)
        return None
    entry = str(path)
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
    _purge_modules()
    try:
        mod = importlib.import_module("yt_dlp")
        loaded = getattr(importlib.import_module("yt_dlp.version"), "__version__", "")
        if not str(getattr(mod, "__file__", "")).startswith(entry) or loaded != version:
            raise ImportError(f"imported yt_dlp {loaded!r} from {getattr(mod, '__file__', '?')}")
    except Exception as exc:  # noqa: BLE001
        log.warning("managed yt-dlp %s failed to import (%s); falling back to the bundled copy", version, exc)
        sys.path.remove(entry)
        _purge_modules()
        _discard(path)
        return None
    log.info("using downloaded yt-dlp %s", version)
    return version


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def current_version() -> tuple[str, bool]:
    """(version, managed): the yt-dlp that `import yt_dlp` gives right now and whether it is the
    downloaded copy rather than the bundled one."""
    try:
        mod = importlib.import_module("yt_dlp")
        version = importlib.import_module("yt_dlp.version").__version__
    except Exception:  # noqa: BLE001
        return "", False
    managed = str(getattr(mod, "__file__", "")).startswith(str(managed_zip()))
    return str(version), managed


async def fetch_latest_version(session: aiohttp.ClientSession, proxy: str | None = None) -> str:
    """The newest release tag, read from the redirect GitHub answers for /releases/latest
    (no API quota involved)."""
    async with session.get(LATEST_URL, allow_redirects=False, proxy=proxy) as resp:
        location = resp.headers.get("Location", "")
    m = re.search(r"/tag/([^/?#]+)", location)
    if not m:
        raise ValueError(f"unexpected answer from GitHub ({resp.status})")
    return m.group(1)


async def download_latest(session: aiohttp.ClientSession, proxy: str | None = None) -> str:
    """Fetch the zipapp into the managed folder (atomically) and return the version inside."""
    target = managed_zip()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    async with session.get(DOWNLOAD_URL, allow_redirects=True, proxy=proxy) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            async for chunk in resp.content.iter_chunked(64 * 1024):
                fh.write(chunk)
    try:
        version = validate_zip(tmp)
    except ValueError:
        _discard(tmp)
        raise
    tmp.replace(target)
    return version
