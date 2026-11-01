"""BitTorrent downloads (magnet links and .torrent files) through libtorrent.

One libtorrent session is shared by every torrent task and created lazily on the manager's
loop thread. A TorrentRunner drives a single torrent the same way TaskRunner drives an HTTP
download: it reports progress into the DownloadTask (the completed-piece runs become the
segment map), honours pause/resume, saves fast-resume data so nothing is re-hashed on the
next start, and optionally keeps seeding after the download finishes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from .download import DownloadTask, Segment, Status
from .settings import DEFAULT_TRACKERS

if TYPE_CHECKING:
    from .settings import Settings

log = logging.getLogger("idm.torrent")

MAGNET_RE = re.compile(r"^magnet:\?.*xt=urn:bt[im]h:[0-9a-zA-Z]{32,64}", re.I)
TORRENT_MIME = "application/x-bittorrent"


MAX_SEGMENT_RUNS = 400  # cap on how many contiguous piece runs we show in the map


class TorrentError(Exception):
    pass


def is_magnet(text: str) -> bool:
    return bool(MAGNET_RE.match(text.strip()))


def looks_like_torrent_file(name_or_url: str, content_type: str = "") -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == TORRENT_MIME:
        return True
    path = name_or_url.split("?")[0].split("#")[0]
    return path.lower().endswith(".torrent")


def magnet_name(uri: str) -> str:
    """The display name a magnet link carries (dn=), or the info-hash as a stand-in."""
    q = parse_qs(urlparse(uri).query)
    dn = (q.get("dn") or [""])[0].strip()
    if dn:
        return dn
    xt = (q.get("xt") or [""])[0]
    return xt.rsplit(":", 1)[-1][:16] or "torrent"


def torrents_dir() -> Path:
    from .settings import app_data_dir

    d = app_data_dir() / "torrents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def describe_torrent_file(data: bytes) -> dict[str, Any]:
    """Name, total size and files of a .torrent, parsed locally (used by the add dialog)."""
    import libtorrent as lt

    try:
        ti = lt.torrent_info(lt.bdecode(data))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        raise TorrentError(f"not a valid .torrent file: {exc}") from exc
    fs = ti.files()
    files = [(fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files())]
    return {"name": ti.name(), "size": ti.total_size(), "files": files, "info_hash": str(ti.info_hash())}


# ------------------------------------------------------------------ session
class TorrentEngine:
    """Owns the libtorrent session. Create on the asyncio loop thread; call close() on shutdown."""

    def __init__(self, settings: Settings) -> None:
        import libtorrent as lt

        self.lt = lt
        self.settings = settings
        self._ses: Any = None
        self._seeding: dict[str, Any] = {}  # task id -> handle kept alive for seeding
        self._seeding_tasks: dict[str, DownloadTask] = {}
        self._pump: asyncio.Task | None = None
        self._waiters: dict[tuple[str, str], asyncio.Future] = {}  # (alert type, info hash) -> future

    @property
    def session(self) -> Any:
        if self._ses is None:
            s = self.settings
            port = max(0, int(s.torrent_port)) or _free_port()  # same number for TCP and uTP/UDP
            sp = {
                "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": True,
                "enable_natpmp": True,
                "user_agent": "idm-clone/1.0 libtorrent/" + self.lt.version,
                "alert_mask": self.lt.alert.category_t.error_notification
                | self.lt.alert.category_t.status_notification
                | self.lt.alert.category_t.storage_notification,
                "upload_rate_limit": int(s.torrent_upload_limit_kb) * 1024 if s.torrent_upload_limit_kb > 0 else 0,
                "connections_limit": max(50, int(s.torrent_max_connections)),
                "active_downloads": max(1, int(s.max_concurrent_downloads)),
                "active_seeds": 20,
                "active_limit": 40,
            }
            if s.proxy:
                p = urlparse(s.proxy)
                if p.hostname:
                    sp["proxy_hostname"] = p.hostname
                    sp["proxy_port"] = int(p.port or 8080)
                    sp["proxy_type"] = (
                        self.lt.proxy_type_t.socks5 if p.scheme.startswith("socks") else self.lt.proxy_type_t.http
                    )
                    sp["proxy_peer_connections"] = True
            self._ses = self.lt.session(sp)  # type: ignore[call-overload]
            for router in (
                ("router.bittorrent.com", 6881),
                ("router.utorrent.com", 6881),
                ("dht.transmissionbt.com", 6881),
            ):
                self._ses.add_dht_router(*router)
            self._pump = asyncio.ensure_future(self._pump_loop())
            log.info("libtorrent session started on port %s", self._ses.listen_port())
        return self._ses

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        if self._ses is None:
            return
        self._ses.apply_settings(
            {
                "upload_rate_limit": int(settings.torrent_upload_limit_kb) * 1024
                if settings.torrent_upload_limit_kb > 0
                else 0,
                "connections_limit": max(50, int(settings.torrent_max_connections)),
            }
        )
        if not settings.torrent_seed:
            for tid in list(self._seeding):
                self.stop_seeding(tid)

    async def close(self) -> None:
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if self._ses is not None:
            for tid in list(self._seeding):
                self.stop_seeding(tid)
            ses, self._ses = self._ses, None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, ses.pause)

    async def _pump_loop(self) -> None:
        """Drain the alert queue (waking up whoever waits for an alert) and refresh seeding stats."""
        tick = 0
        while True:
            await asyncio.sleep(0.2)
            if self._ses is None:
                return
            try:
                alerts = self._ses.pop_alerts()
            except Exception:  # noqa: BLE001
                alerts = []
            for a in alerts:
                self._dispatch(a)
            tick += 1
            if tick % 5:
                continue
            for tid, h in list(self._seeding.items()):
                task = self._seeding_tasks.get(tid)
                if task is None or not h.is_valid():
                    continue
                st = h.status()
                task.upload_speed = float(st.upload_payload_rate)
                task.peers = int(st.num_peers)
                task.seeds = int(st.num_seeds)
                task.note = f"seeding · {_fmt_rate(task.upload_speed)}" if task.upload_speed else "seeding"

    def _dispatch(self, alert: Any) -> None:
        h = getattr(alert, "handle", None)
        try:
            key = (type(alert).__name__, str(h.info_hash()) if h is not None and h.is_valid() else "")
        except Exception:  # noqa: BLE001
            return
        fut = self._waiters.pop(key, None)
        if fut is not None and not fut.done():
            fut.set_result(alert)

    async def wait_alert(self, name: str, handle: Any, timeout: float) -> Any:
        """Wait for the next alert of type `name` about `handle`; None on timeout."""
        try:
            key = (name, str(handle.info_hash()))
        except Exception:  # noqa: BLE001
            return None
        fut = asyncio.get_running_loop().create_future()
        self._waiters[key] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._waiters.pop(key, None)
            return None

    async def flush(self, handle: Any, timeout: float = 15.0) -> bool:
        """Make sure every downloaded byte is on disk before we call the torrent complete."""
        if handle is None or not handle.is_valid():
            return False
        waiter = asyncio.ensure_future(self.wait_alert("cache_flushed_alert", handle, timeout))
        await asyncio.sleep(0)  # register the waiter before the flush can complete
        try:
            handle.flush_cache()
        except Exception:  # noqa: BLE001
            waiter.cancel()
            return False
        return (await waiter) is not None

    # ----------------------------------------------------------- seeding
    def keep_seeding(self, task: DownloadTask, handle: Any) -> None:
        self._seeding[task.id] = handle
        self._seeding_tasks[task.id] = task
        task.note = "seeding"

    def stop_seeding(self, task_id: str, delete_files: bool = False) -> None:
        h = self._seeding.pop(task_id, None)
        task = self._seeding_tasks.pop(task_id, None)
        if task is not None:
            task.note = ""
            task.upload_speed = 0.0
        if h is not None and self._ses is not None and h.is_valid():
            self._remove(h, delete_files)

    def is_seeding(self, task_id: str) -> bool:
        return task_id in self._seeding

    def _remove(self, handle: Any, delete_files: bool) -> None:
        try:
            if delete_files:
                self._ses.remove_torrent(handle, self.lt.session.delete_files)
            else:
                self._ses.remove_torrent(handle)
        except Exception:  # noqa: BLE001
            pass

    def forget(self, task: DownloadTask, delete_files: bool = False) -> None:
        """Called when a task is removed from the list: drop seeding handle and resume data."""
        self.stop_seeding(task.id, delete_files)
        for suffix in (".fastresume", ".torrent"):
            try:
                (torrents_dir() / f"{task.id}{suffix}").unlink()
            except OSError:
                pass


# ------------------------------------------------------------------ runner
class TorrentRunner:
    """Drives one DownloadTask of kind 'torrent'. The interface mirrors TaskRunner."""

    POLL = 0.5

    def __init__(
        self,
        task: DownloadTask,
        engine: TorrentEngine,
        *,
        speed_limit: int = 0,
        seed: bool = False,
        trackers: list[str] | None = None,
    ) -> None:
        self.task = task
        self.engine = engine
        self.speed_limit = speed_limit
        self.seed = seed
        self.trackers = trackers if trackers is not None else DEFAULT_TRACKERS
        self._stop = asyncio.Event()
        self._handle: Any = None

    def pause(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- run
    async def run(self) -> None:
        task = self.task
        lt = self.engine.lt
        task.status = Status.DOWNLOADING
        task.error = None
        task.note = "connecting"
        self._stop.clear()
        try:
            params = self._params()
        except Exception as exc:  # noqa: BLE001
            task.status = Status.ERROR
            task.error = f"{type(exc).__name__}: {exc}"
            return
        ses = self.engine.session
        try:
            if self.engine.is_seeding(task.id):
                self.engine.stop_seeding(task.id)
            self._handle = ses.add_torrent(params)
        except Exception as exc:  # noqa: BLE001
            task.status = Status.ERROR
            task.error = f"could not add torrent: {exc}"
            return
        h = self._handle
        applied_limit = -1
        last_save = time.monotonic()
        try:
            while not self._stop.is_set():
                limit = task.speed_limit or self.speed_limit
                if limit != applied_limit:
                    h.set_download_limit(int(limit) if limit > 0 else -1)
                    applied_limit = limit
                st = h.status(lt.torrent_handle.query_pieces)
                self._report(st, h)
                if st.errc.value() != 0 and not st.has_metadata:
                    raise TorrentError(st.errc.message())
                if st.has_metadata and task.size is None:
                    task.size = int(st.total_wanted)
                if st.is_finished:
                    break
                if time.monotonic() - last_save > 30 and st.need_save_resume:
                    self._save_resume(h)
                    last_save = time.monotonic()
                await asyncio.sleep(self.POLL)
        except asyncio.CancelledError:
            self._stop.set()
            self._save_resume(h)
            self.engine._remove(h, False)
            task.status = Status.PAUSED
            task.speed = 0.0
            raise
        except Exception as exc:  # noqa: BLE001
            self._save_resume(h)
            self.engine._remove(h, False)
            task.status = Status.ERROR
            task.error = str(exc)
            task.speed = 0.0
            task.note = ""
            return

        task.speed = 0.0
        task.note = ""
        if self._stop.is_set():
            self._save_resume(h)
            self.engine._remove(h, False)
            task.status = Status.PAUSED
            return

        st = h.status(lt.torrent_handle.query_pieces)
        self._report(st, h)
        task.note = "writing to disk"
        log.debug("flush start")
        await self.engine.flush(h)
        log.debug("flush done")
        task.note = ""
        task.size = int(st.total_wanted)
        task.segments = [Segment(0, task.size - 1, task.size)] if task.size else []
        task.status = Status.COMPLETED
        task.completed_at = time.time()
        self._save_resume(h)
        log.debug("resume saved")
        if self.seed:
            self.engine.keep_seeding(task, h)
        else:
            self.engine._remove(h, False)

    # ------------------------------------------------------------ helpers
    def _params(self) -> Any:
        lt = self.engine.lt
        task = self.task
        resume = torrents_dir() / f"{task.id}.fastresume"
        params = None
        if resume.exists():
            try:
                params = lt.read_resume_data(resume.read_bytes())
            except Exception:  # noqa: BLE001
                params = None
        source = task.origin or task.url
        if params is None:
            if is_magnet(source):
                params = lt.parse_magnet_uri(source)
            else:
                tfile = torrents_dir() / f"{task.id}.torrent"
                if not tfile.exists():
                    raise TorrentError("the .torrent file for this download is missing; add it again")
                params = lt.add_torrent_params()
                params.ti = lt.torrent_info(lt.bdecode(tfile.read_bytes()))
        params.save_path = task.dest_dir
        params.flags |= lt.torrent_flags.auto_managed
        params.flags &= ~lt.torrent_flags.paused
        if self.trackers and is_magnet(source):
            existing = set(params.trackers)
            params.trackers = list(params.trackers) + [t for t in self.trackers if t not in existing]
        return params

    def _save_resume(self, h: Any) -> None:
        try:
            if not h.is_valid() or not h.has_metadata():
                return
            data = self.engine.lt.bencode(h.write_resume_data())
            (torrents_dir() / f"{self.task.id}.fastresume").write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not save resume data: %s", exc)

    def _report(self, st: Any, h: Any) -> None:
        task = self.task
        task.speed = float(st.download_payload_rate)
        task.upload_speed = float(st.upload_payload_rate)
        task.peers = int(st.num_peers)
        task.seeds = int(st.num_seeds)
        if st.has_metadata:
            ti = h.torrent_file()
            name = ti.name() if ti is not None else (st.name or "")
            if name and task.filename != name:
                task.filename = name
                task.title = name
            total = int(st.total_wanted)
            task.size = total or task.size
            task.segments = _piece_runs(st, total)
        state_names = {
            0: "checking files",
            1: "checking files",
            2: "fetching metadata",
            3: "",
            4: "finished",
            5: "seeding",
            6: "",
            7: "checking resume data",
        }
        state = int(st.state)
        label = state_names.get(state, "")
        if state == 3 or state == 6:
            label = f"{task.seeds} seeds · {task.peers} peers" if task.peers else "looking for peers"
        task.note = label


def _free_port() -> int:
    """A currently unused TCP port. libtorrent binds TCP and UDP (uTP) to the same number, and
    peers expect both on that number, so 'random' has to be resolved before the session starts."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


