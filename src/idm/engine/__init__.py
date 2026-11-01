from .download import DownloadTask, DuplicateError, Segment, Status
from .manager import DownloadManager
from .media import MediaError, MediaFormat, MediaInfo, find_ffmpeg, is_video_site
from .mega import MegaError, MegaFile, is_mega_link
from .probe import AuthRequiredError, ProbeResult, probe
from .settings import Settings
from .torrent import TorrentError, is_magnet, looks_like_torrent_file

__all__ = [
    "AuthRequiredError",
    "DownloadManager",
    "DownloadTask",
    "DuplicateError",
    "MediaError",
    "MediaFormat",
    "MediaInfo",
    "MegaError",
    "MegaFile",
    "ProbeResult",
    "Segment",
    "Settings",
    "Status",
    "TorrentError",
    "find_ffmpeg",
    "is_magnet",
    "is_mega_link",
    "is_video_site",
    "looks_like_torrent_file",
    "probe",
]
