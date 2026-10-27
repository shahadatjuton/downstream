"""Engine tests against a local aiohttp server."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web

from idm.engine import DownloadManager, Settings, Status

FILE_SIZE = 6 * 1024 * 1024 + 12345
PAYLOAD = os.urandom(FILE_SIZE)
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    if not value.startswith("bytes="):
        return None
    spec = value[6:].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    if start_s == "":
        n = int(end_s)
        return max(0, size - n), size - 1
    start = int(start_s)
    end = int(end_s) if end_s else size - 1
    if start >= size:
        return None
    return start, min(end, size - 1)


class TestServer:
    """Serves PAYLOAD at /file (Range-aware) and /norange (ignores Range), with an optional delay."""

    def __init__(self) -> None:
        self.delay = 0.0
        self.requests: list[dict[str, str]] = []
        self.port = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    async def _file(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(dict(request.headers))
        rng = request.headers.get("Range")
        parsed = _parse_range(rng, FILE_SIZE) if rng else None
        if parsed:
            start, end = parsed
            resp = web.StreamResponse(
                status=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{FILE_SIZE}",
                    "Content-Length": str(end - start + 1),
                    "Accept-Ranges": "bytes",
                    "Content-Disposition": 'attachment; filename="payload.bin"',
                    "ETag": '"abc"',
                },
            )
        else:
            start, end = 0, FILE_SIZE - 1
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Length": str(FILE_SIZE), "Accept-Ranges": "bytes", "ETag": '"abc"'},
            )
        await resp.prepare(request)
        pos = start
        while pos <= end:
            chunk = PAYLOAD[pos : min(pos + 65536, end + 1)]
            await resp.write(chunk)
            pos += len(chunk)
            if self.delay:
                await asyncio.sleep(self.delay)
        return resp

    async def _norange(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(dict(request.headers))
        resp = web.StreamResponse(status=200, headers={"Content-Length": str(FILE_SIZE)})
        await resp.prepare(request)
        for pos in range(0, FILE_SIZE, 65536):
            await resp.write(PAYLOAD[pos : pos + 65536])
        return resp

    async def _page(self, request: web.Request) -> web.Response:
        """An HTML page embedding the file as a <video>: what a 'download this page' mistake looks like."""
        self.requests.append(dict(request.headers))
        html = (
            f'<html><head><title>Clip page</title></head><body><video src="{self.url("/file")}"></video></body></html>'
        )
        return web.Response(text=html, content_type="text/html")

    async def _chunked(self, request: web.Request) -> web.StreamResponse:
        """Unknown length: no Content-Length, no ranges."""
        self.requests.append(dict(request.headers))
        resp = web.StreamResponse(status=200)
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for pos in range(0, FILE_SIZE, 65536):
            await resp.write(PAYLOAD[pos : pos + 65536])
        return resp

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        app = web.Application()
        app.router.add_get("/file", self._file)
        app.router.add_get("/norange", self._norange)
        app.router.add_get("/chunked", self._chunked)
        app.router.add_get("/page", self._page)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(runner.cleanup())
            loop.close()

    def start(self) -> TestServer:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()
        return self

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture
def server():
    s = TestServer().start()
    yield s
    s.stop()


@pytest.fixture
def manager(tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), connections_per_download=6, use_categories=False)
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.start()
    yield m
    m.stop()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_probe_detects_ranges_and_filename(server, manager):
    pr = manager.probe(server.url("/file")).result(10)
    assert pr.size == FILE_SIZE
    assert pr.resumable is True
    assert pr.filename == "payload.bin"

    pr2 = manager.probe(server.url("/norange")).result(10)
    assert pr2.size == FILE_SIZE
    assert pr2.resumable is False
    assert pr2.filename == "norange"


def test_segmented_download_is_correct(server, manager, tmp_path):
    task = manager.add(server.url("/file")).result(10)
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    out = tmp_path / "dl" / "payload.bin"
    assert out.exists()
    assert not (tmp_path / "dl" / "payload.bin.part").exists()
    assert sha256_of(out) == DIGEST
    # Multiple Range requests were made (probe + at least 2 segments).
    ranged = [r for r in server.requests if "Range" in r]
    assert len(ranged) >= 3


def test_download_without_range_support(server, manager, tmp_path):
    task = manager.add(server.url("/norange")).result(10)
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert sha256_of(tmp_path / "dl" / "norange") == DIGEST


def test_download_unknown_length(server, manager, tmp_path):
    task = manager.add(server.url("/chunked")).result(10)
    assert task.size is None
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert done.size == FILE_SIZE
    assert sha256_of(tmp_path / "dl" / "chunked") == DIGEST


def test_pause_and_resume(server, manager, tmp_path):
    server.delay = 0.02  # slow it down so we can pause mid-flight
    task = manager.add(server.url("/file")).result(10)
    deadline = time.time() + 20
    while task.downloaded < 512 * 1024 and time.time() < deadline:
        time.sleep(0.05)
    assert task.downloaded > 0
    manager.pause_task(task.id).result(10)
    paused = manager.wait(task.id, 20)
    assert paused.status == Status.PAUSED
    downloaded_at_pause = paused.downloaded
    assert 0 < downloaded_at_pause < FILE_SIZE
    assert (tmp_path / "dl" / "payload.bin.part").exists()

    server.delay = 0.0
    before = len(server.requests)
    manager.start_task(task.id).result(10)
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST
    # Resume requests must start from non-zero offsets, not restart from scratch.
    resumed = [r["Range"] for r in server.requests[before:] if "Range" in r]
    assert resumed and all(not r.startswith("bytes=0-") for r in resumed)


def test_state_survives_restart(server, tmp_path):
    settings = Settings(download_dir=str(tmp_path / "dl"), connections_per_download=4, use_categories=False)
    state = tmp_path / "state.json"
    server.delay = 0.02

    m1 = DownloadManager(settings, state_path=state)
    m1.start()
    task = m1.add(server.url("/file")).result(10)
    deadline = time.time() + 20
    while task.downloaded < 256 * 1024 and time.time() < deadline:
        time.sleep(0.05)
    m1.stop()  # pauses and persists

    server.delay = 0.0
    m2 = DownloadManager(settings, state_path=state)
    m2.start()
    try:
        tasks = m2.snapshot()
        assert len(tasks) == 1 and tasks[0].id == task.id
        assert tasks[0].status == Status.PAUSED
        assert tasks[0].downloaded > 0
        m2.start_task(task.id).result(10)
        done = m2.wait(task.id, 60)
        assert done.status == Status.COMPLETED, done.error
        assert sha256_of(tmp_path / "dl" / "payload.bin") == DIGEST
    finally:
        m2.stop()


def test_remove_deletes_part_file(server, manager, tmp_path):
    server.delay = 0.05
    task = manager.add(server.url("/file")).result(10)
    deadline = time.time() + 20
    while task.downloaded == 0 and time.time() < deadline:
        time.sleep(0.05)
    manager.remove_task(task.id).result(20)
    assert not manager.snapshot()
    assert not (tmp_path / "dl" / "payload.bin.part").exists()


def test_bad_url_errors(server, manager):
    task = manager.add(server.url("/file"), probe_result=None).result(10)
    manager.wait(task.id, 60)
    fut = manager.add(server.url("/missing"))
    with pytest.raises(Exception):  # noqa: B017 - aiohttp raises ClientResponseError for the 404
        fut.result(10)


def test_duplicate_filename_gets_suffix(server, manager, tmp_path):
    t1 = manager.add(server.url("/norange")).result(10)
    manager.wait(t1.id, 60)
    t2 = manager.add(server.url("/norange")).result(10)
    done = manager.wait(t2.id, 60)
    assert done.status == Status.COMPLETED
    assert done.filename == "norange (1)"
    assert (tmp_path / "dl" / "norange (1)").exists()
