"""Add-dialog flows for the newer sources: magnet links, local .torrent files, MEGA links and
video thumbnails. Runs the real dialog against local stand-in services (headless Qt)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog

import idm.engine.manager as manager_mod
from idm.engine import DownloadManager, MediaFormat, MediaInfo, Settings, Status
from idm.gui.add_dialog import AddDownloadDialog
from idm.gui.main_window import MainWindow
from idm.gui.models import TaskFilter
from idm.gui.theme import apply_theme
from tests.test_engine import DIGEST, LocalServer
from tests.test_gui import pump, select_filter, sidebar_counts, visible_names, wait_until
from tests.test_mega import FILE_LINK, FILE_SIZE, PLAIN, FakeMega
from tests.test_torrent import PAYLOAD, Seeder

pytest.importorskip("libtorrent")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


@pytest.fixture
def fake_mega(monkeypatch):
    import idm.engine.mega as mega_mod

    srv = FakeMega().start()
    monkeypatch.setattr(mega_mod, "API_URL", srv.url("/cs"))
    yield srv
    srv.stop()


@pytest.fixture
def seeder(tmp_path: Path):
    s = Seeder(tmp_path)
    yield s
    s.stop()


@pytest.fixture
def window(app, tmp_path: Path):
    settings = Settings(
        download_dir=str(tmp_path / "dl"),
        connections_per_download=4,
        use_categories=True,
        first_run_done=True,
        check_updates=False,
        torrent_port=0,
        torrent_trackers=[],
    )
    mgr = DownloadManager(settings, state_path=tmp_path / "state.json")
    mgr.start()
    theme = apply_theme(app)
    win = MainWindow(mgr, theme)
    win.show()
    yield win
    win._timer.stop()
    mgr.on_event = None
    mgr.stop()
    win.close()


def run_dialog(app, win: MainWindow, url: str, ready, monkeypatch, *, paused: bool = False, tweak=None, kind="auto"):
    """Open the real add dialog for `url`, wait until `ready(dlg)` is true, optionally tweak it, accept."""
    captured: dict[str, AddDownloadDialog] = {}

    def fake_exec(dlg: AddDownloadDialog) -> int:
        captured["dlg"] = dlg
        wait_until(app, lambda: ready(dlg), 20, "add dialog detection")
        assert dlg.start_btn.isEnabled(), "Start button must enable once detection succeeds"
        if tweak:
            tweak(dlg)
        if paused:
            dlg.start_now = False
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(AddDownloadDialog, "exec", fake_exec)
    win.add_url(url, kind=kind)
    assert "dlg" in captured
    return captured["dlg"]


# ------------------------------------------------------------------------- torrents
def test_magnet_link_through_dialog(app, window, seeder, monkeypatch, tmp_path):
    dlg = run_dialog(app, window, seeder.magnet, lambda d: d.source_kind == "magnet", monkeypatch)
    assert dlg.card_title.text() == "payload.bin"
    assert "Magnet" in dlg.card_sub.text()
    assert not dlg.name_edit.isVisibleTo(dlg)  # the name comes from the torrent itself
    assert "Torrents" in dlg.use_category.text()

    wait_until(app, lambda: any(t.status == Status.COMPLETED for t in window.manager.snapshot()), 90, "torrent done")
    pump(app, 0.6)
    task = window.manager.snapshot()[0]
    assert task.kind == "torrent" and task.filename == "payload.bin"
    assert (tmp_path / "dl" / "Torrents" / "payload.bin").read_bytes() == PAYLOAD
    counts = sidebar_counts(window)
    assert counts[TaskFilter.TORRENTS] == 1 and counts[TaskFilter.DONE] == 1
    assert counts.get("Documents", 0) == 0  # extension-less torrent name must not leak into a category
    select_filter(window, TaskFilter.TORRENTS)
    assert visible_names(window) == ["payload.bin"]


def test_local_torrent_file_through_dialog(app, window, seeder, monkeypatch, tmp_path):
    tfile = tmp_path / "payload.torrent"
    tfile.write_bytes(seeder.torrent_bytes)
    dlg = run_dialog(
        app,
        window,
        str(tfile),
        lambda d: d.torrent_desc is not None and d.source_kind == "torrent",
        monkeypatch,
        paused=True,
    )
    assert dlg.card_title.text() == "payload.bin"
    assert "Torrent" in dlg.card_sub.text() and "MB" in dlg.card_sub.text()
    wait_until(app, lambda: window.manager.snapshot(), 10, "task added")
    task = window.manager.snapshot()[0]
    assert task.status == Status.PAUSED and task.size == len(PAYLOAD) and task.kind == "torrent"


def test_dropping_a_torrent_file_opens_the_dialog(app, window, seeder, monkeypatch, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl

    tfile = tmp_path / "payload.torrent"
    tfile.write_bytes(seeder.torrent_bytes)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tfile))])
    assert [Path(p) for p in window._urls_from_mime(mime)] == [tfile]
    magnet_mime = QMimeData()
    magnet_mime.setText(seeder.magnet)
    assert window._urls_from_mime(magnet_mime) == [seeder.magnet]


# ----------------------------------------------------------------------------- MEGA
def test_mega_link_through_dialog(app, window, fake_mega, monkeypatch, tmp_path):
    dlg = run_dialog(app, window, FILE_LINK, lambda d: d.source_kind == "mega", monkeypatch)
    assert dlg.card_title.text() == "report.pdf"
    assert "MEGA" in dlg.card_sub.text()
    assert dlg.name_edit.text() == "report.pdf"
    assert "Documents" in dlg.use_category.text()

    wait_until(app, lambda: any(t.status == Status.COMPLETED for t in window.manager.snapshot()), 60, "mega done")
    pump(app, 0.6)
    task = window.manager.snapshot()[0]
    assert task.kind == "mega" and task.size == FILE_SIZE
    out = tmp_path / "dl" / "Documents" / "report.pdf"
    assert hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(PLAIN).hexdigest()
    assert sidebar_counts(window)["Documents"] == 1


def test_bad_mega_link_reports_error(app, window, fake_mega, monkeypatch):
    captured = {}

    def fake_exec(dlg: AddDownloadDialog) -> int:
        captured["dlg"] = dlg
        wait_until(app, lambda: "Could not" in dlg.card_title.text(), 20, "error card")
        assert not dlg.start_btn.isEnabled()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(AddDownloadDialog, "exec", fake_exec)
    window.add_url("https://mega.nz/file/NoSuchFi#" + "A" * 43)
    assert "not found" in captured["dlg"].card_sub.text()


# ----------------------------------------------------------------------- thumbnails
def _fake_media(server: LocalServer):
    def extract(url, headers=None, proxy=None, ffmpeg=None):
        return MediaInfo(
            url=server.url("/file"),
            title="Clip title",
            extractor="generic",
            duration=12.0,
            thumbnail=server.url("/file"),
            formats=[
                MediaFormat("best", "360p · mp4", "mp4", height=360),
                MediaFormat("__thumbnail__", "Thumbnail image only · bin", "bin", thumbnail=True),
            ],
        )

    return extract


def test_thumbnail_only_download(app, window, server, monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod, "extract_media", _fake_media(server))

    def pick_thumbnail(dlg: AddDownloadDialog) -> None:
        idx = next(i for i in range(dlg.quality.count()) if dlg.quality.itemData(i).thumbnail)
        dlg.quality.setCurrentIndex(idx)
        assert dlg.name_edit.text() == "Clip title.bin"
        assert not dlg.save_thumb.isVisibleTo(dlg)  # pointless when the thumbnail *is* the download

    run_dialog(app, window, server.url("/page"), lambda d: d.media_info is not None, monkeypatch, tweak=pick_thumbnail)
    wait_until(app, lambda: any(t.status == Status.COMPLETED for t in window.manager.snapshot()), 60, "thumb done")
    task = window.manager.snapshot()[0]
    assert task.kind == "file" and task.filename == "Clip title.bin"
    assert hashlib.sha256(task.final_path.read_bytes()).hexdigest() == DIGEST


def test_video_plus_thumbnail_checkbox_adds_two_downloads(app, window, server, monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod, "extract_media", _fake_media(server))

    def tick_thumb(dlg: AddDownloadDialog) -> None:
        assert dlg.save_thumb.isVisibleTo(dlg)
        dlg.save_thumb.setChecked(True)

    run_dialog(app, window, server.url("/page"), lambda d: d.media_info is not None, monkeypatch, tweak=tick_thumb)
    wait_until(app, lambda: len(window.manager.snapshot()) == 2, 20, "two tasks")
    wait_until(
        app, lambda: all(t.status == Status.COMPLETED for t in window.manager.snapshot()), 90, "both downloads done"
    )
    kinds = sorted((t.kind, t.filename) for t in window.manager.snapshot())
    assert [k for k, _ in kinds] == ["file", "media"]
    assert kinds[0][1] == "Clip title.jpg"  # thumbnail gets an image extension and the video's name
    assert kinds[1][1].startswith("Clip title")
    media = next(t for t in window.manager.snapshot() if t.kind == "media")
    assert media.thumbnail == server.url("/file")  # remembered for "Save thumbnail image" later
