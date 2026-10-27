"""Browser integration tests: IPC server, native host framing and forwarding, media (yt-dlp) downloads."""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from idm.engine import DownloadManager, Settings, Status
from idm.engine import ipc as ipc_mod
from idm.engine.media import build_format_choices, extract_media
from idm.gui.add_dialog import AddDownloadDialog
from idm.gui.main_window import MainWindow
from idm.gui.theme import apply_theme

from .test_engine import FILE_SIZE, TestServer
from .test_gui import pump, wait_until

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server():
    s = TestServer().start()
    yield s
    s.stop()


@pytest.fixture
def ipc_dir(tmp_path: Path, monkeypatch):
    """Point the IPC files at a temp dir so tests never touch the real app data."""
    d = tmp_path / "idm-clone"  # same layout as %APPDATA%\idm-clone, so a child process agrees
    d.mkdir()
    monkeypatch.setattr(ipc_mod, "app_data_dir", lambda: d)
    return d


@pytest.fixture
def manager(tmp_path: Path, ipc_dir):
    settings = Settings(download_dir=str(tmp_path / "dl"), connections_per_download=4, use_categories=False)
    m = DownloadManager(settings, state_path=tmp_path / "state.json", enable_ipc=True)
    m.start()
    yield m
    m.stop()


# ------------------------------------------------------------------- IPC
def test_ipc_server_writes_info_and_answers_ping(manager, ipc_dir):
    info = ipc_mod.read_ipc_info()
    assert info and info["port"] == manager.ipc_port and info["pid"] == os.getpid()
    resp = ipc_mod.send({"cmd": "ping"})
    assert resp["ok"] is True


def test_ipc_rejects_bad_token(manager, ipc_dir):
    import socket

    info = ipc_mod.read_ipc_info()
    with socket.create_connection(("127.0.0.1", info["port"]), timeout=5) as sock:
        sock.sendall((json.dumps({"cmd": "ping", "token": "wrong"}) + "\n").encode())
        reply = json.loads(sock.recv(4096).decode().strip())
    assert reply == {"ok": False, "error": "unauthorized"}


def test_ipc_add_headless_downloads_file(manager, ipc_dir, server, tmp_path):
    resp = ipc_mod.send({"cmd": "add", "url": server.url("/file"), "headers": {"Referer": "https://example.com/page"}})
    assert resp["ok"], resp
    done = manager.wait(resp["id"], 60)
    assert done.status == Status.COMPLETED, done.error
    assert done.headers["Referer"] == "https://example.com/page"
    assert any(r.get("Referer") == "https://example.com/page" for r in server.requests)
    assert (tmp_path / "dl" / "payload.bin").stat().st_size == FILE_SIZE


def test_ipc_info_removed_on_stop(tmp_path, ipc_dir):
    m = DownloadManager(Settings(download_dir=str(tmp_path)), state_path=tmp_path / "s.json", enable_ipc=True)
    m.start()
    assert ipc_mod.ipc_info_path().exists()
    m.stop()
    assert not ipc_mod.ipc_info_path().exists()


# ----------------------------------------------------------- native host
def test_native_host_framing(monkeypatch):
    from idm import native_host

    payload = {"cmd": "host-ping"}
    data = json.dumps(payload).encode()
    fake_in = io.BytesIO(struct.pack("<I", len(data)) + data)
    fake_out = io.BytesIO()

    class In:
        buffer = fake_in

    class Out:
        buffer = fake_out

    monkeypatch.setattr(sys, "stdin", In)
    monkeypatch.setattr(sys, "stdout", Out)
    assert native_host.read_message() == payload
    assert native_host.read_message() is None  # EOF
    native_host.write_message({"ok": True})
    raw = fake_out.getvalue()
    (n,) = struct.unpack("<I", raw[:4])
    assert json.loads(raw[4 : 4 + n]) == {"ok": True}


def test_native_host_forwards_to_running_app(manager, ipc_dir, server):
    """Spawn the real host process the way Chrome would and send it an 'add' message."""
    msg = json.dumps({"cmd": "add", "url": server.url("/norange")}).encode()
    framed = struct.pack("<I", len(msg)) + msg
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["APPDATA"] = str(ipc_dir.parent)  # child computes APPDATA/idm-clone == ipc_dir
    proc = subprocess.run(
        [sys.executable, "-m", "idm.native_host"],
        input=framed,
        capture_output=True,
        env=env,
        timeout=60,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    out = proc.stdout
    (n,) = struct.unpack("<I", out[:4])
    reply = json.loads(out[4 : 4 + n])
    assert reply.get("ok") is True, reply
    done = manager.wait(reply["id"], 60)
    assert done.status == Status.COMPLETED, done.error


# ------------------------------------------------------------------ media
def test_build_format_choices_without_ffmpeg():
    formats = [
        {
            "format_id": "18",
            "ext": "mp4",
            "height": 360,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "filesize": 1000,
            "url": "x",
        },
        {
            "format_id": "22",
            "ext": "mp4",
            "height": 720,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "filesize": 5000,
            "url": "x",
        },
        {
            "format_id": "137",
            "ext": "mp4",
            "height": 1080,
            "vcodec": "avc1",
            "acodec": "none",
            "filesize": 9000,
            "url": "x",
        },
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 800, "url": "x"},
    ]
    no_ff = build_format_choices(formats, ffmpeg=False)
    labels = [f.label for f in no_ff]
    assert labels[0].startswith("720p") and "no ffmpeg" in labels[0]
    assert any(f.audio_only for f in no_ff)
    assert not any(f.needs_ffmpeg for f in no_ff)

    with_ff = build_format_choices(formats, ffmpeg=True)
    assert with_ff[0].needs_ffmpeg and "1080p" in with_ff[0].label
    assert any(f.selector.startswith("bestvideo*[height<=720]") for f in with_ff)


def test_extract_and_download_direct_media_link(manager, server, tmp_path):
    """yt-dlp's generic extractor treats a direct media URL as a one-format 'video'."""
    info = extract_media(server.url("/file"))
    assert info.formats, "generic extractor should expose at least one format"
    fmt = info.formats[0]
    task = manager.add_media(info, fmt.selector, filename="clip.bin", ext="bin").result(10)
    assert task.kind == "media"
    done = manager.wait(task.id, 120)
    assert done.status == Status.COMPLETED, done.error
    out = Path(done.dest_dir) / done.filename
    assert out.exists() and out.stat().st_size == FILE_SIZE
    assert done.size == FILE_SIZE and done.progress == 1.0


def test_media_task_survives_persistence(tmp_path):
    from idm.engine.download import DownloadTask

    t = DownloadTask(
        url="https://youtu.be/x", filename="a.mp4", dest_dir=str(tmp_path), kind="media", media_format="best", title="A"
    )
    back = DownloadTask.from_dict(t.to_dict())
    assert back.kind == "media" and back.media_format == "best" and back.title == "A"


# ------------------------------------------------------------ GUI via IPC
@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_browser_add_opens_dialog_with_headers(app, manager, ipc_dir, server, monkeypatch, tmp_path):
    theme = apply_theme(app)
    win = MainWindow(manager, theme)
    win.show()
    seen: dict = {}

    def fake_exec(dlg: AddDownloadDialog) -> int:
        seen["headers"] = dict(dlg.headers)
        seen["kind"] = dlg.kind_hint
        seen["filename"] = dlg.name_edit.text()
        wait_until(app, lambda: dlg.probe_result is not None, 15, "probe inside browser-triggered dialog")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(AddDownloadDialog, "exec", fake_exec)

    reply: dict = {}

    def send_from_browser() -> None:
        reply.update(
            ipc_mod.send(
                {
                    "cmd": "add",
                    "url": server.url("/file"),
                    "filename": "from-browser.bin",
                    "kind": "download",
                    "referrer": "https://example.com/page",
                    "headers": {"Referer": "https://example.com/page", "Cookie": "a=b"},
                }
            )
        )

    t = threading.Thread(target=send_from_browser)
    t.start()
    wait_until(app, lambda: "headers" in seen, 15, "dialog to open from IPC")
    wait_until(app, lambda: win.model.rowCount() == 1, 15, "row from browser add")
    t.join(5)
    assert reply.get("ok") is True
    assert seen["headers"]["Cookie"] == "a=b" and seen["kind"] == "download" and seen["filename"] == "from-browser.bin"
    task = manager.snapshot()[0]
    assert (
        task.filename == "from-browser.bin"
        and task.headers["Cookie"] == "a=b"
        and task.referrer == "https://example.com/page"
    )
    wait_until(app, lambda: task.status == Status.COMPLETED, 60, "browser download completion")
    pump(app, 0.3)
    win._timer.stop()
    manager.on_event = None
    manager.on_ipc = None
    win.close()


def test_install_ffmpeg_from_zip_extracts_binaries(tmp_path, monkeypatch):
    import zipfile

    from idm.engine import media as media_mod

    monkeypatch.setattr(media_mod, "managed_ffmpeg_dir", lambda: tmp_path / "ffmpeg")
    zip_path = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe", b"MZ fake ffmpeg")
        zf.writestr("ffmpeg-master-latest-win64-gpl/bin/ffprobe.exe", b"MZ fake ffprobe")
        zf.writestr("ffmpeg-master-latest-win64-gpl/LICENSE.txt", b"...")
    exe = media_mod.install_ffmpeg_from_zip(zip_path)
    assert Path(exe).name == "ffmpeg.exe" and Path(exe).read_bytes() == b"MZ fake ffmpeg"
    assert (tmp_path / "ffmpeg" / "ffprobe.exe").exists()
    assert media_mod.find_ffmpeg() == exe

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", b"nothing here")
    with pytest.raises(media_mod.MediaError):
        media_mod.install_ffmpeg_from_zip(bad)


def test_add_smart_routes_pages_to_media_and_files_to_engine(manager, server, tmp_path):
    """The bug seen in the wild: an HTML page must never be saved as a 'file'."""
    plain = manager.add_smart(server.url("/file")).result(30)
    assert plain.kind == "file"
    assert manager.wait(plain.id, 60).status == Status.COMPLETED

    page = manager.add_smart(server.url("/page")).result(120)
    assert page.kind == "media", "HTML page should be analysed by yt-dlp, not downloaded as a file"
    done = manager.wait(page.id, 120)
    assert done.status == Status.COMPLETED, done.error
    out = Path(done.dest_dir) / done.filename
    assert out.stat().st_size == FILE_SIZE
    assert not out.read_bytes().startswith(b"<html"), "must contain the video bytes, not the page"

    forced = manager.add_smart(server.url("/page"), kind="file", filename="page.html").result(30)
    assert forced.kind == "file"  # explicit 'file' still wins when the caller insists
