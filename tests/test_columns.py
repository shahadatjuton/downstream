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
