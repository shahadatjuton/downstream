"""Video/audio downloads from web pages (YouTube and 1000+ sites) via yt-dlp."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .download import DownloadTask, Segment, Status
from .probe import sanitize_filename


class MediaError(Exception):
    pass


@dataclass
class MediaFormat:
    selector: str  # yt-dlp format selector
    label: str
    ext: str = "mp4"
    filesize: int | None = None
    height: int | None = None
    audio_only: bool = False
    needs_ffmpeg: bool = False
    client: str = ""  # yt-dlp player client that exposes this format ("" = default)


@dataclass
class MediaInfo:
    url: str
    title: str
    extractor: str
    duration: float | None = None
    thumbnail: str | None = None
    formats: list[MediaFormat] = field(default_factory=list)

    @property
    def filename_base(self) -> str:
        return sanitize_filename(self.title)[:120] or "video"


FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"


def managed_ffmpeg_dir() -> Path:
    from .settings import app_data_dir

    return app_data_dir() / "ffmpeg"


def install_ffmpeg_from_zip(zip_path: Path) -> str:
    """Extract ffmpeg.exe/ffprobe.exe from a BtbN/gyan style zip into the app data folder. Returns ffmpeg.exe path."""
    import zipfile

    target = managed_ffmpeg_dir()
    target.mkdir(parents=True, exist_ok=True)
    wanted = {"ffmpeg.exe", "ffprobe.exe"}
    found: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            name = member.rsplit("/", 1)[-1].lower()
            if name in wanted and not member.endswith("/"):
                with zf.open(member) as src, open(target / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                found.add(name)
    if "ffmpeg.exe" not in found:
        raise MediaError("ffmpeg.exe not found inside the downloaded archive")
    return str(target / "ffmpeg.exe")


def find_ffmpeg(hint: str = "") -> str | None:
    """Return a usable ffmpeg location (directory or exe) or None."""
    candidates = [hint] if hint else []
    candidates += [str(managed_ffmpeg_dir() / "ffmpeg.exe"), shutil.which("ffmpeg") or ""]
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        if base:
            candidates += [str(Path(base) / "ffmpeg" / "bin" / "ffmpeg.exe")]
    # Next to a frozen exe, or in the project folder.
    import sys

    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]
    candidates += [str(here / "ffmpeg.exe"), str(here / "ffmpeg" / "bin" / "ffmpeg.exe")]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def looks_like_media_page(url: str, content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("text/html", "application/xhtml+xml"):
        return True
    lowered = url.lower()
    return any(h in lowered for h in ("youtube.com/", "youtu.be/", "vimeo.com/", "twitch.tv/", "dailymotion.com/"))


def _client_args(client: str) -> dict:
    return {"extractor_args": {"youtube": {"player_client": [client]}}} if client else {}


# Clients that still expose YouTube's combined video+audio MP4 (format 18, 360p).
FALLBACK_CLIENTS = ("android", "android_vr")


def _ydl_opts(headers: dict[str, str] | None, proxy: str | None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if headers:
        opts["http_headers"] = dict(headers)
    if proxy:
        opts["proxy"] = proxy
    return opts


def extract_media(
    url: str, headers: dict[str, str] | None = None, proxy: str | None = None, ffmpeg: str | None = None
) -> MediaInfo:
    """Blocking: ask yt-dlp what this page contains and build a friendly format list."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(headers, proxy)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise MediaError(_clean_error(str(exc))) from exc
    if not info:
        raise MediaError("no media found on this page")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise MediaError("playlist is empty")
        info = entries[0]
    media = MediaInfo(
        url=info.get("webpage_url") or url,
        title=info.get("title") or "video",
        extractor=info.get("extractor_key") or info.get("extractor") or "generic",
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
    )
    media.formats = build_format_choices(info.get("formats") or [], ffmpeg is not None)
    if ffmpeg is None and not any(not f.audio_only for f in media.formats) and "youtube" in media.extractor.lower():
        media.formats = _youtube_progressive_fallback(url, headers, proxy) + media.formats
    if not media.formats and info.get("url"):
        media.formats = [MediaFormat(selector="best", label="Default", ext=info.get("ext") or "mp4")]
    if not media.formats:
        raise MediaError("no downloadable formats found")
    return media


def _youtube_progressive_fallback(url: str, headers: dict[str, str] | None, proxy: str | None) -> list[MediaFormat]:
    """Without ffmpeg the default client offers no video. Ask the Android client for its combined MP4."""
    import yt_dlp

    for client in FALLBACK_CLIENTS:
        opts = _ydl_opts(headers, proxy)
        opts.update(_client_args(client))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:  # noqa: BLE001
            continue
        if not info:
            continue
        choices = [f for f in build_format_choices(info.get("formats") or [], False) if not f.audio_only]
        if choices:
            for f in choices:
                f.client = client
                f.label = f.label.replace("(no ffmpeg needed)", "(video+audio, no ffmpeg needed)")
            return choices
    return []


def _clean_error(msg: str) -> str:
    msg = msg.replace("ERROR: ", "").strip()
    return msg.split("\n")[0][:300]


def build_format_choices(formats: list[dict], ffmpeg: bool) -> list[MediaFormat]:
    """Turn yt-dlp's raw format list into a short menu: merged qualities (ffmpeg), progressive, audio."""
    choices: list[MediaFormat] = []
    heights = sorted(
        {f.get("height") for f in formats if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True
    )
    progressive = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none") and f.get("url")
    ]
    progressive.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)

    if ffmpeg and heights:
        choices.append(
            MediaFormat(
                "bestvideo*+bestaudio/best",
                f"Best quality ({heights[0]}p, merged)",
                "mp4",
                height=heights[0],
                needs_ffmpeg=True,
            )
        )
        for h in heights[:6]:
            size = _size_for_height(formats, h)
            choices.append(
                MediaFormat(
                    f"bestvideo*[height<={h}]+bestaudio/best[height<={h}]",
                    f"{h}p" + (f" · ~{_fmt_size(size)}" if size else ""),
                    "mp4",
                    filesize=size,
                    height=h,
                    needs_ffmpeg=True,
                )
            )
    seen_heights: set[int] = set()
    for f in progressive:
        h = f.get("height") or 0
        if h in seen_heights:
            continue
        seen_heights.add(h)
        size = f.get("filesize") or f.get("filesize_approx")
        parts = [f"{h}p" if h else (f.get("format_note") or f.get("format_id") or "video")]
        parts.append(f.get("ext") or "mp4")
        if size:
            parts.append(_fmt_size(size))
        label = " · ".join(parts) + ("" if ffmpeg else "  (no ffmpeg needed)")
        choices.append(MediaFormat(f["format_id"], label, f.get("ext") or "mp4", filesize=size, height=h))
    audio = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    if audio:
        best_audio = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        size = best_audio.get("filesize") or best_audio.get("filesize_approx")
        ext = best_audio.get("ext") or "m4a"
        label = "Audio only" + (f" · {ext}" if ext else "") + (f" · {_fmt_size(size)}" if size else "")
        choices.append(MediaFormat(f"bestaudio[ext={ext}]/bestaudio", label, ext, filesize=size, audio_only=True))
    return choices


def _size_for_height(formats: list[dict], h: int) -> int | None:
    vids = [f for f in formats if f.get("height") == h and f.get("vcodec") not in (None, "none")]
    auds = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    if not vids:
        return None
    v = max(vids, key=lambda f: f.get("tbr") or 0)
    vs = v.get("filesize") or v.get("filesize_approx")
    if not vs:
        return None
    a_size = 0
    if auds and v.get("acodec") in (None, "none"):
        a = max(auds, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        a_size = a.get("filesize") or a.get("filesize_approx") or 0
    return int(vs + a_size)


def _fmt_size(n: int | float | None) -> str:
    if not n:
        return ""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


class MediaRunner:
    """Drives a DownloadTask of kind 'media' with yt-dlp in a worker thread."""

    def __init__(
        self, task: DownloadTask, *, ffmpeg: str | None = None, proxy: str | None = None, max_retries: int = 5
    ) -> None:
        self.task = task
        self.ffmpeg = ffmpeg
        self.proxy = proxy
        self.max_retries = max_retries
        self._stop = threading.Event()
        self._final_path: str | None = None
        self._samples: list[tuple[float, int]] = []

    def pause(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_blocking)

    # ------------------------------------------------------------- blocking
    def _run_blocking(self) -> None:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled

        task = self.task
        task.status = Status.DOWNLOADING
        task.error = None
        Path(task.dest_dir).mkdir(parents=True, exist_ok=True)
        base = Path(task.filename).stem if task.filename else "video"
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "continuedl": True,
            "retries": self.max_retries,
            "fragment_retries": self.max_retries,
            "outtmpl": str(Path(task.dest_dir) / f"{base}.%(ext)s"),
            "format": task.media_format or "best",
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._pp_hook],
            "socket_timeout": 30,
            "overwrites": False,
        }
        if task.headers:
            opts["http_headers"] = dict(task.headers)
        if self.proxy:
            opts["proxy"] = self.proxy
        opts.update(_client_args(task.media_client))
        if self.ffmpeg:
            opts["ffmpeg_location"] = self.ffmpeg
            if not task.media_format or "+" in task.media_format or task.media_format.startswith("best"):
                opts["merge_output_format"] = "mp4"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(task.url, download=True)
            if info:
                reqs = info.get("requested_downloads") or []
                if reqs and reqs[0].get("filepath"):
                    self._final_path = reqs[0]["filepath"]
                elif info.get("filepath"):
                    self._final_path = info["filepath"]
        except DownloadCancelled:
            task.status = Status.PAUSED
            task.speed = 0.0
            return
        except Exception as exc:  # noqa: BLE001
            if self._stop.is_set():
                task.status = Status.PAUSED
            else:
                task.status = Status.ERROR
                task.error = _clean_error(str(exc))
            task.speed = 0.0
            return

        task.speed = 0.0
        if self._final_path and Path(self._final_path).exists():
            task.filename = Path(self._final_path).name
            task.dest_dir = str(Path(self._final_path).parent)
            size = Path(self._final_path).stat().st_size
            task.size = size
            task.segments = [Segment(0, size - 1, size)]
        task.status = Status.COMPLETED
        task.completed_at = time.time()

    def _hook(self, d: dict) -> None:
        from yt_dlp.utils import DownloadCancelled

        if self._stop.is_set():
            raise DownloadCancelled("paused by user")
        task = self.task
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            task.size = int(total) if total else None
            task.segments = [Segment(0, int(total) - 1 if total else -1, int(done))]
            task.speed = float(d.get("speed") or 0.0)
            name = d.get("filename")
            if name:
                task.filename = Path(name).name.removesuffix(".part")
        elif status == "finished":
            name = d.get("filename")
            if name:
                self._final_path = name
                task.filename = Path(name).name
            total = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            if total:
                task.size = int(total)
                task.segments = [Segment(0, int(total) - 1, int(total))]

    def _pp_hook(self, d: dict) -> None:
        if d.get("status") == "finished":
            info = d.get("info_dict") or {}
            path = info.get("filepath") or info.get("_filename")
            if path:
                self._final_path = path
