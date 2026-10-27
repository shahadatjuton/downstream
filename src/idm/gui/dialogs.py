"""About, Check-for-updates, and Browser-setup / Welcome dialogs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..engine import DownloadManager
from .icons import icon
from .theme import Theme

REPO = "shahadatjuton/idm-clone"
REPO_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"{REPO_URL}/releases/latest"
ISSUES_URL = f"{REPO_URL}/issues/new/choose"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def _vtuple(v: str) -> tuple[int, ...]:
    import re

    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


# ------------------------------------------------------------------ About
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


# --------------------------------------------------------- Update checker
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


# ------------------------------------------------------------ Browsers
BROWSERS = (
    # (label, exe candidates, extensions page)
    ("Google Chrome", ("Google\\Chrome\\Application\\chrome.exe",), "chrome://extensions"),
    ("Microsoft Edge", ("Microsoft\\Edge\\Application\\msedge.exe",), "edge://extensions"),
    ("Brave", ("BraveSoftware\\Brave-Browser\\Application\\brave.exe",), "brave://extensions"),
)


def installed_browsers() -> list[tuple[str, str, str]]:
    """[(label, exe path, extensions url)] for the browsers found on this machine."""
    found = []
    roots = [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for label, rels, page in BROWSERS:
        for root in roots:
            if not root:
                continue
            for rel in rels:
                exe = Path(root) / rel
                if exe.exists():
                    found.append((label, str(exe), page))
                    break
            else:
                continue
            break
    return found


def extension_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]
    return base / "extension"


def open_extensions_page(exe: str, page: str) -> bool:
    try:
        subprocess.Popen([exe, page])
        return True
    except OSError:
        return False


class BrowserSetupDialog(QDialog):
    """Guided one-time extension setup. Also used as the first-run welcome window.

    The installer already registered the native host; the user only has to load the
    unpacked extension once. The dialog turns green as soon as the extension says hello.
    """

    def __init__(
        self,
        theme: Theme,
        manager: DownloadManager,
        registered: bool,
        parent: QWidget | None = None,
        *,
        welcome: bool = False,
    ) -> None:
        super().__init__(parent)
        self.theme = theme
        self.manager = manager
        self.welcome = welcome
        self.setWindowTitle("Welcome to IDM Clone" if welcome else "Browser setup")
        self.setMinimumWidth(600)
        ext = extension_dir()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(12)

        if welcome:
            head = QHBoxLayout()
            pic = QLabel()
            pic.setPixmap(parent.windowIcon().pixmap(48, 48) if parent else QApplication.windowIcon().pixmap(48, 48))
            pic.setFixedSize(48, 48)
            t = QLabel("IDM Clone is installed. One more step connects it to your browser.")
            t.setObjectName("title")
            t.setWordWrap(True)
            head.addWidget(pic)
            head.addWidget(t, 1)
            root.addLayout(head)
            why = QLabel(
                "With the extension, downloads you start in the browser are caught here, a <b>Download</b> "
                "button appears over videos on web pages, and YouTube and 1000+ sites can be saved in the "
                "quality you choose."
            )
            why.setObjectName("muted")
            why.setWordWrap(True)
            root.addWidget(why)

        # Status card
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
        cl.setSpacing(4)
        self.host_status = QLabel()
        self.host_status.setWordWrap(True)
        self.ext_status = QLabel()
        self.ext_status.setWordWrap(True)
        cl.addWidget(self.host_status)
        cl.addWidget(self.ext_status)
        root.addWidget(card)
        self._registered = registered

        steps = QLabel(
            "<b>Load the extension (one time)</b><br>"
            "1. Open your browser's extensions page with a button below.<br>"
            "2. Turn on <b>Developer mode</b> (top right).<br>"
            "3. Click <b>Load unpacked</b> and choose this folder (use <b>Copy path</b>):"
        )
        steps.setWordWrap(True)
        root.addWidget(steps)
        path_row = QHBoxLayout()
        path = QLabel(str(ext))
        path.setObjectName("link")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        copy_btn = QPushButton("Copy path")
        copy_btn.setAutoDefault(False)
        copy_btn.clicked.connect(lambda: (QApplication.clipboard().setText(str(ext)), copy_btn.setText("Copied ✓")))
        folder_btn = QPushButton("Show folder")
        folder_btn.setAutoDefault(False)
        folder_btn.clicked.connect(lambda: _open(ext))
        path_row.addWidget(path, 1)
        path_row.addWidget(copy_btn)
        path_row.addWidget(folder_btn)
        root.addLayout(path_row)

        browsers = installed_browsers()
        row = QHBoxLayout()
        row.setSpacing(8)
        if browsers:
            for i, (label, exe, page) in enumerate(browsers):
                b = QPushButton(
                    icon("program", theme.text if i else QApplication.palette().base().color(), 16),
                    f"Open {label} extensions",
                )
                if i == 0:
                    b.setObjectName("primary")
                    b.setDefault(True)
                else:
                    b.setAutoDefault(False)
                b.clicked.connect(lambda _c=False, e=exe, p=page: self._open(e, p))
                row.addWidget(b)
        else:
            none = QLabel(
                "No Chrome, Edge or Brave installation was found. Install one, then open its extensions page."
            )
            none.setObjectName("warn")
            none.setWordWrap(True)
            row.addWidget(none)
        row.addStretch(1)
        root.addLayout(row)

        self.later = QCheckBox("Don't show this again")
        bottom = QHBoxLayout()
        if welcome:
            bottom.addWidget(self.later)
        bottom.addStretch(1)
        done = QPushButton("Done" if welcome else "Close")
        done.setAutoDefault(False)
        done.clicked.connect(self.accept)
        bottom.addWidget(done)
        root.addLayout(bottom)

        self._refresh_status()
        self._poll = QTimer(self)
        self._poll.setInterval(1500)
        self._poll.timeout.connect(self._refresh_status)
        self._poll.start()

    def _open(self, exe: str, page: str) -> None:
        if not open_extensions_page(exe, page):
            QMessageBox.warning(self, "Browser", f"Could not start {exe}")

    def _refresh_status(self) -> None:
        from .main_window import host_registered

        reg = self._registered or host_registered()
        self._registered = reg
        self.host_status.setText(
            "✓  Native host registered: Chrome, Edge and Brave can reach IDM Clone."
            if reg
            else "•  Native host not registered. Reinstall with 'Browser integration' ticked, or run scripts\\register-browser.ps1."
        )
        self.host_status.setObjectName("ok" if reg else "warn")
        seen = self.manager.settings.extension_seen
        self.ext_status.setText(
            "✓  Extension connected. Downloads and page videos are sent to IDM Clone."
            if seen
            else "•  Extension not loaded yet. Follow the steps below; this line turns green automatically."
        )
        self.ext_status.setObjectName("ok" if seen else "warn")
        for w in (self.host_status, self.ext_status):
            w.style().unpolish(w)
            w.style().polish(w)

    def accept(self) -> None:
        self._poll.stop()
        super().accept()

    def reject(self) -> None:
        self._poll.stop()
        super().reject()

    def dont_show_again(self) -> bool:
        return self.later.isChecked()


def _open(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass
