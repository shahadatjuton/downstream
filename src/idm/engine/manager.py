"""DownloadManager: queue, concurrency, persistence, browser IPC. Runs an asyncio loop on
its own thread so a synchronous GUI (or CLI) can drive it through thread-safe methods."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from . import schedule as _schedule
from .download import DownloadTask, DuplicateError, Status, TaskRunner
from .ipc import IpcServer, app_path_file
from .media import MediaInfo, MediaRunner, extract_media, find_ffmpeg
from .mega import MegaClient, MegaFile, MegaRunner, b64encode, is_mega_link
from .probe import AuthRequiredError, ProbeResult, probe
from .secrets import CredentialStore, basic_auth_header, host_of
from .settings import Settings, app_data_dir
from .torrent import (
    TorrentEngine,
    TorrentError,
    TorrentRunner,
    describe_torrent_file,
    is_magnet,
    looks_like_torrent_file,
    magnet_name,
    torrents_dir,
)

Runner = TaskRunner | MediaRunner | TorrentRunner

EventCallback = Callable[[str, DownloadTask], None]
IpcCallback = Callable[[dict[str, Any]], dict[str, Any] | None]


class DownloadManager:
    def __init__(
        self, settings: Settings | None = None, state_path: Path | None = None, enable_ipc: bool = False
    ) -> None:
        self.settings = settings or Settings.load()
        self.state_path = state_path or (app_data_dir() / "downloads.json")
        self.tasks: dict[str, DownloadTask] = {}
        self.on_event: EventCallback | None = None  # called on the loop thread
        self.on_ipc: IpcCallback | None = None  # called on the loop thread; GUI decides what to do
        self.enable_ipc = enable_ipc
        self.credentials = CredentialStore()  # per-host logins, password DPAPI-protected on disk
        self.clock: Callable[[], float] = time.time  # injectable for scheduler tests
        self.schedule_poll = 15.0  # seconds between scheduler window checks

        self._held: set[str] = set()  # runners paused by the scheduler; go back to QUEUED when they stop
        self._sched_timer: asyncio.TimerHandle | None = None
        self._window_was_open: bool | None = None

        self._runners: dict[str, Runner] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: aiohttp.ClientSession | None = None
        self._mega: MegaClient | None = None
        self._torrents: TorrentEngine | None = None
        self._ready = threading.Event()
        self._save_timer: asyncio.TimerHandle | None = None
        self._ipc: IpcServer | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread:
            return
        self._load()
        self._thread = threading.Thread(target=self._thread_main, name="idm-loop", daemon=True)
        self._thread.start()
        self._ready.wait()

    def stop(self, timeout: float = 10.0) -> None:
        if not self._loop:
            return
        fut = self._call(self._shutdown())
        try:
            fut.result(timeout)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout)
        self._thread = None
        self._loop = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._startup())
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _startup(self) -> None:
        self._session = self._make_session()
        self._mega = MegaClient(self._session, self.settings.proxy or None)
        if self.enable_ipc:
            self._ipc = IpcServer(self._handle_ipc)
            try:
                await self._ipc.start()
            except OSError:
                self._ipc = None
        asyncio.get_running_loop().call_soon(self._schedule_soon)
        asyncio.get_running_loop().call_soon(self._schedule_check)

    def _make_session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
        connector = aiohttp.TCPConnector(limit=0, limit_per_host=0)
        return aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": self.settings.user_agent},
            trust_env=True,
        )

    async def _shutdown(self) -> None:
        for runner in list(self._runners.values()):
            runner.pause()
        for _ in range(50):
            if not self._runners:
                break
            await asyncio.sleep(0.1)
        self._save()
        if self._ipc:
            await self._ipc.stop()
            self._ipc = None
        if self._torrents:
            await self._torrents.close()
            self._torrents = None
        if self._session:
            await self._session.close()
            self._session = None
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def record_app_path() -> None:
        """Remember how to launch this app so the browser host can start it on demand."""
        if getattr(sys, "frozen", False):
            cmd = f'"{sys.executable}"'
        else:
            cmd = f'"{sys.executable}" -m idm'
        try:
            app_path_file().write_text(cmd, "utf-8")
        except OSError:
            pass

    # ------------------------------------------------------- thread bridge
    def _call(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        assert self._loop is not None, "manager not started"
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------ public API
    @property
    def ffmpeg(self) -> str | None:
        return find_ffmpeg(self.settings.ffmpeg_path)

    def probe(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        username: str = "",
        password: str = "",
    ) -> concurrent.futures.Future:
        """Returns a Future[ProbeResult]. Raises AuthRequiredError (401) when the site wants a login
        and none is known; pass username/password to retry with explicit credentials."""
        return self._call(self._probe(url, self._with_auth(url, headers, username, password)))

    # ------------------------------------------------------------- logins
    @staticmethod
    def _with_auth(
        url: str, headers: dict[str, str] | None, username: str = "", password: str = ""
    ) -> dict[str, str] | None:
        """Explicit credentials become an Authorization header; otherwise headers pass through."""
        if not username:
            return headers
        out = dict(headers or {})
        out["Authorization"] = basic_auth_header(username, password)
        return out

    def auth_header_for(self, url: str) -> str | None:
        cred = self.credentials.get(url)
        return basic_auth_header(cred.username, cred.password) if cred else None

    def save_credential(self, url_or_host: str, username: str, password: str) -> None:
        self.credentials.set(url_or_host, username, password)

    def forget_credential(self, host: str) -> None:
        self.credentials.remove(host)

    def extract_media(self, url: str, headers: dict[str, str] | None = None) -> concurrent.futures.Future:
        """Returns a Future[MediaInfo] (yt-dlp analysis of a page)."""
        return self._call(self._extract_media(url, headers))

    def add(
        self,
        url: str,
        *,
        dest_dir: str | None = None,
        filename: str | None = None,
        connections: int | None = None,
        headers: dict[str, str] | None = None,
        start: bool = True,
        probe_result: ProbeResult | None = None,
        referrer: str = "",
        username: str = "",
        password: str = "",
        remember_login: bool = True,
        allow_duplicate: bool = True,
    ) -> concurrent.futures.Future:
        """Returns a Future[DownloadTask]. With username/password the download authenticates (HTTP
        Basic); remember_login stores them for the host so later downloads from it need nothing.
        With allow_duplicate=False a link that is already listed raises DuplicateError instead."""
        if username and remember_login:
            self.credentials.set(url, username, password)
            headers = dict(headers or {})
            headers.pop("Authorization", None)  # fetched from the store at request time instead
        elif username:
            headers = self._with_auth(url, headers, username, password)
        return self._call(
            self._add(url, dest_dir, filename, connections, headers, start, probe_result, referrer, allow_duplicate)
        )

    def find_duplicate(self, url: str) -> DownloadTask | None:
        """The listed task with this link (original or resolved), if any. Thread-safe."""
        needle = url.strip()
        if not needle:
            return None
        for task in list(self.tasks.values()):
            if needle in (task.url, task.origin):
                return task
        return None

    def add_media(
        self,
        info: MediaInfo,
        format_selector: str,
        *,
        dest_dir: str | None = None,
        filename: str | None = None,
        headers: dict[str, str] | None = None,
        start: bool = True,
        ext: str = "mp4",
        client: str = "",
        postprocess: str = "",
        thumbnail: str = "",
    ) -> concurrent.futures.Future:
        """Returns a Future[DownloadTask] for a page video/audio download."""
        return self._call(
            self._add_media(
                info, format_selector, dest_dir, filename, headers, start, ext, client, postprocess, thumbnail
            )
        )

    def add_smart(
        self,
        url: str,
        *,
        dest_dir: str | None = None,
        filename: str | None = None,
        headers: dict[str, str] | None = None,
        referrer: str = "",
        kind: str = "auto",
        start: bool = True,
    ) -> concurrent.futures.Future:
        """Headless add that never saves a web page as a file: magnets and .torrent files become
        torrents, MEGA links are decrypted, HTML pages go through yt-dlp (best available quality),
        everything else is a normal segmented download. Returns Future[DownloadTask]."""
        return self._call(self._add_smart(url, dest_dir, filename, headers, referrer, kind, start))

    async def _add_smart(self, url, dest_dir, filename, headers, referrer, kind, start: bool = True) -> DownloadTask:
        from .media import looks_like_media_page

        if kind == "torrent" or is_magnet(url) or looks_like_torrent_file(url) and kind != "media":
            return await self._add_torrent(url, dest_dir, start, headers)
        if kind == "mega" or is_mega_link(url):
            return (await self._add_mega(url, dest_dir, filename, start, None))[0]
        pr: ProbeResult | None = None
        is_page = kind == "media"
        if not is_page:
            try:
                pr = await self._probe(url, headers)
                if looks_like_torrent_file(pr.filename, pr.content_type) and kind != "media":
                    return await self._add_torrent(url, dest_dir, start, headers)
                is_page = kind not in ("file", "download") and looks_like_media_page(pr.final_url, pr.content_type)
            except Exception:
                if kind in ("file", "download") or not looks_like_media_page(url, ""):
                    raise
                is_page = True
        if not is_page:
            return await self._add(url, dest_dir, filename, None, headers, start, pr, referrer)
        info = await self._extract_media(url, headers)
        fmt = info.formats[0]
        return await self._add_media(
            info, fmt.selector, dest_dir, filename, headers, start, fmt.ext, fmt.client, thumbnail=info.thumbnail or ""
        )

    # ------------------------------------------------------------ MEGA
    def resolve_mega(self, link: str) -> concurrent.futures.Future:
        """Returns Future[list[MegaFile]]: one entry for a file link, every file for a folder link."""
        assert self._mega
        return self._call(self._mega.resolve(link))

    def add_mega(
        self,
        link: str,
        *,
        dest_dir: str | None = None,
        filename: str | None = None,
        start: bool = True,
        files: list[MegaFile] | None = None,
    ) -> concurrent.futures.Future:
        """Returns Future[list[DownloadTask]]. Pass `files` from resolve_mega to skip a second lookup."""
        return self._call(self._add_mega(link, dest_dir, filename, start, files))

    async def _add_mega(
        self, link: str, dest_dir: str | None, filename: str | None, start: bool, files: list[MegaFile] | None
    ) -> list[DownloadTask]:
        assert self._mega
        if files is None:
            files = await self._mega.resolve(link)
        tasks: list[DownloadTask] = []
        for f in files:
            name = filename if (filename and len(files) == 1) else f.name
            base = self.settings.resolve_dest_dir(name, dest_dir)
            if f.path:
                base = str(Path(base) / f.path)
            task = DownloadTask(
                url=f.url,
                filename=name,
                dest_dir=base,
                size=f.size,
                resumable=True,
                connections=self.settings.connections_per_download,
                status=Status.QUEUED if start else Status.PAUSED,
                kind="mega",
                origin=f.link,
                crypto_key=b64encode(f.key),
                title=f.name,
            )
            tasks.append(self._register(task))
        return tasks

    # --------------------------------------------------------- torrents
    def describe_torrent(self, source: str | bytes, headers: dict[str, str] | None = None) -> concurrent.futures.Future:
        """Returns Future[dict] with name/size/files for a magnet, a .torrent URL, path or bytes."""
        return self._call(self._describe_torrent(source, headers))

    def add_torrent(
        self,
        source: str | bytes,
        *,
        dest_dir: str | None = None,
        start: bool = True,
        headers: dict[str, str] | None = None,
    ) -> concurrent.futures.Future:
        """Returns Future[DownloadTask]. `source` is a magnet link, a .torrent URL or local path, or its bytes."""
        return self._call(self._add_torrent(source, dest_dir, start, headers))

    async def _torrent_bytes(self, source: str | bytes, headers: dict[str, str] | None) -> bytes:
        if isinstance(source, bytes):
            return source
        if source.lower().startswith(("http://", "https://", "ftp://")):
            return await self._fetch_bytes(source, 20_000_000, headers)
        path = Path(source.removeprefix("file:///").removeprefix("file://"))
        if not path.is_file():
            raise TorrentError(f"no such .torrent file: {source}")
        return path.read_bytes()

    async def _describe_torrent(self, source: str | bytes, headers: dict[str, str] | None) -> dict[str, Any]:
        if isinstance(source, str) and is_magnet(source):
            return {"name": magnet_name(source), "size": None, "files": [], "magnet": True}
        data = await self._torrent_bytes(source, headers)
        loop = asyncio.get_running_loop()
        desc = await loop.run_in_executor(None, describe_torrent_file, data)
        desc["magnet"] = False
        return desc

    async def _add_torrent(
        self, source: str | bytes, dest_dir: str | None, start: bool, headers: dict[str, str] | None
    ) -> DownloadTask:
        dest = self.settings.torrent_dest_dir(dest_dir)
        if isinstance(source, str) and is_magnet(source):
            task = DownloadTask(
                url=source,
                filename=magnet_name(source),
                dest_dir=dest,
                size=None,
                resumable=True,
                connections=0,
                status=Status.QUEUED if start else Status.PAUSED,
                kind="torrent",
                origin=source,
            )
            return self._register(task)
        data = await self._torrent_bytes(source, headers)
        loop = asyncio.get_running_loop()
        desc = await loop.run_in_executor(None, describe_torrent_file, data)
        task = DownloadTask(
            url=source if isinstance(source, str) else "",
            filename=desc["name"],
            dest_dir=dest,
            size=desc["size"],
            resumable=True,
            connections=0,
            status=Status.QUEUED if start else Status.PAUSED,
            kind="torrent",
            origin=source if isinstance(source, str) else "",
            title=desc["name"],
        )
        (torrents_dir() / f"{task.id}.torrent").write_bytes(data)
        return self._register(task)

    def _torrent_engine(self) -> TorrentEngine:
        if self._torrents is None:
            self._torrents = TorrentEngine(self.settings)
        return self._torrents

    def torrent_seeding(self, task_id: str) -> bool:
        return bool(self._torrents and self._torrents.is_seeding(task_id))

    def move_task(self, task_id: str, where: str) -> concurrent.futures.Future:
        """Reorder the queue: where is 'top', 'up', 'down' or 'bottom'."""
        return self._call(self._move_task(task_id, where))

    def remove_completed(self) -> concurrent.futures.Future:
        """Drop finished entries from the list (files are kept). Returns Future[int]."""
        return self._call(self._remove_completed())

    def fetch_bytes(self, url: str, limit: int = 2_000_000) -> concurrent.futures.Future:
        """Small helper for thumbnails. Returns Future[bytes]."""
        return self._call(self._fetch_bytes(url, limit))

    async def _move_task(self, task_id: str, where: str) -> None:
        ids = list(self.tasks.keys())
        if task_id not in ids:
            return
        i = ids.index(task_id)
        ids.pop(i)
        if where == "top":
            ids.insert(0, task_id)
        elif where == "bottom":
            ids.append(task_id)
        elif where == "up":
            ids.insert(max(0, i - 1), task_id)
        elif where == "down":
            ids.insert(min(len(ids), i + 1), task_id)
        else:
            ids.insert(i, task_id)
        self.tasks = {k: self.tasks[k] for k in ids}
        self._save()
        self._schedule_soon()

    async def _remove_completed(self) -> int:
        done = [t.id for t in self.tasks.values() if t.status == Status.COMPLETED]
        for tid in done:
            task = self.tasks.pop(tid)
            self._emit("removed", task)
        if done:
            self._save()
        return len(done)

    async def _fetch_bytes(self, url: str, limit: int, headers: dict[str, str] | None = None) -> bytes:
        """Read a whole (small) response body, stopping at `limit` bytes."""
        assert self._session
        async with self._session.get(url, headers=headers or {}, proxy=self.settings.proxy or None) as resp:
            resp.raise_for_status()
            if resp.content_length and resp.content_length > limit:
                raise ValueError(f"response too large ({resp.content_length} bytes, limit {limit})")
            chunks: list[bytes] = []
            got = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                chunks.append(chunk)
                got += len(chunk)
                if got >= limit:
                    break
            return b"".join(chunks)[:limit]

    def start_task(self, task_id: str) -> concurrent.futures.Future:
        return self._call(self._start_task(task_id))

    def pause_task(self, task_id: str) -> concurrent.futures.Future:
        return self._call(self._pause_task(task_id))

    def remove_task(self, task_id: str, delete_file: bool = False) -> concurrent.futures.Future:
        return self._call(self._remove_task(task_id, delete_file))

    def start_all(self) -> concurrent.futures.Future:
        return self._call(self._start_all())

    def pause_all(self) -> concurrent.futures.Future:
        return self._call(self._pause_all())

    def set_task_limit(self, task_id: str, bytes_per_sec: int) -> concurrent.futures.Future:
        """Per-download speed cap (0 = follow the global setting); applies immediately."""
        return self._call(self._set_task_field(task_id, "speed_limit", max(0, int(bytes_per_sec))))

    def set_task_connections(self, task_id: str, n: int) -> concurrent.futures.Future:
        """Change the connection count; a running download adds or retires workers on the fly."""
        return self._call(self._set_task_field(task_id, "connections", max(1, min(32, int(n)))))

    async def _set_task_field(self, task_id: str, name: str, value: int) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        setattr(task, name, value)
        self._save()

    def update_settings(self, settings: Settings) -> concurrent.futures.Future:
        return self._call(self._update_settings(settings))

    def snapshot(self) -> list[DownloadTask]:
        return list(self.tasks.values())

    def wait(self, task_id: str, timeout: float | None = None) -> DownloadTask:
        return self._call(self._wait(task_id)).result(timeout)

    @property
    def ipc_port(self) -> int | None:
        return self._ipc.port if self._ipc else None

    def install_ffmpeg(self) -> concurrent.futures.Future:
        """Download a static ffmpeg build through the normal queue and unpack it into app data.
        Emits 'ffmpeg-ready' or 'ffmpeg-failed' on the event callback. Returns Future[DownloadTask]."""
        return self._call(self._install_ffmpeg())

    async def _install_ffmpeg(self) -> DownloadTask:
        from .media import FFMPEG_URL, managed_ffmpeg_dir

        target = managed_ffmpeg_dir() / "download"
        task = await self._add(FFMPEG_URL, str(target), "ffmpeg-win64.zip", None, None, True, None, "")
        task.title = "ffmpeg (video merging tool)"
        self._ffmpeg_task_id = task.id
        return task

    async def _after_ffmpeg_download(self, task: DownloadTask) -> None:
        from .media import install_ffmpeg_from_zip

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, install_ffmpeg_from_zip, task.final_path)
            self.settings.ffmpeg_path = ""  # auto-detect now finds the managed copy
            self.settings.save()
            self._emit("ffmpeg-ready", task)
        except Exception as exc:  # noqa: BLE001
            task.status = Status.ERROR
            task.error = f"could not unpack ffmpeg: {exc}"
            self._emit("ffmpeg-failed", task)
        finally:
            try:
                task.final_path.unlink()
                self.tasks.pop(task.id, None)
                self._emit("removed", task)
            except OSError:
                pass
            self._save()

    # -------------------------------------------------------- loop-side impl
    async def _probe(self, url: str, headers: dict[str, str] | None) -> ProbeResult:
        assert self._session
        hdrs = dict(headers or {})
        saved = None if "Authorization" in hdrs else self.auth_header_for(url)
        if saved:
            hdrs["Authorization"] = saved
        try:
            return await probe(self._session, url, hdrs, proxy=self.settings.proxy)
        except AuthRequiredError:
            if saved:
                self.credentials.remove(host_of(url))  # the stored login no longer works
            raise

    async def _extract_media(self, url: str, headers: dict[str, str] | None) -> MediaInfo:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: extract_media(url, headers, self.settings.proxy or None, self.ffmpeg)
        )

    async def _add(
        self,
        url: str,
        dest_dir: str | None,
        filename: str | None,
        connections: int | None,
        headers: dict[str, str] | None,
        start: bool,
        pr: ProbeResult | None,
        referrer: str,
        allow_duplicate: bool = True,
    ) -> DownloadTask:
        if not allow_duplicate:
            existing = self.find_duplicate(url)
            if existing is not None:
                raise DuplicateError(existing)
        if pr is None:
            pr = await self._probe(url, headers)
        if not allow_duplicate and pr.final_url != url:
            existing = self.find_duplicate(pr.final_url)
            if existing is not None:
                raise DuplicateError(existing)
        name = filename or pr.filename
        task = DownloadTask(
            url=pr.final_url,
            filename=name,
            dest_dir=self.settings.resolve_dest_dir(name, dest_dir),
            size=pr.size,
            resumable=pr.resumable,
            connections=connections or self.settings.connections_per_download,
            headers=dict(headers or {}),
            etag=pr.etag,
            last_modified=pr.last_modified,
            status=Status.QUEUED if start else Status.PAUSED,
            referrer=referrer,
            origin=url if url != pr.final_url else "",
        )
        return self._register(task)

    async def _add_media(
        self,
        info: MediaInfo,
        selector: str,
        dest_dir: str | None,
        filename: str | None,
        headers: dict[str, str] | None,
        start: bool,
        ext: str,
        client: str = "",
        postprocess: str = "",
        thumbnail: str = "",
    ) -> DownloadTask:
        name = filename or f"{info.filename_base}.{ext}"
        task = DownloadTask(
            url=info.url,
            filename=name,
            dest_dir=self.settings.resolve_dest_dir(name, dest_dir),
            size=None,
            resumable=True,
            connections=1,
            headers=dict(headers or {}),
            status=Status.QUEUED if start else Status.PAUSED,
            kind="media",
            media_format=selector,
            media_client=client,
            media_postprocess=postprocess,
            title=info.title,
            thumbnail=thumbnail or (info.thumbnail or ""),
        )
        return self._register(task)

    def _register(self, task: DownloadTask) -> DownloadTask:
        self.tasks[task.id] = task
        self._emit("added", task)
        self._save()
        self._schedule_soon()
        return task

    async def _start_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or task.status in (Status.DOWNLOADING, Status.COMPLETED, Status.QUEUED):
            return
        task.status = Status.QUEUED
        task.error = None
        self._save()
        self._schedule_soon()

    async def _pause_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        runner = self._runners.get(task_id)
        if runner:
            runner.pause()
        elif task.status == Status.QUEUED:
            task.status = Status.PAUSED
            self._save()

    async def _remove_task(self, task_id: str, delete_file: bool) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        runner = self._runners.get(task_id)
        if runner:
            runner.pause()
            for _ in range(100):
                if task_id not in self._runners:
                    break
                await asyncio.sleep(0.1)
        self.tasks.pop(task_id, None)
        try:
            if task.kind == "torrent":
                if self._torrents:
                    self._torrents.forget(task, delete_file)
                if delete_file and task.final_path.exists():
                    import shutil

                    if task.final_path.is_dir():
                        shutil.rmtree(task.final_path, ignore_errors=True)
                    else:
                        task.final_path.unlink()
            if task.part_path.exists():
                task.part_path.unlink()
            if task.kind == "media":
                for leftover in Path(task.dest_dir).glob(f"{Path(task.filename).stem}*.part*"):
                    leftover.unlink()
            if delete_file and task.status == Status.COMPLETED and task.final_path.exists() and task.kind != "torrent":
                task.final_path.unlink()
        except OSError:
            pass
        self._emit("removed", task)
        self._save()
        self._schedule_soon()

    async def _start_all(self) -> None:
        for task in self.tasks.values():
            if task.status in (Status.PAUSED, Status.ERROR):
                task.status = Status.QUEUED
                task.error = None
        self._save()
        self._schedule_soon()

    async def _pause_all(self) -> None:
        for task in self.tasks.values():
            if task.status == Status.QUEUED:
                task.status = Status.PAUSED
        for runner in self._runners.values():
            runner.pause()
        self._save()

    async def _update_settings(self, settings: Settings) -> None:
        old_ua = self.settings.user_agent
        self.settings = settings
        settings.save()
        if settings.user_agent != old_ua and self._session:
            await self._session.close()
            self._session = self._make_session()
        if self._session:
            self._mega = MegaClient(self._session, settings.proxy or None)
        if self._torrents:
            self._torrents.apply_settings(settings)
        self._window_was_open = None  # re-evaluate the scheduler window with the new settings
        self._schedule_check()
        self._schedule_soon()

    async def _wait(self, task_id: str) -> DownloadTask:
        while True:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if task.status not in (Status.QUEUED, Status.DOWNLOADING):
                return task
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------ IPC
    async def _handle_ipc(self, msg: dict[str, Any]) -> dict[str, Any]:
        cmd = msg.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pid": __import__("os").getpid()}
        if cmd == "hello":
            # The browser extension announces itself (on install and on browser start).
            if not self.settings.extension_seen:
                self.settings.extension_seen = True
                self.settings.save()
            self._emit("extension-connected", DownloadTask(url="", filename=str(msg.get("version", "")), dest_dir=""))
            return {"ok": True}
        if cmd in ("add", "show"):
            if self.on_ipc:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(None, self.on_ipc, msg)
                if resp is not None:
                    return resp
            if cmd == "add":
                # Headless fallback (no GUI attached): pages go to yt-dlp, files to the segment engine.
                url = str(msg.get("url", ""))
                headers = {k: v for k, v in (msg.get("headers") or {}).items() if isinstance(v, str)}
                try:
                    task = await self._add_smart(
                        url,
                        None,
                        msg.get("filename") or None,
                        headers,
                        str(msg.get("referrer", "")),
                        str(msg.get("kind") or "auto"),
                    )
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "id": task.id}
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    # ----------------------------------------------------------- scheduling
    def _schedule_soon(self) -> None:
        if self._loop:
            self._loop.call_soon(self._schedule)

    def _schedule(self) -> None:
        if not self._window_open():
            return  # the scheduler window is closed; _schedule_check re-dispatches when it opens
        active = len(self._runners)
        for task in self.tasks.values():
            if active >= self.settings.max_concurrent_downloads:
                break
            if task.status == Status.QUEUED and task.id not in self._runners:
                runner: Runner
                if task.kind == "media":
                    runner = MediaRunner(
                        task,
                        ffmpeg=self.ffmpeg,
                        proxy=self.settings.proxy or None,
                        max_retries=self.settings.max_retries,
                    )
                elif task.kind == "torrent":
                    runner = TorrentRunner(
                        task,
                        self._torrent_engine(),
                        speed_limit=self.settings.speed_limit_bytes,
                        seed=self.settings.torrent_seed,
                        trackers=self.settings.torrent_trackers,
                    )
                elif task.kind == "mega":
                    assert self._session and self._mega
                    runner = MegaRunner(
                        task,
                        self._session,
                        client=self._mega,
                        max_retries=self.settings.max_retries,
                        speed_limit=self.settings.speed_limit_bytes,
                        proxy=self.settings.proxy,
                    )
                else:
                    assert self._session
                    runner = TaskRunner(
                        task,
                        self._session,
                        max_retries=self.settings.max_retries,
                        speed_limit=self.settings.speed_limit_bytes,
                        proxy=self.settings.proxy,
                        auth_header=self.auth_header_for(task.url),
                    )
                self._runners[task.id] = runner
                asyncio.ensure_future(self._run(runner))
                active += 1
        self._arm_save_timer()

    async def _run(self, runner: Runner) -> None:
        task = runner.task
        self._emit("started", task)
        try:
            await runner.run()
        finally:
            self._runners.pop(task.id, None)
            if task.id in self._held:
                self._held.discard(task.id)
                if task.status == Status.PAUSED:
                    task.status = Status.QUEUED  # stopped by the scheduler, not the user: run again later
            self._emit(task.status.value, task)
            self._save()
            self._schedule_soon()
        if task.status == Status.COMPLETED and task.id == getattr(self, "_ffmpeg_task_id", None):
            await self._after_ffmpeg_download(task)

    # ------------------------------------------------------------ scheduler
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock())

    def _window_open(self) -> bool:
        return _schedule.is_open(self.settings, self._now())

    def schedule_note(self) -> str:
        """'' when downloads run freely, else 'starts at 23:00' / 'stops at 07:00' (thread-safe)."""
        return _schedule.describe(self.settings, self._now())

    def _schedule_check(self) -> None:
        """Periodic (loop thread): notice the window opening or closing and act on it."""
        self._sched_timer = None
        open_now = self._window_open()
        if self._window_was_open is None:
            self._window_was_open = open_now
        elif open_now != self._window_was_open:
            self._window_was_open = open_now
            if open_now:
                self._schedule_soon()
            else:
                for tid, runner in list(self._runners.items()):
                    self._held.add(tid)
                    runner.pause()
        if self._loop and self.settings.schedule_enabled:
            self._sched_timer = self._loop.call_later(self.schedule_poll, self._schedule_check)

    def _arm_save_timer(self) -> None:
        if self._save_timer or not self._runners or not self._loop:
            return

        def tick() -> None:
            self._save_timer = None
            if self._runners:
                self._save()
                self._arm_save_timer()

        self._save_timer = self._loop.call_later(2.0, tick)

    # ---------------------------------------------------------- persistence
    def _emit(self, event: str, task: DownloadTask) -> None:
        if self.on_event:
            try:
                self.on_event(event, task)
            except Exception:  # noqa: BLE001
                pass

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        self._interrupted: list[str] = []
        for item in data.get("tasks", []):
            try:
                task = DownloadTask.from_dict(item)
            except (TypeError, ValueError):
                continue
            if item.get("status") in (Status.DOWNLOADING.value, Status.QUEUED.value):
                self._interrupted.append(task.id)  # was running when the app died
            self.tasks[task.id] = task
        if self.settings.auto_resume:
            for tid in self._interrupted:
                t = self.tasks.get(tid)
                if t and t.status == Status.PAUSED:
                    t.status = Status.QUEUED

    def _save(self) -> None:
        data = {"tasks": [t.to_dict() for t in self.tasks.values()]}
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1), "utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass
