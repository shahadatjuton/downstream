"""Main window: toolbar with search, category sidebar, sortable download table, details pane with
actions, setup card, tray, clipboard, drag-and-drop, queue control, and 'when done' actions."""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSystemTrayIcon,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..engine import DownloadManager, DownloadTask, Status
from .add_dialog import AddDownloadDialog
from .complete_dialog import DownloadCompleteDialog
from .details import DetailsPane
from .dialogs import ISSUES_URL, REPO_URL, AboutDialog, BrowserSetupDialog, UpdateChecker
from .icons import CATEGORY_ICONS, icon
from .models import (
    COL_ADDED,
    COL_ETA,
    COL_NAME,
    COL_SIZE,
    COL_SPEED,
    COL_STATUS,
    ROW_HEIGHT,
    ProgressDelegate,
    TaskFilter,
    TaskProxyModel,
    TaskRole,
    TaskTableModel,
)
from .settings_dialog import SettingsDialog
from .theme import Theme, apply_theme
from .util import fmt_speed, looks_like_url

DOWNLOADABLE_HINT_EXTS = {
    "zip",
    "rar",
    "7z",
    "tar",
    "gz",
    "bz2",
    "xz",
    "iso",
    "exe",
    "msi",
    "apk",
    "dmg",
    "deb",
    "rpm",
    "mp4",
    "mkv",
    "avi",
    "mov",
    "webm",
    "mp3",
    "flac",
    "wav",
    "pdf",
    "epub",
    "jpg",
    "png",
    "gif",
}

SIDEBAR_ICONS = {TaskFilter.ALL: "list", TaskFilter.ACTIVE: "clock", TaskFilter.DONE: "check", **CATEGORY_ICONS}
IconRole = Qt.ItemDataRole.UserRole
CountRole = Qt.ItemDataRole.UserRole + 1

WHEN_DONE = [("Do nothing", "nothing"), ("Exit IDM Clone", "exit"), ("Sleep", "sleep"), ("Shut down", "shutdown")]


def make_app_icon() -> QIcon:
    """Blue circle with a white down-arrow (also used for the tray and the exe)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2d7dd2"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 60, 60)
    pen = QPen(QColor("white"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawLine(32, 14, 32, 44)
    p.drawLine(18, 32, 32, 46)
    p.drawLine(46, 32, 32, 46)
    p.end()
    return QIcon(pm)


def host_registered() -> bool:
    """Is the browser native-messaging host registered for at least one browser (Windows registry)?"""
    if sys.platform != "win32":
        return False
    import winreg

    for base in ("Google\\Chrome", "Microsoft\\Edge", "Chromium", "BraveSoftware\\Brave-Browser"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"Software\\{base}\\NativeMessagingHosts\\com.idmclone.host"):
                return True
        except OSError:
            continue
    return False


class _EventBridge(QObject):
    event = Signal(str, object)
    ipc = Signal(object)


class SidebarDelegate(QStyledItemDelegate):
    """Icon + name + right-aligned count, with rounded selection."""

    def __init__(self, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(150, 34)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        t = self.theme
        r = QRectF(option.rect).adjusted(2, 1, -2, -1)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.accent_soft)
            painter.drawRoundedRect(r, 6, 6)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.hover)
            painter.drawRoundedRect(r, 6, 6)
        name = index.data(Qt.ItemDataRole.DisplayRole)
        count = index.data(CountRole) or 0
        icon_name = index.data(IconRole) or "file"
        color = t.accent if selected else t.muted
        pm = icon(icon_name, color, 18).pixmap(18, 18)
        painter.drawPixmap(int(r.x() + 10), int(r.center().y() - 9), pm)
        painter.setPen(t.text)
        text_rect = QRectF(r.x() + 36, r.y(), r.width() - 36 - 38, r.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        if count:
            painter.setPen(t.accent if selected else t.muted)
            painter.drawText(
                QRectF(r.right() - 40, r.y(), 32, r.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                str(count),
            )
        painter.restore()


class WhenDoneDialog(QDialog):
    """Cancellable countdown before exiting / sleeping / shutting down."""

    def __init__(self, action_label: str, seconds: int = 60, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("All downloads finished")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.remaining = seconds
        self.action_label = action_label
        self.label = QLabel()
        self.label.setObjectName("title")
        cancel = QPushButton("Cancel")
        cancel.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        now = QPushButton(f"{action_label} now")
        now.setAutoDefault(False)
        now.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(now)
        row.addWidget(cancel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(16)
        lay.addWidget(self.label)
        lay.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick(first=True)

    def _tick(self, first: bool = False) -> None:
        if not first:
            self.remaining -= 1
        if self.remaining <= 0:
            self.accept()
            return
        self.label.setText(f"{self.action_label} in {self.remaining} seconds…")


class MainWindow(QMainWindow):
    def __init__(self, manager: DownloadManager, theme: Theme, initial_url: str = "") -> None:
        super().__init__()
        self.manager = manager
        self.theme = theme
        self.setWindowTitle("IDM Clone")
        self.setWindowIcon(make_app_icon())
        self.resize(1160, 700)
        self.setAcceptDrops(True)

        self._bridge = _EventBridge(self)
        self._bridge.event.connect(self._on_engine_event)
        self._bridge.ipc.connect(self._on_ipc)
        manager.on_event = lambda name, task: self._bridge.event.emit(name, task)
        manager.on_ipc = self._ipc_from_engine

        self._complete_dialogs: list[DownloadCompleteDialog] = []
        self._last_completed: DownloadTask | None = None
        self._completed_since_idle = False
        self._was_busy = False
        self._reopen_after_ffmpeg = ""
        self._themed_actions: list[tuple[QAction, str]] = []
        self._updates = UpdateChecker(manager, self)
        self._updates.result.connect(self._on_update_result)
        self._silent_update_check = False
        self._build_menubar()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._build_tray()
        self._build_shortcuts()
        self._last_clip = ""
        QApplication.clipboard().dataChanged.connect(self._on_clipboard)

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()
        if initial_url:
            QTimer.singleShot(0, lambda: self.add_url(initial_url))
        if manager.settings.check_updates:
            QTimer.singleShot(4000, lambda: self.check_for_updates(silent=True))
        if not manager.settings.first_run_done:
            QTimer.singleShot(700, self.show_welcome)

    # ------------------------------------------------------------- building
    def _icon(self, name: str) -> QIcon:
        return icon(name, self.theme.text, 22)

    def _build_menubar(self) -> None:
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        file_menu.addAction("&Add link…", self.add_url, QKeySequence("Ctrl+N"))
        file_menu.addAction("Add &many links…", lambda: self.add_url("\n"), QKeySequence("Ctrl+Shift+N"))
        file_menu.addAction("&Paste link from clipboard", self._paste_url, QKeySequence("Ctrl+Shift+V"))
        file_menu.addSeparator()
        file_menu.addAction("Open &downloads folder", lambda: _open_path(Path(self.manager.settings.download_dir)))
        file_menu.addSeparator()
        file_menu.addAction("Minimise to &tray", self.hide)
        file_menu.addAction("E&xit", self._quit, QKeySequence("Ctrl+Q"))

        dl = mb.addMenu("&Downloads")
        dl.addAction("&Resume selected", self.resume_selected)
        dl.addAction("&Pause selected", self.pause_selected)
        dl.addAction("Re&move selected", self.remove_selected, QKeySequence("Delete"))
        dl.addAction("Remove selected and &delete files", lambda: self.remove_selected(True))
        dl.addSeparator()
        dl.addAction("Resume &all", lambda: self.manager.start_all())
        dl.addAction("Pause a&ll", lambda: self.manager.pause_all())
        dl.addAction("&Clear finished", self.clear_completed)
        dl.addSeparator()
        dl.addAction(
            "Select all", self.table.selectAll if hasattr(self, "table") else (lambda: None), QKeySequence("Ctrl+A")
        )

        tools = mb.addMenu("&Tools")
        tools.addAction("&Browser setup…", self.open_browser_setup)
        tools.addAction("Install &ffmpeg (HD video)…", self._install_ffmpeg_plain)
        tools.addSeparator()
        tools.addAction("&Settings…", self.open_settings, QKeySequence("Ctrl+,"))

        help_menu = mb.addMenu("&Help")
        help_menu.addAction("&Check for updates…", lambda: self.check_for_updates(silent=False))
        help_menu.addAction("&Project page on GitHub", lambda: webbrowser.open(REPO_URL))
        help_menu.addAction("&Report a problem…", lambda: webbrowser.open(ISSUES_URL))
        help_menu.addAction("Open &log folder", self._open_logs)
        help_menu.addSeparator()
        help_menu.addAction("&About IDM Clone", self.show_about)

    # ------------------------------------------------------------- help/tools
    def show_about(self) -> None:
        AboutDialog(self.theme, self).exec()

    def open_browser_setup(self) -> None:
        BrowserSetupDialog(self.theme, self.manager, host_registered(), self).exec()
        self._refresh_setup_card()

    def show_welcome(self) -> None:
        """First run after install: guide the user through loading the browser extension."""
        self._show_from_tray()
        s = self.manager.settings
        if s.extension_seen and host_registered():
            self.manager.update_settings(replace(s, first_run_done=True))
            return
        dlg = BrowserSetupDialog(self.theme, self.manager, host_registered(), self, welcome=True)
        dlg.exec()
        if dlg.dont_show_again() or self.manager.settings.extension_seen:
            self.manager.update_settings(replace(self.manager.settings, first_run_done=True))
        self._refresh_setup_card()

    def check_for_updates(self, silent: bool = False) -> None:
        self._silent_update_check = silent
        self._updates.check()

    def _on_update_result(self, latest, error) -> None:
        UpdateChecker.show_result(self, latest, error, silent_if_current=self._silent_update_check)

    def _open_logs(self) -> None:
        from ..engine.settings import app_data_dir

        _open_path(app_data_dir() / "logs")

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(22, 22))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(tb)

        def act(text: str, icon_name: str, slot, shortcut: str | None = None, tip: str = "") -> QAction:
            a = QAction(self._icon(icon_name), text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip((tip or text) + (f"  ({shortcut})" if shortcut else ""))
            tb.addAction(a)
            self._themed_actions.append((a, icon_name))
            return a

        self.act_add = act("Add URL", "download", self.add_url, "Ctrl+N", "Add one link, or many at once")
        tb.addSeparator()
        self.act_resume = act("Resume", "play", self.resume_selected, None, "Resume selected  (Space)")
        self.act_pause = act("Pause", "pause", self.pause_selected, None, "Pause selected  (Space)")
        self.act_remove = act("Remove", "trash", self.remove_selected, "Delete", "Remove selected from list")
        tb.addSeparator()
        act("Resume all", "play-all", lambda: self.manager.start_all())
        act("Pause all", "pause-all", lambda: self.manager.pause_all())
        self.act_clear = act(
            "Clear done",
            "check",
            self.clear_completed,
            None,
            "Remove finished downloads from the list (files are kept)",
        )
        tb.addSeparator()
        act("Open folder", "folder", self.open_folder_selected, None, "Open the download folder")
        self.act_details = act("Details", "details", self._toggle_details, "Ctrl+D", "Show or hide the details pane")
        self.act_details.setCheckable(True)
        self.act_details.setChecked(True)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search downloads…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        tb.addWidget(self.search)
        tb.addSeparator()
        act("Settings", "gear", self.open_settings, "Ctrl+,")

    def _build_central(self) -> None:
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar_delegate = SidebarDelegate(self.theme, self.sidebar)
        self.sidebar.setItemDelegate(self.sidebar_delegate)
        self.sidebar.setMouseTracking(True)
        self.sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._rebuild_sidebar()
        self.sidebar.currentTextChanged.connect(self._on_filter_changed)

        self.model = TaskTableModel(self.manager, self.theme, self)
        self.proxy = TaskProxyModel(self.model, self)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.progress_delegate = ProgressDelegate(self.theme, self.table)
        self.table.setItemDelegateForColumn(COL_STATUS, self.progress_delegate)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setIconSize(QSize(20, 20))
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(60)
        header.setHighlightSections(False)
        header.setSectionsClickable(True)
        for col, width in ((COL_SIZE, 90), (COL_STATUS, 260), (COL_SPEED, 100), (COL_ETA, 90), (COL_ADDED, 130)):
            self.table.setColumnWidth(col, width)
        self.table.setSortingEnabled(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)  # insertion (queue) order until the user sorts
        header.setToolTip("Click a column to sort. Click the empty area of the header to restore queue order.")
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda _i: self.open_file_selected())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._on_selection_changed())
        self.table.viewport().installEventFilter(self)

        self._build_empty_state()

        self.details = DetailsPane(self.theme)
        self.details.open_file.connect(self.open_file_selected)
        self.details.open_folder.connect(self.open_folder_selected)
        self.details.copy_link.connect(self.copy_link_selected)
        self.details.toggle_pause.connect(self._toggle_selected)
        self.details.remove.connect(self.remove_selected)

        right = QWidget()
        right.setObjectName("central")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self.table, 1)
        rl.addWidget(self.details)

        left = QWidget()
        left.setObjectName("sidebarColumn")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        ll.addWidget(self.sidebar, 1)
        self.folder_btn = QPushButton(icon("folder", self.theme.accent, 18), "Downloads folder")
        self.folder_btn.setObjectName("sidebarButton")
        self.folder_btn.setToolTip("Open the folder where files are saved")
        self.folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_btn.clicked.connect(lambda: _open_path(Path(self.manager.settings.download_dir)))
        ll.addWidget(self.folder_btn)
        left.setMaximumWidth(190)
        left.setMinimumWidth(150)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([170, 990])
        splitter.setHandleWidth(1)
        self.setCentralWidget(splitter)
        self.details.set_task(None)

    def _build_empty_state(self) -> None:
        """Overlay on the table viewport: a welcome + setup card when the list is empty."""
        self.empty = QWidget(self.table.viewport())
        lay = QVBoxLayout(self.empty)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(10)
        self.empty_pic = QLabel()
        self.empty_pic.setPixmap(icon("download", self.theme.track, 64).pixmap(64, 64))
        self.empty_pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("No downloads here")
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Press Ctrl+N, paste a link with Ctrl+V, or drop a URL anywhere in this window.")
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btns = QHBoxLayout()
        btns.setAlignment(Qt.AlignmentFlag.AlignCenter)
        add_btn = QPushButton("Add link")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(lambda: self.add_url())
        paste_btn = QPushButton("Paste from clipboard")
        paste_btn.clicked.connect(self._paste_url)
        btns.addWidget(add_btn)
        btns.addWidget(paste_btn)

        self.setup_card = QFrame()
        self.setup_card.setObjectName("setupCard")
        self.setup_card.setFixedWidth(560)
        card = QVBoxLayout(self.setup_card)
        card.setContentsMargins(16, 12, 16, 12)
        card.setSpacing(8)
        head = QLabel("Finish setting up")
        head.setObjectName("title")
        card.addWidget(head)
        self.setup_browser = QLabel()
        self.setup_ext = QLabel()
        self.setup_ffmpeg = QLabel()
        setup_btns = QHBoxLayout()
        self.setup_browser_btn = QPushButton("Browser setup…")
        self.setup_browser_btn.clicked.connect(self.open_browser_setup)
        self.setup_ffmpeg_btn = QPushButton("Install ffmpeg")
        self.setup_ffmpeg_btn.clicked.connect(self._install_ffmpeg_plain)
        setup_btns.addWidget(self.setup_browser_btn)
        setup_btns.addWidget(self.setup_ffmpeg_btn)
        setup_btns.addStretch(1)
        card.addWidget(self.setup_browser)
        card.addWidget(self.setup_ext)
        card.addWidget(self.setup_ffmpeg)
        card.addLayout(setup_btns)

        lay.addWidget(self.empty_pic)
        lay.addWidget(self.empty_title)
        lay.addWidget(hint)
        lay.addLayout(btns)
        lay.addSpacing(10)
        lay.addWidget(self.setup_card, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty.hide()
        self._refresh_setup_card()

    def _refresh_setup_card(self) -> None:
        browser_ok = host_registered()
        ext_ok = self.manager.settings.extension_seen
        ffmpeg_ok = self.manager.ffmpeg is not None
        self.setup_ext.setText(
            "✓  Browser extension connected"
            if ext_ok
            else "•  Browser extension not loaded yet (Download button on videos, caught downloads)"
        )
        self.setup_ext.setObjectName("ok" if ext_ok else "warn")
        self.setup_browser.setText(
            "✓  Browser extension host is registered"
            if browser_ok
            else "•  Browser extension not set up: catch downloads and page videos from Chrome/Edge"
        )
        self.setup_browser.setObjectName("ok" if browser_ok else "warn")
        self.setup_ffmpeg.setText(
            "✓  ffmpeg installed: HD video formats available"
            if ffmpeg_ok
            else "•  ffmpeg missing: YouTube limited to 360p (one-click install, 90 MB)"
        )
        self.setup_ffmpeg.setObjectName("ok" if ffmpeg_ok else "warn")
        self.setup_browser_btn.setVisible(not (browser_ok and ext_ok))
        self.setup_ffmpeg_btn.setVisible(not ffmpeg_ok)
        self.setup_card.setVisible(not (browser_ok and ext_ok and ffmpeg_ok))
        for w in (self.setup_browser, self.setup_ext, self.setup_ffmpeg):
            w.style().unpolish(w)
            w.style().polish(w)

    def _build_statusbar(self) -> None:
        self.when_done = QComboBox()
        self.when_done.setObjectName("whenDone")
        for label, value in WHEN_DONE:
            self.when_done.addItem(f"When done: {label}", value)
        self.when_done.setToolTip("What to do once every download has finished")
        self.statusBar().addPermanentWidget(self.when_done)
        self.statusBar().showMessage("Ready")

    def _rebuild_sidebar(self) -> None:
        current = self.sidebar.currentItem().text() if self.sidebar.currentItem() else TaskFilter.ALL
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        names = [*TaskFilter.PSEUDO, *self.manager.settings.categories.keys()]
        for name in names:
            item = QListWidgetItem(name)
            item.setData(IconRole, SIDEBAR_ICONS.get(name, "folder"))
            item.setData(CountRole, 0)
            self.sidebar.addItem(item)
        self.sidebar.setCurrentRow(names.index(current) if current in names else 0)
        self.sidebar.blockSignals(False)

    def _update_sidebar_counts(self, tasks: list[DownloadTask]) -> None:
        settings = self.manager.settings
        for i in range(self.sidebar.count()):
            item = self.sidebar.item(i)
            flt = TaskFilter(item.text())
            n = sum(1 for t in tasks if flt.accepts(t, settings))
            if item.data(CountRole) != n:
                item.setData(CountRole, n)

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        menu.addAction("Show", self._show_from_tray)
        menu.addAction("Add URL…", self.add_url)
        menu.addAction("Pause all", lambda: self.manager.pause_all())
        menu.addAction("Resume all", lambda: self.manager.start_all())
        menu.addSeparator()
        menu.addAction("About", self.show_about)
        menu.addAction("Quit", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.setToolTip("IDM Clone")
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        self.tray.show()

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Paste, self.table, activated=self._paste_url)
        QShortcut(QKeySequence("Return"), self.table, activated=self.open_file_selected)
        QShortcut(QKeySequence("Enter"), self.table, activated=self.open_file_selected)
        QShortcut(QKeySequence("Escape"), self.table, activated=self.table.clearSelection)
        QShortcut(QKeySequence("Space"), self.table, activated=self._toggle_selected)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: (self.search.setFocus(), self.search.selectAll()))
        QShortcut(QKeySequence("Escape"), self.search, activated=lambda: (self.search.clear(), self.table.setFocus()))
        QShortcut(QKeySequence("Ctrl+C"), self.table, activated=self.copy_link_selected)

    # ------------------------------------------------------------- theming
    def set_theme_mode(self, mode: str) -> None:
        """Re-skin everything live (Settings › Appearance)."""
        self.theme = apply_theme(QApplication.instance(), mode)
        self.model.theme = self.theme
        self.progress_delegate.theme = self.theme
        self.sidebar_delegate.theme = self.theme
        self.details.set_theme(self.theme)
        for a, name in self._themed_actions:
            a.setIcon(self._icon(name))
        self.folder_btn.setIcon(icon("folder", self.theme.accent, 18))
        self.empty_pic.setPixmap(icon("download", self.theme.track, 64).pixmap(64, 64))
        self.table.viewport().update()
        self.sidebar.viewport().update()

    # --------------------------------------------------------------- slots
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.table.viewport():
            if event.type() == QEvent.Type.Resize:
                self.empty.setGeometry(self.table.viewport().rect())
            elif (
                event.type() == QEvent.Type.MouseButtonDblClick
                and not self.table.indexAt(event.position().toPoint()).isValid()
            ):
                self.add_url()
                return True
        return super().eventFilter(obj, event)

    def _tick(self) -> None:
        self.model.refresh()
        tasks = self.manager.snapshot()
        self._update_sidebar_counts(tasks)
        show_empty = self.proxy.rowCount() == 0
        if show_empty != self.empty.isVisible():
            if show_empty:
                searching = bool(self.search.text().strip())
                self.empty_title.setText(
                    "No matches" if searching else ("No downloads here" if tasks else "No downloads yet")
                )
                self.setup_card.setVisible(not searching and not tasks and self.setup_card.isVisibleTo(self.empty))
                self._refresh_setup_card()
            self.empty.setVisible(show_empty)
            self.empty.setGeometry(self.table.viewport().rect())

        active = [t for t in tasks if t.status == Status.DOWNLOADING]
        total_speed = sum(t.speed for t in active)
        queued = sum(1 for t in tasks if t.status == Status.QUEUED)
        parts = []
        if active:
            parts.append(f"{len(active)} downloading")
        if queued:
            parts.append(f"{queued} queued")
        if total_speed:
            parts.append(fmt_speed(total_speed))
        status = "  ·  ".join(parts) if parts else "Idle"
        if not self.statusBar().currentMessage() or self.statusBar().currentMessage() in ("Ready", "Idle") or parts:
            self.statusBar().showMessage(status)

        title = "IDM Clone"
        sized = [t for t in active if t.size]
        if sized:
            done = sum(t.downloaded for t in sized)
            total = sum(t.size or 0 for t in sized)
            if total:
                title = f"{done / total * 100:.0f}% · {fmt_speed(total_speed)} · IDM Clone"
        if self.windowTitle() != title:
            self.setWindowTitle(title)
        if self.tray:
            self.tray.setToolTip(f"IDM Clone — {status}")
        self.details.refresh()
        self._update_actions()

        busy = bool(active or queued)
        if self._completed_since_idle and not busy:
            self._completed_since_idle = False
            self._run_when_done()

    def _update_actions(self) -> None:
        sel = self.selected_tasks()
        self.act_resume.setEnabled(any(t.status in (Status.PAUSED, Status.ERROR) for t in sel))
        self.act_pause.setEnabled(any(t.status in (Status.DOWNLOADING, Status.QUEUED) for t in sel))
        self.act_remove.setEnabled(bool(sel))
        self.act_clear.setEnabled(any(t.status == Status.COMPLETED for t in self.manager.snapshot()))

    def _on_selection_changed(self) -> None:
        sel = self.selected_tasks()
        self.details.set_task(sel[0] if sel else None)
        self._update_actions()

    def _on_filter_changed(self, name: str) -> None:
        self.model.set_filter(TaskFilter(name))
        self._on_selection_changed()

    def _on_search(self, text: str) -> None:
        self.proxy.set_search(text)
        self._on_selection_changed()

    def _toggle_details(self, checked: bool) -> None:
        self.details.setVisible(checked)

    def _on_engine_event(self, name: str, task: DownloadTask) -> None:
        if name == "add-failed":
            QMessageBox.warning(self, "Could not add download", f"{task.url}\n\n{task.error}")
            return
        if name == "added":
            self._select_task(task.id)
            return
        if name == "ffmpeg-ready":
            self._refresh_setup_card()
            pending = self._reopen_after_ffmpeg
            self._reopen_after_ffmpeg = ""
            if pending:
                self._show_from_tray()
                self.add_url(pending, kind="media")
            else:
                QMessageBox.information(
                    self, "ffmpeg installed", "ffmpeg is ready. HD video formats are now available."
                )
            return
        if name == "extension-connected":
            self._refresh_setup_card()
            self.statusBar().showMessage(f"Browser extension connected (v{task.filename})", 6000)
            return
        if name == "ffmpeg-failed":
            QMessageBox.warning(self, "ffmpeg", task.error or "could not install ffmpeg")
            return
        if name == Status.COMPLETED.value:
            self._on_download_complete(task)
            return
        if name == Status.ERROR.value and self.tray and self.tray.supportsMessages():
            self.tray.showMessage(
                "Download failed", f"{task.filename}\n{task.error}", QSystemTrayIcon.MessageIcon.Warning, 6000
            )

    # ------------------------------------------------------------ browser IPC
    def _ipc_from_engine(self, msg: dict) -> dict:
        """Runs on a worker thread: hand the message to the GUI thread and answer right away."""
        self._bridge.ipc.emit(msg)
        return {"ok": True}

    def _on_ipc(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "show":
            self._show_from_tray()
            return
        if cmd != "add":
            return
        url = str(msg.get("url", "")).strip()
        if not looks_like_url(url):
            return
        headers = {k: v for k, v in (msg.get("headers") or {}).items() if isinstance(k, str) and isinstance(v, str)}
        kind = str(msg.get("kind") or "auto")
        self._show_from_tray()
        self.add_url(
            url,
            headers=headers,
            kind=kind,
            filename=str(msg.get("filename") or ""),
            referrer=str(msg.get("referrer") or ""),
        )

    # --------------------------------------------------------- completion
    def _on_download_complete(self, task: DownloadTask) -> None:
        if task.id == getattr(self.manager, "_ffmpeg_task_id", None):
            return  # internal helper download, handled by the ffmpeg flow
        self._last_completed = task
        self._completed_since_idle = True
        if self.tray and self.tray.supportsMessages():
            self.tray.showMessage(
                "Download complete",
                f"{task.filename}\nClick to open the folder.",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )
        if not self.manager.settings.show_complete_dialog:
            return
        dlg = DownloadCompleteDialog(
            task,
            self.theme,
            self.manager.settings,
            on_open=self._open_task_file,
            on_open_folder=self._reveal_task,
            on_dont_show=self._disable_complete_dialog,
            parent=None,
        )
        dlg.setWindowIcon(self.windowIcon())
        self._complete_dialogs = [d for d in self._complete_dialogs if d.isVisible()]
        self._complete_dialogs.append(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _run_when_done(self) -> None:
        action = self.when_done.currentData()
        if action in (None, "nothing"):
            return
        label = {"exit": "Exit IDM Clone", "sleep": "Sleep", "shutdown": "Shut down"}[action]
        self._show_from_tray()
        dlg = WhenDoneDialog(label, 60, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.when_done.setCurrentIndex(0)
            return
        self.when_done.setCurrentIndex(0)
        self.execute_when_done(action)

    def execute_when_done(self, action: str) -> None:
        if action == "exit":
            self._quit()
        elif action == "sleep":
            _system_sleep()
        elif action == "shutdown":
            _system_shutdown()

    def _install_ffmpeg_then_reopen(self, url: str) -> None:
        self._reopen_after_ffmpeg = url
        self.manager.install_ffmpeg()
        self.statusBar().showMessage(
            "Downloading ffmpeg… the video link will reopen with all qualities when it is ready.", 8000
        )

    def _install_ffmpeg_plain(self) -> None:
        self.manager.install_ffmpeg()
        self.statusBar().showMessage(
            "Downloading ffmpeg… it appears in the list and installs itself when finished.", 8000
        )

    def _disable_complete_dialog(self) -> None:
        self.manager.update_settings(replace(self.manager.settings, show_complete_dialog=False))

    def _on_tray_message_clicked(self) -> None:
        if self._last_completed is not None:
            self._reveal_task(self._last_completed)
            self._show_from_tray()

    @staticmethod
    def _open_task_file(task: DownloadTask) -> None:
        if task.final_path.exists():
            _open_path(task.final_path)
        else:
            _open_path(Path(task.dest_dir))

    @staticmethod
    def _reveal_task(task: DownloadTask) -> None:
        if task.final_path.exists():
            _reveal_in_explorer(task.final_path)
        else:
            _open_path(Path(task.dest_dir))

    def _on_clipboard(self) -> None:
        if not self.manager.settings.watch_clipboard:
            return
        text = QApplication.clipboard().text().strip()
        if text == self._last_clip or not looks_like_url(text):
            return
        self._last_clip = text
        ext = Path(text.split("?")[0].split("#")[0]).suffix.lower().lstrip(".")
        if ext in DOWNLOADABLE_HINT_EXTS:
            self._show_from_tray()
            self.add_url(text)

    def _paste_url(self) -> None:
        text = QApplication.clipboard().text().strip()
        lines = [ln.strip() for ln in text.splitlines() if looks_like_url(ln.strip())]
        if len(lines) > 1:
            self.add_url("\n".join(lines))
        elif lines:
            self.add_url(lines[0])
        else:
            self.statusBar().showMessage("Clipboard does not contain a link", 3000)

    # ---------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._urls_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        urls = self._urls_from_mime(event.mimeData())
        if not urls:
            return
        event.acceptProposedAction()
        if len(urls) == 1:
            self.add_url(urls[0])
        else:
            self.add_url("\n".join(urls))

    @staticmethod
    def _urls_from_mime(mime) -> list[str]:
        found: list[str] = []
        if mime.hasUrls():
            found = [u.toString() for u in mime.urls() if u.scheme() in ("http", "https", "ftp")]
        if not found and mime.hasText():
            found = [line.strip() for line in mime.text().splitlines() if looks_like_url(line.strip())]
        return found

    # ------------------------------------------------------------- actions
    def selected_tasks(self) -> list[DownloadTask]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        tasks = [self.proxy.index(r, 0).data(TaskRole) for r in rows]
        return [t for t in tasks if t is not None]

    def add_url(
        self,
        url: str = "",
        *,
        headers: dict[str, str] | None = None,
        kind: str = "auto",
        filename: str = "",
        referrer: str = "",
    ) -> None:
        if not url:
            clip = QApplication.clipboard().text().strip()
            if looks_like_url(clip):
                url = clip
        dlg = AddDownloadDialog(
            self.manager, self.theme, url, self, headers=headers, kind=kind, filename=filename, referrer=referrer
        )
        dlg.install_ffmpeg_requested.connect(self._install_ffmpeg_then_reopen)
        if dlg.exec() != AddDownloadDialog.DialogCode.Accepted:
            return
        futures = dlg.submit()
        if len(futures) > 1:
            self.statusBar().showMessage(f"Adding {len(futures)} downloads…", 4000)

        def done(f) -> None:
            try:
                f.result()  # selection happens via the "added" engine event on the GUI thread
            except Exception as exc:  # noqa: BLE001
                self._bridge.event.emit(
                    "add-failed", DownloadTask(url=dlg.url(), filename="", dest_dir="", error=str(exc))
                )

        for fut in futures:
            fut.add_done_callback(done)

    def _select_task(self, task_id: str) -> None:
        self.model.refresh()
        row = self.proxy.row_of(task_id)
        if row >= 0:
            self.table.selectRow(row)
            self.table.scrollTo(self.proxy.index(row, 0))

    def resume_selected(self) -> None:
        for t in self.selected_tasks():
            self.manager.start_task(t.id)

    def pause_selected(self) -> None:
        for t in self.selected_tasks():
            self.manager.pause_task(t.id)

    def _toggle_selected(self) -> None:
        for t in self.selected_tasks():
            if t.status in (Status.DOWNLOADING, Status.QUEUED):
                self.manager.pause_task(t.id)
            elif t.status in (Status.PAUSED, Status.ERROR):
                self.manager.start_task(t.id)

    def remove_selected(self, delete_files: bool = False) -> None:
        sel = self.selected_tasks()
        if not sel:
            return
        if delete_files:
            answer = QMessageBox.question(
                self, "Delete files", f"Remove {len(sel)} download(s) from the list AND delete the files?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif any(t.status == Status.DOWNLOADING for t in sel):
            answer = QMessageBox.question(self, "Remove", f"Stop and remove {len(sel)} download(s) from the list?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        for t in sel:
            self.manager.remove_task(t.id, delete_file=delete_files)

    def clear_completed(self) -> None:
        fut = self.manager.remove_completed()
        fut.add_done_callback(lambda f: None)
        self.statusBar().showMessage("Finished downloads removed from the list (files kept)", 4000)

    def move_selected(self, where: str) -> None:
        sel = self.selected_tasks()
        order = sel if where in ("top", "up") else list(reversed(sel))
        for t in order:
            self.manager.move_task(t.id, where)

    def copy_link_selected(self) -> None:
        sel = self.selected_tasks()
        if sel:
            QApplication.clipboard().setText("\n".join(t.url for t in sel))
            self._last_clip = QApplication.clipboard().text().strip()  # don't re-offer our own copy
            self.statusBar().showMessage("Link copied", 2000)

    def open_file_selected(self) -> None:
        for t in self.selected_tasks()[:1]:
            if t.status == Status.COMPLETED and t.final_path.exists():
                _open_path(t.final_path)
            else:
                _open_path(Path(t.dest_dir))

    def open_folder_selected(self) -> None:
        sel = self.selected_tasks()
        if sel:
            t = sel[0]
            if t.status == Status.COMPLETED and t.final_path.exists():
                _reveal_in_explorer(t.final_path)
            else:
                _open_path(Path(t.dest_dir))
        else:
            _open_path(Path(self.manager.settings.download_dir))

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.manager.settings, self, manager=self.manager)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            new = dlg.result_settings()
            old_theme = self.manager.settings.theme
            from . import winstartup

            if new.start_with_windows != winstartup.is_enabled():
                winstartup.set_enabled(new.start_with_windows)
            self.manager.update_settings(new)
            self._rebuild_sidebar()
            if new.theme != old_theme:
                self.set_theme_mode(new.theme)
            self._refresh_setup_card()

    def _context_menu(self, pos: QPoint) -> None:
        sel = self.selected_tasks()
        menu = QMenu(self)
        if sel:
            t = sel[0]
            done = t.status == Status.COMPLETED
            menu.addAction(self._icon("folder"), "Open" if done else "Open folder", self.open_file_selected)
            if done:
                menu.addAction("Show in folder", self.open_folder_selected)
            menu.addAction(self._icon("paste"), "Copy link", self.copy_link_selected)
            menu.addSeparator()
            menu.addAction(self._icon("play"), "Resume", self.resume_selected).setEnabled(self.act_resume.isEnabled())
            menu.addAction(self._icon("pause"), "Pause", self.pause_selected).setEnabled(self.act_pause.isEnabled())
            if not done:
                queue = menu.addMenu("Queue position")
                queue.addAction("Move to top", lambda: self.move_selected("top"))
                queue.addAction("Move up", lambda: self.move_selected("up"))
                queue.addAction("Move down", lambda: self.move_selected("down"))
                queue.addAction("Move to bottom", lambda: self.move_selected("bottom"))
            menu.addSeparator()
            menu.addAction(self._icon("trash"), "Remove from list", self.remove_selected)
            menu.addAction("Remove and delete file", lambda: self.remove_selected(True))
        else:
            menu.addAction(self._icon("download"), "Add link…", self.add_url)
            menu.addAction(self._icon("paste"), "Paste link(s) from clipboard", self._paste_url)
            menu.addSeparator()
            menu.addAction(self._icon("check"), "Clear finished", self.clear_completed)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ----------------------------------------------------------- lifecycle
    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._really_quit = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        really = getattr(self, "_really_quit", False)
        if self.tray and not really and any(t.status == Status.DOWNLOADING for t in self.manager.snapshot()):
            self.hide()
            self.tray.showMessage(
                "Still downloading", "IDM Clone keeps running in the tray. Right-click the icon to quit."
            )
            event.ignore()
            return
        self._timer.stop()
        for d in self._complete_dialogs:
            d.close()
        self.manager.on_event = None
        self.manager.stop()
        if self.tray:
            self.tray.hide()
        event.accept()
        QApplication.quit()


def _open_path(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


def _reveal_in_explorer(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        _open_path(path.parent)


def _system_sleep() -> None:
    try:
        if sys.platform == "win32":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        elif sys.platform == "darwin":
            subprocess.Popen(["pmset", "sleepnow"])
        else:
            subprocess.Popen(["systemctl", "suspend"])
    except OSError:
        pass


def _system_shutdown() -> None:
    try:
        if sys.platform == "win32":
            subprocess.Popen(["shutdown", "/s", "/t", "10"])
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", 'tell app "System Events" to shut down'])
        else:
            subprocess.Popen(["systemctl", "poweroff"])
    except OSError:
        pass
