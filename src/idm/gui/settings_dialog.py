"""Settings dialog."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..engine import Settings, find_ffmpeg

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _qtime(hhmm: str, fallback: str) -> QTime:
    t = QTime.fromString(hhmm, "HH:mm")
    return t if t.isValid() else QTime.fromString(fallback, "HH:mm")


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None, manager=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._original = settings
        self._manager = manager

        tabs = QTabWidget()

        # --- General
        general = QWidget()
        form = QFormLayout(general)
        self.dir_edit = QLineEdit(settings.download_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir_edit)
        row.addWidget(browse)
        form.addRow("Download folder:", row)

        self.use_categories = QCheckBox("Sort downloads into category sub-folders")
        self.use_categories.setChecked(settings.use_categories)
        form.addRow("", self.use_categories)

        self.watch_clipboard = QCheckBox("Offer to download URLs copied to the clipboard")
        self.watch_clipboard.setChecked(settings.watch_clipboard)
        form.addRow("", self.watch_clipboard)

        self.watch_sites = QCheckBox("…including video pages (YouTube, Facebook, TikTok, …), magnet and MEGA links")
        self.watch_sites.setChecked(settings.watch_clipboard_sites)
        form.addRow("", self.watch_sites)

        self.show_complete = QCheckBox("Show a 'Download complete' popup with Open / Open folder buttons")
        self.show_complete.setChecked(settings.show_complete_dialog)
        form.addRow("", self.show_complete)

        self.auto_resume = QCheckBox("Resume downloads that were interrupted by a crash or shutdown")
        self.auto_resume.setChecked(settings.auto_resume)
        form.addRow("", self.auto_resume)

        from . import winstartup

        self.start_with_windows = QCheckBox("Start IDM Clone when I sign in to Windows")
        self.start_with_windows.setChecked(winstartup.is_enabled() or settings.start_with_windows)
        form.addRow("", self.start_with_windows)
        self.start_minimized = QCheckBox("Start minimised to the tray")
        self.start_minimized.setChecked(settings.start_minimized)
        form.addRow("", self.start_minimized)
        self.check_updates = QCheckBox("Check for a newer version at startup")
        self.check_updates.setChecked(settings.check_updates)
        form.addRow("", self.check_updates)

        self.theme_box = QComboBox()
        for label, value in (("Follow Windows", "auto"), ("Light", "light"), ("Dark", "dark")):
            self.theme_box.addItem(label, value)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(settings.theme)))
        form.addRow("Appearance:", self.theme_box)
        tabs.addTab(general, "General")

        # --- Connection
        conn = QWidget()
        form = QFormLayout(conn)
        self.connections = QSpinBox()
        self.connections.setRange(1, 32)
        self.connections.setValue(settings.connections_per_download)
        form.addRow("Connections per download:", self.connections)

        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 20)
        self.concurrent.setValue(settings.max_concurrent_downloads)
        form.addRow("Simultaneous downloads:", self.concurrent)

        self.retries = QSpinBox()
        self.retries.setRange(0, 50)
        self.retries.setValue(settings.max_retries)
        form.addRow("Retries per segment:", self.retries)

        self.speed_limit = QSpinBox()
        self.speed_limit.setRange(0, 1_000_000)
        self.speed_limit.setSuffix(" KB/s")
        self.speed_limit.setSpecialValueText("Unlimited")
        self.speed_limit.setValue(settings.speed_limit_bytes // 1024)
        form.addRow("Speed limit per download:", self.speed_limit)

        self.proxy = QLineEdit(settings.proxy)
        self.proxy.setPlaceholderText("http://host:port (blank = system proxy)")
        form.addRow("Proxy:", self.proxy)

        self.user_agent = QLineEdit(settings.user_agent)
        form.addRow("User-Agent:", self.user_agent)

        self.logins = QListWidget()
        self.logins.setFixedHeight(84)
        self.logins.setToolTip("Sites you logged in to from the Add dialog; passwords are stored encrypted")
        if manager is not None:
            self.logins.addItems(manager.credentials.hosts())
        forget = QPushButton("Forget selected")
        forget.setAutoDefault(False)
        forget.clicked.connect(self._forget_login)
        logins_row = QHBoxLayout()
        logins_row.addWidget(self.logins, 1)
        logins_row.addWidget(forget, 0, Qt.AlignmentFlag.AlignTop)
        form.addRow("Saved logins:", logins_row)
        tabs.addTab(conn, "Connection")

        # --- Scheduler
        sched = QWidget()
        form = QFormLayout(sched)
        self.schedule_enabled = QCheckBox("Only run queued downloads inside this time window")
        self.schedule_enabled.setChecked(settings.schedule_enabled)
        form.addRow("", self.schedule_enabled)
        self.schedule_start = QTimeEdit(_qtime(settings.schedule_start, "23:00"))
        self.schedule_start.setDisplayFormat("HH:mm")
        form.addRow("Start downloading at:", self.schedule_start)
        self.schedule_stop = QTimeEdit(_qtime(settings.schedule_stop, "07:00"))
        self.schedule_stop.setDisplayFormat("HH:mm")
        form.addRow("Stop downloading at:", self.schedule_stop)
        days_row = QHBoxLayout()
        self.schedule_days: list[QCheckBox] = []
        for i, name in enumerate(DAY_NAMES):
            cb = QCheckBox(name)
            cb.setChecked(i in settings.schedule_days)
            self.schedule_days.append(cb)
            days_row.addWidget(cb)
        days_row.addStretch(1)
        form.addRow("On:", days_row)
        sched_note = QLabel(
            "A stop time earlier than the start time runs past midnight (23:00 to 07:00). "
            "Downloads running when the window closes are paused and continue automatically when it "
            "opens again; paused downloads are never started by the scheduler. Combine with "
            "'When done' in the status bar to shut down afterwards."
        )
        sched_note.setObjectName("muted")
        sched_note.setWordWrap(True)
        form.addRow("", sched_note)
        for w in (self.schedule_start, self.schedule_stop, *self.schedule_days):
            w.setEnabled(settings.schedule_enabled)
            self.schedule_enabled.toggled.connect(w.setEnabled)
        tabs.addTab(sched, "Scheduler")

        # --- Video
        video = QWidget()
        form = QFormLayout(video)
        self.ffmpeg = QLineEdit(settings.ffmpeg_path)
        self.ffmpeg.setPlaceholderText("auto-detect (PATH, %LOCALAPPDATA%\\ffmpeg\\bin, next to the app)")
        ff_browse = QPushButton("Browse…")
        ff_browse.setAutoDefault(False)
        ff_browse.clicked.connect(self._browse_ffmpeg)
        ff_row = QHBoxLayout()
        ff_row.addWidget(self.ffmpeg)
        ff_row.addWidget(ff_browse)
        form.addRow("ffmpeg:", ff_row)
        found = find_ffmpeg(settings.ffmpeg_path)
        status = QLabel(
            f"Found: {found}"
            if found
            else "Not found. Without ffmpeg only progressive formats (usually up to 720p) and audio-only "
            "are offered. Get a build from https://www.gyan.dev/ffmpeg/builds/ and point this field at ffmpeg.exe."
        )
        status.setObjectName("muted")
        status.setWordWrap(True)
        form.addRow("", status)
        if not found:
            get_btn = QPushButton("Download ffmpeg automatically (~90 MB)")
            get_btn.setAutoDefault(False)
            get_btn.setToolTip(
                "Fetches a static Windows build from github.com/BtbN/FFmpeg-Builds into the app data folder"
            )
            get_btn.clicked.connect(self._download_ffmpeg)
            form.addRow("", get_btn)
        tabs.addTab(video, "Video")

        # --- Torrents
        torrents = QWidget()
        form = QFormLayout(torrents)
        self.torrent_port = QSpinBox()
        self.torrent_port.setRange(0, 65535)
        self.torrent_port.setSpecialValueText("Random")
        self.torrent_port.setValue(settings.torrent_port)
        self.torrent_port.setToolTip("Port for incoming peer connections; UPnP / NAT-PMP mapping is attempted")
        form.addRow("Listening port:", self.torrent_port)

        self.torrent_seed = QCheckBox("Keep seeding finished torrents while IDM Clone is running")
        self.torrent_seed.setChecked(settings.torrent_seed)
        form.addRow("", self.torrent_seed)

        self.torrent_upload = QSpinBox()
        self.torrent_upload.setRange(0, 1_000_000)
        self.torrent_upload.setSuffix(" KB/s")
        self.torrent_upload.setSpecialValueText("Unlimited")
        self.torrent_upload.setValue(settings.torrent_upload_limit_kb)
        form.addRow("Upload limit:", self.torrent_upload)

        self.torrent_conns = QSpinBox()
        self.torrent_conns.setRange(10, 2000)
        self.torrent_conns.setValue(settings.torrent_max_connections)
        form.addRow("Max peer connections:", self.torrent_conns)

        self.trackers = QPlainTextEdit()
        self.trackers.setPlaceholderText("udp://tracker.example.org:1337/announce")
        self.trackers.setPlainText("\n".join(settings.torrent_trackers))
        self.trackers.setFixedHeight(96)
        self.trackers.setToolTip("Added to every magnet link so bare magnets find peers faster (DHT is always on)")
        form.addRow("Extra trackers:", self.trackers)

        from . import protocols

        self.handle_magnets = QCheckBox(
            "Open magnet links and .torrent files from the browser and Explorer with IDM Clone"
        )
        self.handle_magnets.setChecked(protocols.is_registered() or settings.handle_magnet_links)
        form.addRow("", self.handle_magnets)
        note = QLabel(
            "Torrents are saved into the 'Torrents' sub-folder when category sorting is on. "
            "The download speed limit on the Connection tab applies per torrent too."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        form.addRow("", note)
        tabs.addTab(torrents, "Torrents")

        # --- Browser
        browser = QWidget()
        lay = QVBoxLayout(browser)
        self.browser_integration = QCheckBox("Accept downloads from the browser extension (takes effect after restart)")
        self.browser_integration.setChecked(settings.browser_integration)
        lay.addWidget(self.browser_integration)
        import sys

        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]
        ext_dir = base / "extension"
        steps = QLabel(
            "<b>Set up once</b><br>"
            "1. The installer registers the native host for Chrome, Edge and Brave (or run <code>scripts/register-browser.ps1</code> from source).<br>"
            "2. Open <code>chrome://extensions</code>, enable Developer mode, click <i>Load unpacked</i> and pick:<br>"
            f"&nbsp;&nbsp;&nbsp;<code>{ext_dir}</code><br>"
            "3. Click the extension icon on any page to grab its videos, or download a file as usual: it is caught here."
        )
        steps.setWordWrap(True)
        steps.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(steps)
        lay.addStretch(1)
        tabs.addTab(browser, "Browser")

        # --- Categories
        cats = QWidget()
        lay = QVBoxLayout(cats)
        self.categories = QPlainTextEdit()
        self.categories.setPlaceholderText("Video: mp4 mkv avi\nMusic: mp3 flac")
        self.categories.setPlainText(
            "\n".join(f"{name}: {' '.join(exts)}" for name, exts in settings.categories.items())
        )
        lay.addWidget(self.categories)
        tabs.addTab(cats, "Categories")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addWidget(buttons)

    def _forget_login(self) -> None:
        for item in self.logins.selectedItems():
            if self._manager is not None:
                self._manager.forget_credential(item.text())
            self.logins.takeItem(self.logins.row(item))

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Download folder", self.dir_edit.text())
        if folder:
            self.dir_edit.setText(folder)

    def _download_ffmpeg(self) -> None:
        if self._manager is None:
            return
        self._manager.install_ffmpeg()
        self.accept()

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate ffmpeg.exe", "", "ffmpeg (ffmpeg.exe ffmpeg);;All files (*)"
        )
        if path:
            self.ffmpeg.setText(path)

    def _parse_categories(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for line in self.categories.toPlainText().splitlines():
            if ":" not in line:
                continue
            name, _, exts = line.partition(":")
            name = name.strip()
            ext_list = [e.strip().lower().lstrip(".") for e in exts.replace(",", " ").split()]
            if name and ext_list:
                result[name] = ext_list
        return result

    def result_settings(self) -> Settings:
        return replace(
            self._original,
            download_dir=self.dir_edit.text().strip() or self._original.download_dir,
            use_categories=self.use_categories.isChecked(),
            watch_clipboard=self.watch_clipboard.isChecked(),
            watch_clipboard_sites=self.watch_sites.isChecked(),
            torrent_port=self.torrent_port.value(),
            torrent_seed=self.torrent_seed.isChecked(),
            torrent_upload_limit_kb=self.torrent_upload.value(),
            torrent_max_connections=self.torrent_conns.value(),
            torrent_trackers=[ln.strip() for ln in self.trackers.toPlainText().splitlines() if ln.strip()],
            handle_magnet_links=self.handle_magnets.isChecked(),
            connections_per_download=self.connections.value(),
            max_concurrent_downloads=self.concurrent.value(),
            max_retries=self.retries.value(),
            speed_limit_bytes=self.speed_limit.value() * 1024,
            proxy=self.proxy.text().strip(),
            user_agent=self.user_agent.text().strip() or self._original.user_agent,
            categories=self._parse_categories() or self._original.categories,
            ffmpeg_path=self.ffmpeg.text().strip(),
            browser_integration=self.browser_integration.isChecked(),
            show_complete_dialog=self.show_complete.isChecked(),
            auto_resume=self.auto_resume.isChecked(),
            theme=self.theme_box.currentData() or "auto",
            start_with_windows=self.start_with_windows.isChecked(),
            start_minimized=self.start_minimized.isChecked(),
            check_updates=self.check_updates.isChecked(),
            schedule_enabled=self.schedule_enabled.isChecked(),
            schedule_start=self.schedule_start.time().toString("HH:mm"),
            schedule_stop=self.schedule_stop.time().toString("HH:mm"),
            schedule_days=[i for i, cb in enumerate(self.schedule_days) if cb.isChecked()],
        )
