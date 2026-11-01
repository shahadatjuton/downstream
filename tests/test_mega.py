"""MEGA support, tested against a local stand-in for MEGA's API and storage servers.

The fake serves a file link and a folder link the way MEGA does: encrypted attributes,
AES-CTR ciphertext with Range support, and temporary download URLs. The real crypto code
runs end to end; only the network endpoints are local.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path

import pytest
from aiohttp import web

import idm.engine.mega as mega_mod
from idm.engine import DownloadManager, Settings, Status
from idm.engine.mega import (
    MegaClient,
    MegaError,
    b64encode,
    ctr_decryptor,
    ctr_encrypt,
    decrypt_attributes,
    encrypt_attributes,
    is_mega_link,
    parse_link,
    unpack_file_key,
)

FILE_SIZE = 2 * 1024 * 1024 + 4321
PLAIN = os.urandom(FILE_SIZE)
FILE_KEY = os.urandom(32)
CIPHER = ctr_encrypt(FILE_KEY, PLAIN)
FILE_HANDLE = "FiLeHaNd"

FOLDER_KEY = os.urandom(16)
FOLDER_HANDLE = "FoLdErHa"
SUB_HANDLE = "SuBfOlDr"
INNER_HANDLE = "InNeRfIl"
INNER_KEY = os.urandom(32)
INNER_PLAIN = os.urandom(70_000)
INNER_CIPHER = ctr_encrypt(INNER_KEY, INNER_PLAIN)


def _ecb(key: bytes, data: bytes) -> bytes:
    from Crypto.Cipher import AES

    return AES.new(key, AES.MODE_ECB).encrypt(data)


class FakeMega:
    """POST /cs answers the three API calls we use; GET /dl/<handle> serves ciphertext with Range support."""

    def __init__(self) -> None:
        self.port = 0
        self.calls: list[dict] = []
        self.quota_exceeded = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _cs(self, request: web.Request) -> web.Response:
        body = await request.json()
        req = body[0]
        self.calls.append({"req": req, "n": request.query.get("n")})
        if self.quota_exceeded:
            return web.json_response([-17])
        a = req.get("a")
        if a == "g" and req.get("p") == FILE_HANDLE:
            aes_key, _ = unpack_file_key(FILE_KEY)
            return web.json_response(
                [{"s": FILE_SIZE, "at": encrypt_attributes({"n": "report.pdf"}, aes_key), "g": self.url("/dl/file")}]
            )
        if a == "g" and req.get("n") == INNER_HANDLE and request.query.get("n") == FOLDER_HANDLE:
            return web.json_response([{"s": len(INNER_PLAIN), "g": self.url("/dl/inner")}])
        if a == "f" and request.query.get("n") == FOLDER_HANDLE:
            sub_key = os.urandom(16)
            nodes = [
                {
                    "h": FOLDER_HANDLE,
                    "p": "",
                    "t": 1,
                    "k": f"{FOLDER_HANDLE}:{b64encode(_ecb(FOLDER_KEY, FOLDER_KEY))}",
                    "a": encrypt_attributes({"n": "Shared stuff"}, FOLDER_KEY),
                },
                {
                    "h": SUB_HANDLE,
                    "p": FOLDER_HANDLE,
                    "t": 1,
                    "k": f"{FOLDER_HANDLE}:{b64encode(_ecb(FOLDER_KEY, sub_key))}",
                    "a": encrypt_attributes({"n": "Photos"}, sub_key),
                },
                {
                    "h": INNER_HANDLE,
                    "p": SUB_HANDLE,
                    "t": 0,
                    "s": len(INNER_PLAIN),
                    "k": f"{FOLDER_HANDLE}:{b64encode(_ecb(FOLDER_KEY, INNER_KEY))}",
                    "a": encrypt_attributes({"n": "holiday.jpg"}, unpack_file_key(INNER_KEY)[0]),
                },
            ]
            return web.json_response([{"f": nodes}])
        return web.json_response([-9])

    async def _dl(self, request: web.Request) -> web.StreamResponse:
        data = CIPHER if request.match_info["which"] == "file" else INNER_CIPHER
        size = len(data)
        rng = request.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
            end = min(end, size - 1)
            resp = web.StreamResponse(
                status=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Length": str(end - start + 1),
                    "Accept-Ranges": "bytes",
                },
            )
        else:
            start, end = 0, size - 1
            resp = web.StreamResponse(status=200, headers={"Content-Length": str(size), "Accept-Ranges": "bytes"})
        await resp.prepare(request)
        pos = start
        while pos <= end:
            chunk = data[pos : min(pos + 65536, end + 1)]
            await resp.write(chunk)
            pos += len(chunk)
        return resp

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        app = web.Application()
        app.router.add_post("/cs", self._cs)
        app.router.add_get("/dl/{which}", self._dl)
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

    def start(self) -> FakeMega:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()
        return self

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)


@pytest.fixture
def fake(monkeypatch):
    srv = FakeMega().start()
    monkeypatch.setattr(mega_mod, "API_URL", srv.url("/cs"))
    yield srv
    srv.stop()


@pytest.fixture
def manager(tmp_path: Path, fake):
    settings = Settings(download_dir=str(tmp_path / "dl"), connections_per_download=5, use_categories=False)
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.start()
    yield m
    m.stop()


FILE_LINK = f"https://mega.nz/file/{FILE_HANDLE}#{b64encode(FILE_KEY)}"
FOLDER_LINK = f"https://mega.nz/folder/{FOLDER_HANDLE}#{b64encode(FOLDER_KEY)}"


# ------------------------------------------------------------------ pure crypto / parsing
def test_link_parsing_covers_new_and_legacy_forms():
    assert parse_link(FILE_LINK) == ("file", FILE_HANDLE, b64encode(FILE_KEY), "")
    legacy = f"https://mega.nz/#!{FILE_HANDLE}!{b64encode(FILE_KEY)}"
    assert parse_link(legacy)[:2] == ("file", FILE_HANDLE)
    assert parse_link(f"{FOLDER_LINK}/file/{INNER_HANDLE}") == (
        "folder",
        FOLDER_HANDLE,
        b64encode(FOLDER_KEY),
        INNER_HANDLE,
    )
    assert parse_link(f"https://mega.nz/#F!{FOLDER_HANDLE}!{b64encode(FOLDER_KEY)}")[:2] == ("folder", FOLDER_HANDLE)
    assert is_mega_link(FILE_LINK) and is_mega_link(FOLDER_LINK)
    assert not is_mega_link("https://example.com/file/abc")
    with pytest.raises(MegaError):
        parse_link(f"https://mega.nz/file/{FILE_HANDLE}")  # key missing


def test_attribute_roundtrip_and_wrong_key():
    aes_key, _ = unpack_file_key(FILE_KEY)
    assert decrypt_attributes(encrypt_attributes({"n": "a b.txt"}, aes_key), aes_key) == {"n": "a b.txt"}
    with pytest.raises(MegaError):
        decrypt_attributes(encrypt_attributes({"n": "x"}, aes_key), os.urandom(16))


def test_ctr_decryptor_is_seekable_at_unaligned_offsets():
    out = bytearray(FILE_SIZE)
    pos = 0
    step = 100_003  # deliberately not a multiple of 16
    while pos < FILE_SIZE:
        dec = ctr_decryptor(FILE_KEY, pos)
        out[pos : pos + step] = dec(CIPHER[pos : pos + step])
        pos += step
    assert bytes(out) == PLAIN


# ------------------------------------------------------------------ against the fake service
def test_resolve_file_and_folder_links(manager, fake):
    files = manager.resolve_mega(FILE_LINK).result(10)
    assert [(f.name, f.size) for f in files] == [("report.pdf", FILE_SIZE)]
    assert files[0].url == fake.url("/dl/file")

    folder = manager.resolve_mega(FOLDER_LINK).result(10)
    assert [(f.name, f.size, f.path) for f in folder] == [("holiday.jpg", len(INNER_PLAIN), "Photos")]
    assert folder[0].key == INNER_KEY
    assert folder[0].link == f"{FOLDER_LINK}/file/{INNER_HANDLE}"

    one = manager.resolve_mega(f"{FOLDER_LINK}/file/{INNER_HANDLE}").result(10)
    assert len(one) == 1 and one[0].handle == INNER_HANDLE


def test_mega_file_downloads_segmented_and_decrypted(manager, fake, tmp_path):
    tasks = manager.add_mega(FILE_LINK).result(10)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.kind == "mega" and task.filename == "report.pdf" and task.size == FILE_SIZE
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error
    out = tmp_path / "dl" / "report.pdf"
    assert hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(PLAIN).hexdigest()
    # several connections were used, and the download URL was refreshed before starting
    assert len(done.segments) > 1
    assert sum(1 for c in fake.calls if c["req"].get("a") == "g") >= 2


def test_mega_folder_link_queues_every_file_into_subfolders(manager, fake, tmp_path):
    tasks = manager.add_mega(FOLDER_LINK).result(10)
    assert [t.filename for t in tasks] == ["holiday.jpg"]
    done = manager.wait(tasks[0].id, 60)
    assert done.status == Status.COMPLETED, done.error
    assert (tmp_path / "dl" / "Photos" / "holiday.jpg").read_bytes() == INNER_PLAIN
    # the URL refresh used the folder-scoped lookup
    assert any(c["req"].get("n") == INNER_HANDLE and c["n"] == FOLDER_HANDLE for c in fake.calls)


def test_add_smart_routes_mega_links(manager, fake, tmp_path):
    task = manager.add_smart(FILE_LINK).result(10)
    assert task.kind == "mega"
    done = manager.wait(task.id, 60)
    assert done.status == Status.COMPLETED, done.error


def test_quota_error_is_explained(manager, fake):
    fake.quota_exceeded = True
    with pytest.raises(Exception, match="quota"):
        manager.resolve_mega(FILE_LINK).result(10)


def test_task_survives_restart_and_refreshes_url(tmp_path, fake):
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False, auto_resume=False)
    state = tmp_path / "state.json"
    m = DownloadManager(settings, state_path=state)
    m.start()
    try:
        task = m.add_mega(FILE_LINK, start=False).result(10)[0]
        task_id = task.id
    finally:
        m.stop()
    m2 = DownloadManager(Settings.load(), state_path=state)
    m2.settings = settings
    m2.start()
    try:
        restored = m2.tasks[task_id]
        assert restored.kind == "mega" and restored.crypto_key == b64encode(FILE_KEY) and restored.origin == FILE_LINK
        m2.start_task(task_id).result(5)
        done = m2.wait(task_id, 60)
        assert done.status == Status.COMPLETED, done.error
        assert (tmp_path / "dl" / "report.pdf").read_bytes() == PLAIN
    finally:
        m2.stop()


async def test_client_rejects_folder_link_without_file_part(fake):
    import aiohttp

    async with aiohttp.ClientSession() as session:
        client = MegaClient(session)
        with pytest.raises(MegaError):
            await client.download_url(FOLDER_LINK)
