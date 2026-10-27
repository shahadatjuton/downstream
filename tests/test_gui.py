"""GUI tests: drive the real MainWindow (offscreen) against the local test server."""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from idm.engine import DownloadManager, Settings, Status
from idm.gui.add_dialog import AddDownloadDialog
from idm.gui.main_window import CountRole, MainWindow
from idm.gui.models import COL_STATUS, TaskFilter
from idm.gui.theme import apply_theme

from .test_engine import FILE_SIZE, TestServer


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def server():
    s = TestServer().start()
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


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_until(app: QApplication, cond, timeout: float = 20.0, what: str = "condition") -> None:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def sidebar_counts(win: MainWindow) -> dict[str, int]:
    return {win.sidebar.item(i).text(): win.sidebar.item(i).data(CountRole) for i in range(win.sidebar.count())}


def select_filter(win: MainWindow, name: str) -> None:
    for i in range(win.sidebar.count()):
        if win.sidebar.item(i).text() == name:
            win.sidebar.setCurrentRow(i)
            return
    raise AssertionError(f"no sidebar entry {name}")


def visible_names(win: MainWindow) -> list[str]:
    return [win.model.task_at(r).filename for r in range(win.model.rowCount())]


def add_through_dialog(app, win: MainWindow, url: str, save_as: str, monkeypatch) -> None:
    """Run the real Add-URL flow: open the dialog, let it probe, fill 'Save as', accept."""
    captured: dict[str, AddDownloadDialog] = {}

    def fake_exec(dlg: AddDownloadDialog) -> int:
        captured["dlg"] = dlg
        wait_until(app, lambda: dlg.probe_result is not None, 10, "probe result in add dialog")
        assert dlg.start_btn.isEnabled(), "Start button must enable once the probe succeeds"
        assert dlg.card_title.text() == dlg.probe_result.filename
        dlg.name_edit.setText(save_as)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(AddDownloadDialog, "exec", fake_exec)
    win.add_url(url)
    assert "dlg" in captured


# ----------------------------------------------------------------------------- tests
def test_empty_state_and_zero_counts(app, window):
    pump(app, 0.4)
    assert window.model.rowCount() == 0
    assert window.empty.isVisible()
    counts = sidebar_counts(window)
    assert counts[TaskFilter.ALL] == 0 and counts[TaskFilter.ACTIVE] == 0 and counts[TaskFilter.DONE] == 0
    assert window.windowTitle() == "IDM Clone"


def test_add_url_filters_and_dynamic_counts(app, window, server, monkeypatch, tmp_path):
    server.delay = 0.25  # ~6 s total so the in-flight checks below run before completion

    # 1. Add a PDF through the real dialog flow.
    add_through_dialog(app, window, server.url("/file"), "report.pdf", monkeypatch)
    wait_until(app, lambda: window.model.rowCount() == 1, 10, "row to appear")
    assert visible_names(window) == ["report.pdf"]
    wait_until(app, lambda: not window.empty.isVisible(), 3, "empty overlay to hide")

    # Newly added download gets selected and the details pane follows it.
    wait_until(app, lambda: window.selected_tasks() and window.details.task is not None, 5, "auto-select")
    assert window.details.title.text() == "report.pdf"

    # 2. Counts update live while downloading.
    wait_until(app, lambda: sidebar_counts(window)[TaskFilter.ACTIVE] == 1, 5, "Unfinished count")
    counts = sidebar_counts(window)
    assert counts[TaskFilter.ALL] == 1
    assert counts[TaskFilter.DONE] == 0
    assert counts["Documents"] == 1
    assert counts["Compressed"] == 0

    task = window.manager.snapshot()[0]
    assert task.dest_dir.endswith("Documents"), "category folder routing"
    wait_until(app, lambda: task.status == Status.DOWNLOADING and task.downloaded > 0, 10, "download progress")

    # Progress column text and window title reflect live state.
    pump(app, 0.6)
    status_text = window.model.index(0, COL_STATUS).data(Qt.ItemDataRole.DisplayRole)
    assert status_text == "Downloading"
    assert "%" in window.windowTitle() and "IDM Clone" in window.windowTitle()
    assert window.statusBar().currentMessage().startswith("1 downloading")

    # 3. Each filter shows the right rows while in flight.
    select_filter(window, TaskFilter.ACTIVE)
    pump(app, 0.3)
    assert visible_names(window) == ["report.pdf"]
    select_filter(window, TaskFilter.DONE)
    pump(app, 0.3)
    assert visible_names(window) == []
    assert window.empty.isVisible()
    select_filter(window, "Documents")
    pump(app, 0.3)
    assert visible_names(window) == ["report.pdf"]
    select_filter(window, "Compressed")
    pump(app, 0.3)
    assert visible_names(window) == []
    select_filter(window, TaskFilter.ALL)
    pump(app, 0.3)
    assert visible_names(window) == ["report.pdf"]

    # 4. Pause via Space toggles state, and Unfinished still lists it.
    window.table.selectRow(0)
    window._toggle_selected()
    wait_until(app, lambda: task.status == Status.PAUSED, 10, "pause")
    pump(app, 0.4)
    assert window.model.index(0, COL_STATUS).data(Qt.ItemDataRole.DisplayRole) == "Paused"
    assert sidebar_counts(window)[TaskFilter.ACTIVE] == 1
    assert window.act_resume.isEnabled() and not window.act_pause.isEnabled()

    # Resume and let it finish.
    server.delay = 0.0
    window._toggle_selected()
    wait_until(app, lambda: task.status == Status.COMPLETED, 30, "completion")
    pump(app, 0.6)

    # 5. The download moved from Unfinished to Finished, dynamically.
    counts = sidebar_counts(window)
    assert counts[TaskFilter.ALL] == 1
    assert counts[TaskFilter.ACTIVE] == 0
    assert counts[TaskFilter.DONE] == 1
    assert counts["Documents"] == 1
    assert (tmp_path / "dl" / "Documents" / "report.pdf").stat().st_size == FILE_SIZE
    assert window.model.index(0, COL_STATUS).data(Qt.ItemDataRole.DisplayRole) == "Complete"
    assert window.windowTitle() == "IDM Clone"

    select_filter(window, TaskFilter.ACTIVE)
    pump(app, 0.3)
    assert visible_names(window) == []
    select_filter(window, TaskFilter.DONE)
    pump(app, 0.3)
    assert visible_names(window) == ["report.pdf"]

    # 6. Add a second download of another category; counts and filters follow.
    select_filter(window, TaskFilter.ALL)
    add_through_dialog(app, window, server.url("/norange"), "archive.zip", monkeypatch)
    wait_until(app, lambda: window.model.rowCount() == 2, 10, "second row")
    wait_until(app, lambda: sidebar_counts(window)["Compressed"] == 1, 5, "Compressed count")
    assert sidebar_counts(window)[TaskFilter.ALL] == 2
    select_filter(window, "Compressed")
    pump(app, 0.3)
    assert visible_names(window) == ["archive.zip"]
    select_filter(window, "Documents")
    pump(app, 0.3)
    assert visible_names(window) == ["report.pdf"]

    second = next(t for t in window.manager.snapshot() if t.filename == "archive.zip")
    wait_until(app, lambda: second.status == Status.COMPLETED, 30, "second completion")
    pump(app, 0.6)
    assert sidebar_counts(window)[TaskFilter.DONE] == 2

    # 7. Remove one; everything updates again.
    select_filter(window, TaskFilter.ALL)
    pump(app, 0.3)
    window.table.selectRow(1)
    assert window.selected_tasks()[0].filename == "archive.zip"
    window.manager.remove_task(second.id).result(10)
    wait_until(app, lambda: window.model.rowCount() == 1, 5, "row removed")
    pump(app, 0.4)
    counts = sidebar_counts(window)
    assert counts[TaskFilter.ALL] == 1 and counts[TaskFilter.DONE] == 1 and counts["Compressed"] == 0


def test_add_dialog_reports_bad_link(app, window, server):
    dlg = AddDownloadDialog(window.manager, window.theme, server.url("/missing"), window)
    wait_until(app, lambda: "Could not reach" in dlg.card_title.text(), 10, "error card")
    assert not dlg.start_btn.isEnabled()
    dlg.close()


def test_paste_and_drop_urls(app, window, server, monkeypatch):
    # Ctrl+V path opens the dialog with the clipboard URL; we accept it immediately.
    QApplication.clipboard().setText(server.url("/norange"))

    def fake_exec(dlg: AddDownloadDialog) -> int:
        wait_until(app, lambda: dlg.probe_result is not None, 10, "probe")
        assert dlg.url() == server.url("/norange")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(AddDownloadDialog, "exec", fake_exec)
    window._paste_url()
    wait_until(app, lambda: window.model.rowCount() == 1, 10, "pasted row")

    # Dropping several URLs adds them directly without a dialog.
    class Mime:
        def hasUrls(self):  # noqa: N802
            return False

        def hasText(self):  # noqa: N802
            return True

        def text(self):
            return "\n".join([server.url("/file"), server.url("/norange")])

    urls = MainWindow._urls_from_mime(Mime())
    assert len(urls) == 2
    for u in urls:
        window.manager.add(u).result(10)
    wait_until(app, lambda: window.model.rowCount() == 3, 10, "dropped rows")
    wait_until(app, lambda: sidebar_counts(window)[TaskFilter.ALL] == 3, 5, "count 3")


def test_download_complete_popup(app, window, server, monkeypatch):
    from idm.gui.complete_dialog import DownloadCompleteDialog

    opened: list[str] = []
    monkeypatch.setattr("idm.gui.main_window._reveal_in_explorer", lambda p: opened.append(f"reveal:{p.name}"))
    monkeypatch.setattr("idm.gui.main_window._open_path", lambda p: opened.append(f"open:{p.name}"))

    task = window.manager.add(server.url("/norange"), filename="done.bin").result(10)
    wait_until(app, lambda: task.status == Status.COMPLETED, 60, "completion")
    wait_until(app, lambda: any(d.isVisible() for d in window._complete_dialogs), 10, "complete popup")
    dlg: DownloadCompleteDialog = window._complete_dialogs[-1]
    assert dlg.windowTitle() == "Download complete"
    assert dlg.task.filename == "done.bin"

    # "Open folder" reveals the file and closes the popup.
    from PySide6.QtWidgets import QPushButton

    btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Open folder")
    btn.click()
    pump(app, 0.2)
    assert opened == ["reveal:done.bin"]
    assert not dlg.isVisible()

    # Ticking "don't show again" turns the setting off; the next completion shows no popup.
    task2 = window.manager.add(server.url("/norange"), filename="second.bin").result(10)
    wait_until(app, lambda: task2.status == Status.COMPLETED, 60, "second completion")
    wait_until(
        app,
        lambda: len(window._complete_dialogs) >= 1 and window._complete_dialogs[-1].task is task2,
        10,
        "second popup",
    )
    dlg2 = window._complete_dialogs[-1]
    dlg2.dont_show.setChecked(True)
    dlg2.close()
    wait_until(app, lambda: window.manager.settings.show_complete_dialog is False, 5, "setting update")

    task3 = window.manager.add(server.url("/norange"), filename="third.bin").result(10)
    wait_until(app, lambda: task3.status == Status.COMPLETED, 60, "third completion")
    pump(app, 0.6)
    assert all(d.task is not task3 for d in window._complete_dialogs)
    assert window._last_completed is task3

    # Sidebar button opens the downloads folder.
    window.folder_btn.click()
    assert opened[-1] == f"open:{Path(window.manager.settings.download_dir).name}"


def test_search_sort_clear_and_queue_order(app, window, server, monkeypatch):
    from PySide6.QtCore import Qt

    from idm.gui.models import COL_NAME

    names = ["zeta.bin", "alpha.bin", "mid.bin"]
    tasks = [window.manager.add(server.url("/norange"), filename=n, start=False).result(10) for n in names]
    wait_until(app, lambda: window.model.rowCount() == 3, 10, "rows")

    # Search filters the proxy (what the table shows), not the source model.
    window.search.setText("alp")
    pump(app, 0.3)
    assert window.proxy.rowCount() == 1 and window.proxy.task_at(0).filename == "alpha.bin"
    window.search.setText("nothing-matches")
    pump(app, 0.4)
    assert window.proxy.rowCount() == 0 and window.empty.isVisible() and window.empty_title.text() == "No matches"
    window.search.clear()
    pump(app, 0.3)
    assert window.proxy.rowCount() == 3 and not window.empty.isVisible()

    # Sorting by name.
    window.table.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
    pump(app, 0.2)
    assert [window.proxy.task_at(r).filename for r in range(3)] == ["alpha.bin", "mid.bin", "zeta.bin"]
    window.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    window.proxy.sort(-1)
    pump(app, 0.2)
    assert [window.proxy.task_at(r).filename for r in range(3)] == names  # queue order restored

    # Queue reorder: move 'mid' to top, 'zeta' to bottom.
    window.manager.move_task(tasks[2].id, "top").result(5)
    window.manager.move_task(tasks[0].id, "bottom").result(5)
    pump(app, 0.4)
    assert [t.filename for t in window.manager.snapshot()] == ["mid.bin", "alpha.bin", "zeta.bin"]
    assert [window.proxy.task_at(r).filename for r in range(3)] == ["mid.bin", "alpha.bin", "zeta.bin"]

    # Selection through the proxy, then Clear done removes only finished entries.
    window.manager.start_task(tasks[1].id)
    wait_until(app, lambda: tasks[1].status == Status.COMPLETED, 60, "alpha completes")
    pump(app, 0.4)
    assert window.act_clear.isEnabled()
    window.clear_completed()
    wait_until(app, lambda: window.model.rowCount() == 2, 10, "clear done")
    assert {t.filename for t in window.manager.snapshot()} == {"mid.bin", "zeta.bin"}
    assert tasks[1].final_path.exists(), "clearing keeps the file"


def test_batch_add_through_dialog(app, window, server, monkeypatch):
    from idm.gui.add_dialog import AddDownloadDialog

    def fake_exec(dlg: AddDownloadDialog) -> int:
        assert dlg.batch_mode, "multi-line input must switch the dialog to batch mode"
        assert len(dlg.batch_urls()) == 2
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(AddDownloadDialog, "exec", fake_exec)
    window.add_url(server.url("/file") + "\n" + server.url("/norange") + "\nnot a link\n")
    wait_until(app, lambda: window.model.rowCount() == 2, 20, "two batch rows")
    for t in window.manager.snapshot():
        wait_until(app, lambda t=t: t.status == Status.COMPLETED, 60, "batch completion")


def test_when_done_runs_action_after_last_download(app, window, server, monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(window, "execute_when_done", lambda action: ran.append(action))
    from idm.gui import main_window as mw

    monkeypatch.setattr(mw.WhenDoneDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window.when_done.setCurrentIndex(1)  # "Exit IDM Clone"
    task = window.manager.add(server.url("/norange"), filename="last.bin").result(10)
    wait_until(app, lambda: task.status == Status.COMPLETED, 60, "completion")
    wait_until(app, lambda: ran == ["exit"], 10, "when-done action")
    assert window.when_done.currentData() == "nothing"  # reset so it does not fire again


def test_interrupted_downloads_auto_resume(server, tmp_path):
    from idm.engine import DownloadManager, Settings

    settings = Settings(download_dir=str(tmp_path / "dl"), use_categories=False, auto_resume=True)
    state = tmp_path / "state.json"
    server.delay = 0.1
    m1 = DownloadManager(settings, state_path=state)
    m1.start()
    t = m1.add(server.url("/file"), filename="resume.bin").result(10)
    wait_until(
        QApplication.instance() or QApplication([]),
        lambda: t.status == Status.DOWNLOADING and t.downloaded > 0,
        20,
        "progress",
    )
    m1._save()
    crashed_state = state.read_text("utf-8")  # status=downloading, as if the process died right now
    m1.stop()
    state.write_text(crashed_state, "utf-8")
    server.delay = 0.0

    m2 = DownloadManager(settings, state_path=state)
    m2.start()
    try:
        done = m2.wait(t.id, 60)
        assert done.status == Status.COMPLETED, done.error
    finally:
        m2.stop()

    m3_settings = Settings(download_dir=str(tmp_path / "dl2"), use_categories=False, auto_resume=False)
    state3 = tmp_path / "state3.json"
    server.delay = 0.1
    m3 = DownloadManager(m3_settings, state_path=state3)
    m3.start()
    t3 = m3.add(server.url("/file"), filename="stay.bin").result(10)
    wait_until(QApplication.instance(), lambda: t3.downloaded > 0, 20, "progress 2")
    m3._save()
    crashed3 = state3.read_text("utf-8")
    m3.stop()
    state3.write_text(crashed3, "utf-8")
    m4 = DownloadManager(m3_settings, state_path=state3)
    m4.start()
    try:
        assert m4.snapshot()[0].status == Status.PAUSED  # auto_resume off: stays paused
    finally:
        m4.stop()
