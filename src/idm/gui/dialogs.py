"""About, Check-for-updates, and Browser-setup dialogs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..engine import DownloadManager
from .theme import Theme

REPO = "shahadatjuton/idm-clone"
REPO_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"{REPO_URL}/releases/latest"
ISSUES_URL = f"{REPO_URL}/issues/new/choose"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def _vtuple(v: str) -> tuple[int, ...]:
    import re

    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


class AboutDialog(QDialog):
    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About IDM Clone")
        self.setMinimumWidth(460)
        pic = QLabel()
        pic.setPixmap(parent.windowIcon().pixmap(64, 64) if parent else QApplication.windowIcon().pixmap(64, 64))
        pic.setFixedSize(64, 64)
        title = QLabel(f"IDM Clone {__version__}")
        title.setObjectName("title")
        sub = QLabel(
            "Open-source download manager for Windows: segmented downloads, resume, browser integration, "
            "and video downloads through yt-dlp."
        )
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        links = QLabel(
            f'<a href="{REPO_URL}">Source code</a> · <a href="{RELEASES_URL}">Releases</a> · '
            f'<a href="{ISSUES_URL}">Report a problem</a>'
        )
        links.setOpenExternalLinks(True)
        legal = QLabel("MIT License. Not affiliated with Tonec Inc. or Internet Download Manager.")
        legal.setObjectName("muted")
        legal.setWordWrap(True)
        texts = QVBoxLayout()
        texts.setSpacing(6)
        texts.addWidget(title)
        texts.addWidget(sub)
        texts.addWidget(links)
        texts.addWidget(legal)
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(texts, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(16)
        root.addLayout(top)
        root.addWidget(buttons)


class UpdateChecker(QObject):
    """Asks the GitHub releases API for the newest tag. Emits on the GUI thread."""

    result = Signal(object, object)  # (latest version str | None, error str | None)

    def __init__(self, manager: DownloadManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.manager = manager

    def check(self) -> None:
        fut = self.manager.fetch_bytes(LATEST_API, limit=200_000)

        def done(f) -> None:
            try:
                data = json.loads(f.result().decode("utf-8"))
                tag = str(data.get("tag_name") or "").lstrip("v")
                self.result.emit(tag or None, None if tag else "no release found")
            except Exception as exc:  # noqa: BLE001
                self.result.emit(None, f"{type(exc).__name__}: {exc}")

        fut.add_done_callback(done)

    @staticmethod
    def show_result(parent: QWidget, latest: str | None, error: str | None, silent_if_current: bool = False) -> None:
        if error or not latest:
            if not silent_if_current:
                QMessageBox.warning(parent, "Check for updates", f"Could not check for updates.\n{error or ''}")
            return
        if _vtuple(latest) > _vtuple(__version__):
            box = QMessageBox(parent)
            box.setWindowTitle("Update available")
            box.setText(f"IDM Clone {latest} is available (you have {__version__}).")
            box.setInformativeText("Open the download page?")
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if box.exec() == QMessageBox.StandardButton.Yes:
                webbrowser.open(RELEASES_URL)
        elif not silent_if_current:
            QMessageBox.information(parent, "Check for updates", f"You are up to date (IDM Clone {__version__}).")


def extension_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]
    return base / "extension"


class BrowserSetupDialog(QDialog):
    """Step-by-step: the installer registered the host; the user still loads the extension once."""

    def __init__(self, theme: Theme, registered: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Browser setup")
        self.setMinimumWidth(560)
        ext = extension_dir()
        status = QLabel(
            "✓  Native host registered. Chrome, Edge and Brave can talk to IDM Clone."
            if registered
            else "•  Native host not registered yet. Run scripts\\register-browser.ps1, or reinstall with the "
            "'Browser integration' option ticked."
        )
        status.setObjectName("ok" if registered else "warn")
        status.setWordWrap(True)
        steps = QLabel(
            "<b>Load the extension (one time)</b><br>"
            "1. Click <b>Open extensions page</b> below (or type <code>chrome://extensions</code>).<br>"
            "2. Turn on <b>Developer mode</b> (top right).<br>"
            "3. Click <b>Load unpacked</b> and choose the folder below (use <b>Copy path</b>).<br>"
            "4. Pin the IDM Clone icon. Downloads and page videos are now sent here."
        )
        steps.setWordWrap(True)
        path = QLabel(str(ext))
        path.setObjectName("link")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        open_btn = QPushButton("Open extensions page")
        open_btn.setObjectName("primary")
        open_btn.clicked.connect(self._open_extensions_page)
        copy_btn = QPushButton("Copy path")
        copy_btn.setAutoDefault(False)
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(str(ext)))
        folder_btn = QPushButton("Show folder")
        folder_btn.setAutoDefault(False)
        folder_btn.clicked.connect(lambda: _open(ext))
        row = QHBoxLayout()
        row.addWidget(open_btn)
        row.addWidget(copy_btn)
        row.addWidget(folder_btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(12)
        root.addWidget(status)
        root.addWidget(steps)
        root.addWidget(path)
        root.addLayout(row)
        root.addWidget(buttons)

    @staticmethod
    def _open_extensions_page() -> None:
        # chrome:// URLs cannot be opened through the default-browser handler; try the browsers directly.
        for exe in ("chrome", "msedge", "brave"):
            try:
                subprocess.Popen([exe, "chrome://extensions" if exe != "msedge" else "edge://extensions"])
                return
            except OSError:
                continue
        webbrowser.open("https://github.com/shahadatjuton/idm-clone#browser-setup-once")


def _open(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass
