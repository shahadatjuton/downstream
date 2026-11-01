"""User settings, persisted as JSON in the app data directory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / "idm-clone"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_download_dir() -> str:
    return str(Path.home() / "Downloads")


DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "Compressed": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"],
    "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "epub"],
    "Music": ["mp3", "flac", "wav", "aac", "ogg", "m4a"],
    "Programs": ["exe", "msi", "apk", "dmg", "deb", "rpm"],
    "Video": ["mp4", "mkv", "avi", "mov", "webm", "flv", "wmv"],
    "Images": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"],
}


DEFAULT_TRACKERS: list[str] = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
]

TORRENT_FOLDER = "Torrents"


@dataclass
class Settings:
    download_dir: str = field(default_factory=default_download_dir)
    connections_per_download: int = 8
    max_concurrent_downloads: int = 3
    use_categories: bool = True
    categories: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_CATEGORIES.items()}
    )
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) idm-clone/0.1"
    max_retries: int = 5
    speed_limit_bytes: int = 0  # 0 = unlimited
    watch_clipboard: bool = True
    proxy: str = ""  # e.g. http://127.0.0.1:8080
    ffmpeg_path: str = ""  # optional; enables merged high-quality video formats
    ytdlp_auto_update: bool = True  # fetch the newest yt-dlp release into app data at startup
    browser_integration: bool = True  # listen for the browser extension
    show_complete_dialog: bool = True  # IDM-style popup when a download finishes
    auto_resume: bool = True  # continue downloads that were interrupted by a crash/shutdown
    theme: str = "auto"  # auto | light | dark
    preferred_height: int = 0  # last chosen video quality (0 = best offered)
    start_with_windows: bool = False  # HKCU Run key (set by the app or the installer)
    start_minimized: bool = False  # start in the tray
    check_updates: bool = True  # ask GitHub for a newer release once per start
    first_run_done: bool = False  # the welcome / browser-setup window has been shown
    extension_seen: bool = False  # the browser extension has talked to this app at least once
    watch_clipboard_sites: bool = True  # clipboard watcher also reacts to video pages, magnets and MEGA links
    # --- torrents
    torrent_port: int = 6881  # incoming peer connections (UPnP/NAT-PMP mapping is attempted)
    torrent_seed: bool = False  # keep sharing finished torrents while the app runs
    torrent_upload_limit_kb: int = 0  # 0 = unlimited
    torrent_max_connections: int = 200
    torrent_trackers: list[str] = field(default_factory=lambda: list(DEFAULT_TRACKERS))
    handle_magnet_links: bool = False  # register as the Windows handler for magnet: links
    # --- scheduler: queued downloads only run inside this daily window (see engine/schedule.py)
    schedule_enabled: bool = False
    schedule_start: str = "23:00"  # HH:MM, local time
    schedule_stop: str = "07:00"  # HH:MM; <= start means the window runs past midnight
    schedule_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # Monday = 0

    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(cls.path().read_text("utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        self.path().write_text(json.dumps(asdict(self), indent=2), "utf-8")

    def category_for(self, filename: str) -> str | None:
        ext = Path(filename).suffix.lower().lstrip(".")
        if not ext:
            return None
        for name, exts in self.categories.items():
            if ext in exts:
                return name
        return None

    def resolve_dest_dir(self, filename: str, override: str | None = None) -> str:
        if override:
            return override
        if self.use_categories:
            cat = self.category_for(filename)
            if cat:
                return str(Path(self.download_dir) / cat)
        return self.download_dir

    def torrent_dest_dir(self, override: str | None = None) -> str:
        if override:
            return override
        if self.use_categories:
            return str(Path(self.download_dir) / TORRENT_FOLDER)
        return self.download_dir
