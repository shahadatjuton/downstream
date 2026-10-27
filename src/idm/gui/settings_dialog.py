"""Settings dialog."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
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
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..engine import Settings, find_ffmpeg


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
        tabs.addTab(conn, "Connection")

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
        )
