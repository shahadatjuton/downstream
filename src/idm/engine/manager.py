"""DownloadManager: queue, concurrency, persistence, browser IPC. Runs an asyncio loop on
its own thread so a synchronous GUI (or CLI) can drive it through thread-safe methods."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import aiohttp

from .download import DownloadTask, Status, TaskRunner
from .ipc import IpcServer, app_path_file
from .media import MediaInfo, MediaRunner, extract_media, find_ffmpeg
from .probe import ProbeResult, probe
from .settings import Settings, app_data_dir

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

        self._runners: dict[str, TaskRunner | MediaRunner] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: aiohttp.ClientSession | None = None
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
        if self.enable_ipc:
            self._ipc = IpcServer(self._handle_ipc)
            try:
                await self._ipc.start()
            except OSError:
                self._ipc = None
        asyncio.get_running_loop().call_soon(self._schedule_soon)

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

    def probe(self, url: str, headers: dict[str, str] | None = None) -> concurrent.futures.Future:
        """Returns a Future[ProbeResult]."""
        return self._call(self._probe(url, headers))

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
    ) -> concurrent.futures.Future:
        """Returns a Future[DownloadTask]."""
        return self._call(self._add(url, dest_dir, filename, connections, headers, start, probe_result, referrer))

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
    ) -> concurrent.futures.Future:
        """Returns a Future[DownloadTask] for a page video/audio download."""
        return self._call(self._add_media(info, format_selector, dest_dir, filename, headers, start, ext, client))

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
        """Headless add that never saves a web page as a file: HTML pages go through yt-dlp
        (best available quality), everything else is a normal segmented download.
        Returns Future[DownloadTask]."""
        return self._call(self._add_smart(url, dest_dir, filename, headers, referrer, kind, start))

    async def _add_smart(self, url, dest_dir, filename, headers, referrer, kind, start: bool = True) -> DownloadTask:
        from .media import looks_like_media_page

        pr: ProbeResult | None = None
        is_page = kind == "media"
        if not is_page:
            try:
                pr = await self._probe(url, headers)
                is_page = kind not in ("file", "download") and looks_like_media_page(pr.final_url, pr.content_type)
            except Exception:
                if kind in ("file", "download") or not looks_like_media_page(url, ""):
                    raise
                is_page = True
        if not is_page:
            return await self._add(url, dest_dir, filename, None, headers, start, pr, referrer)
        info = await self._extract_media(url, headers)
        fmt = info.formats[0]
        return await self._add_media(info, fmt.selector, dest_dir, filename, headers, start, fmt.ext, fmt.client)

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

    async def _fetch_bytes(self, url: str, limit: int) -> bytes:
        assert self._session
        async with self._session.get(url, proxy=self.settings.proxy or None) as resp:
            resp.raise_for_status()
            return await resp.content.read(limit)

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
        return await probe(self._session, url, headers, proxy=self.settings.proxy)

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
    ) -> DownloadTask:
        if pr is None:
            pr = await self._probe(url, headers)
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
            title=info.title,
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
            if task.part_path.exists():
                task.part_path.unlink()
            if task.kind == "media":
                for leftover in Path(task.dest_dir).glob(f"{Path(task.filename).stem}*.part*"):
                    leftover.unlink()
            if delete_file and task.status == Status.COMPLETED and task.final_path.exists():
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
        active = len(self._runners)
        for task in self.tasks.values():
            if active >= self.settings.max_concurrent_downloads:
                break
            if task.status == Status.QUEUED and task.id not in self._runners:
                runner: TaskRunner | MediaRunner
                if task.kind == "media":
                    runner = MediaRunner(
                        task,
                        ffmpeg=self.ffmpeg,
                        proxy=self.settings.proxy or None,
                        max_retries=self.settings.max_retries,
                    )
                else:
                    assert self._session
                    runner = TaskRunner(
                        task,
                        self._session,
                        max_retries=self.settings.max_retries,
                        speed_limit=self.settings.speed_limit_bytes,
                        proxy=self.settings.proxy,
                    )
                self._runners[task.id] = runner
                asyncio.ensure_future(self._run(runner))
                active += 1
        self._arm_save_timer()

    async def _run(self, runner: TaskRunner | MediaRunner) -> None:
        task = runner.task
        self._emit("started", task)
        try:
            await runner.run()
        finally:
            self._runners.pop(task.id, None)
            self._emit(task.status.value, task)
            self._save()
            self._schedule_soon()
        if task.status == Status.COMPLETED and task.id == getattr(self, "_ffmpeg_task_id", None):
            await self._after_ffmpeg_download(task)

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
