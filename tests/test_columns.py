"""Status / Description columns, short error text, and duplicate-link detection."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt

from idm.engine import DownloadManager, DownloadTask, DuplicateError, Settings, Status
from idm.gui.models import COL_DESC, COL_STATUS, COLUMNS, description_text, status_text
from idm.gui.util import short_error

from .test_engine import LocalServer


@pytest.fixture
def server():
    s = LocalServer().start()
    yield s
    s.stop()


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    from idm.gui.main_window import MainWindow
    from idm.gui.theme import apply_theme

    settings = Settings(download_dir=str(tmp_path / "dl"), first_run_done=True, check_updates=False)
    mgr = DownloadManager(settings, state_path=tmp_path / "state.json")
    mgr.start()
    win = MainWindow(mgr, apply_theme(app))
    win.show()
    yield win
    win._timer.stop()
    mgr.on_event = None
    mgr.stop()
    win.close()


@pytest.fixture
def manager(tmp_path: Path):
    settings = Settings(download_dir=str(tmp_path / "dl"), connections_per_download=4, use_categories=False)
    m = DownloadManager(settings, state_path=tmp_path / "state.json")
    m.start()
    yield m
    m.stop()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ClientResponseError: 403, message='Forbidden', url='https://x/y'", "HTTP 403 Forbidden"),
        ("DownloadError: server did not honour Range request (HTTP 416)", "HTTP 416"),
        ("TimeoutError: ", "Timed out"),
        ("ClientConnectorError: Cannot connect to host x:443 ssl:default", "Cannot connect"),
        ("OSError: [Errno 28] No space left on device", "Disk full"),
        ("PermissionError: [Errno 13] Permission denied: 'C:/x'", "Access denied"),
        (None, ""),
        ("MediaError: something long " + "x" * 60, "something long " + "x" * 24 + "…"),
    ],
)
def test_short_error(raw, expected):
    assert short_error(raw) == expected


def test_status_and_description_text():
    t = DownloadTask(url="https://www.example.com/a/b.zip", filename="b.zip", dest_dir="d")
    assert status_text(t) == "Queued"
    assert description_text(t) == "example.com"
    t.status = Status.ERROR
    t.error = "ClientResponseError: 404, message='Not Found', url='https://www.example.com/a/b.zip'"
    assert status_text(t) == "Error · HTTP 404 Not Found"
    t.title = "A lovely video"
    assert description_text(t) == "A lovely video"
    assert (
        description_text(DownloadTask(url="magnet:?xt=urn:btih:abc", filename="x", dest_dir="d", kind="torrent"))
        == "Torrent"
    )
    assert COLUMNS.index("Status") == COL_STATUS and COLUMNS.index("Description") == COL_DESC


def test_duplicate_link_is_refused_unless_allowed(server, manager):
    first = manager.add(server.url("/file"), start=False).result(10)
    with pytest.raises(DuplicateError) as info:
        manager.add(server.url("/file"), start=False, allow_duplicate=False).result(10)
    assert info.value.task.id == first.id
    assert "already in the list" in str(info.value)
    assert manager.find_duplicate(server.url("/file")) is first

    second = manager.add(server.url("/file"), start=False, filename="copy.bin").result(10)
    assert second.id != first.id
    assert len(manager.snapshot()) == 2


def test_duplicate_of_completed_download_mentions_it(server, manager):
    task = manager.add(server.url("/file")).result(10)
    assert manager.wait(task.id, 60).status == Status.COMPLETED
    with pytest.raises(DuplicateError) as info:
        manager.add(server.url("/file"), allow_duplicate=False).result(10)
    assert "already downloaded" in str(info.value)


def test_table_model_columns(server, tmp_path):
    from PySide6.QtWidgets import QApplication

    from idm.gui.models import TaskTableModel
    from idm.gui.theme import apply_theme

    app = QApplication.instance() or QApplication([])
    theme = apply_theme(app)
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False)
    mgr = DownloadManager(settings, state_path=tmp_path / "state.json")
    mgr.start()
    try:
        mgr.add(server.url("/file"), start=False).result(10)
        model = TaskTableModel(mgr, theme)
        model.refresh()
        assert model.columnCount() == len(COLUMNS)
        assert model.index(0, COL_STATUS).data(Qt.ItemDataRole.DisplayRole) == "Paused"
        assert model.index(0, COL_DESC).data(Qt.ItemDataRole.DisplayRole) == "127.0.0.1"
    finally:
        mgr.stop()


def test_add_dialog_marks_duplicates(server, tmp_path):
    from PySide6.QtWidgets import QApplication

    from idm.gui.add_dialog import AddDownloadDialog
    from idm.gui.theme import apply_theme

    from .test_gui import wait_until

    app = QApplication.instance() or QApplication([])
    theme = apply_theme(app)
    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False, first_run_done=True)
    mgr = DownloadManager(settings, state_path=tmp_path / "state.json")
    mgr.start()
    try:
        mgr.add(server.url("/file"), start=False).result(10)
        dlg = AddDownloadDialog(mgr, theme, server.url("/file"))
        dlg.show()
        wait_until(app, lambda: dlg.start_btn.isEnabled(), 10, "probe")
        assert dlg.duplicate_of is not None
        assert "already in your list" in dlg.card_sub.text()
        assert dlg.start_btn.text() == "Download anyway"
        futures = dlg.submit()
        futures[0].result(10)  # no DuplicateError: the user chose to proceed
        assert len(mgr.snapshot()) == 2
        dlg.close()
    finally:
        mgr.stop()


def test_progress_text_complements_status_column():
    from idm.engine.download import Segment
    from idm.gui.models import progress_text

    t = DownloadTask(url="https://example.com/a.bin", filename="a.bin", dest_dir="d", size=1000)
    t.segments = [Segment(start=0, end=499, downloaded=250), Segment(start=500, end=999, downloaded=0)]
    assert progress_text(t) == "Queued"
    assert progress_text(t, "starts 23:00") == "Queued · scheduler starts 23:00"
    t.status = Status.DOWNLOADING
    assert progress_text(t) == "25.0%  ·  250 B of 1000 B"
    t.status = Status.PAUSED
    assert progress_text(t) == "Paused  ·  25.0%"
    t.status = Status.ERROR
    t.error = "ClientResponseError: 403, message='Forbidden'"
    assert progress_text(t) == "Error · HTTP 403 Forbidden"
    t.status = Status.COMPLETED
    assert progress_text(t) == "Complete"
    unknown = DownloadTask(url="https://example.com/b", filename="b", dest_dir="d", status=Status.DOWNLOADING)
    unknown.segments = [Segment(start=0, end=-1, downloaded=2048)]
    assert progress_text(unknown) == "2.0 KB received"


def test_column_menu_and_layout_persist(window, tmp_path):
    from PySide6.QtWidgets import QHeaderView

    from idm.gui.models import COL_ADDED, COL_NAME

    header = window.table.horizontalHeader()
    assert header.sectionResizeMode(COL_NAME) == QHeaderView.ResizeMode.Stretch
    assert not window.table.isColumnHidden(COL_ADDED)
    window._set_column_visible(COL_ADDED, False)
    window.table.setColumnWidth(COL_DESC, 222)
    window.splitter.setSizes([160, 900])
    window._save_ui_state()

    window._reset_columns()
    assert not window.table.isColumnHidden(COL_ADDED)
    window._restore_ui_state()
    assert window.table.isColumnHidden(COL_ADDED)
    assert window.table.columnWidth(COL_DESC) == 222
    assert header.sectionResizeMode(COL_NAME) == QHeaderView.ResizeMode.Stretch
    window._reset_columns()
    assert not window.table.isColumnHidden(COL_ADDED)