def _piece_runs(st: Any, total: int) -> list[Segment]:
    """Turn the piece bitfield into contiguous byte ranges for the segment map."""
    pieces = list(st.pieces) if st.pieces is not None else []
    n = len(pieces)
    if not n or not total:
        done = int(st.total_wanted_done)
        return [Segment(0, max(total - 1, 0), done)] if total else []
    piece_len = max(1, (total + n - 1) // n)
    runs: list[Segment] = []
    i = 0
    while i < n:
        if not pieces[i]:
            i += 1
            continue
        j = i
        while j < n and pieces[j]:
            j += 1
        start = i * piece_len
        end = min(total, j * piece_len) - 1
        runs.append(Segment(start, end, end - start + 1))
        i = j
        if len(runs) >= MAX_SEGMENT_RUNS:
            # Too fragmented to draw piece by piece: summarise the rest as one block.
            break
    if runs and len(runs) >= MAX_SEGMENT_RUNS:
        done = int(st.total_wanted_done)
        return [Segment(0, total - 1, done)]
    if not runs:
        return [Segment(0, total - 1, 0)]
    return runs


def _fmt_rate(bps: float) -> str:
    v = float(bps)
    for unit in ("B/s", "KB/s", "MB/s"):
        if v < 1024 or unit == "MB/s":
            return f"{v:.0f} {unit}" if unit == "B/s" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{bps} B/s"
