"""The 'Download complete' popup shown when a file finishes (like IDM's)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..engine import DownloadTask, Settings
from .icons import CATEGORY_ICONS, icon
from .theme import Theme
from .util import fmt_bytes


class DownloadCompleteDialog(QDialog):
    def __init__(
        self,
        task: DownloadTask,
        theme: Theme,
        settings: Settings,
        *,
        on_open: Callable[[DownloadTask], None],
        on_open_folder: Callable[[DownloadTask], None],
        on_dont_show: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task = task
        self._on_dont_show = on_dont_show
        self.setWindowTitle("Download complete")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.setMinimumWidth(460)

        if task.kind == "media":
            icon_name = "video"
        else:
            icon_name = CATEGORY_ICONS.get(settings.category_for(task.filename) or "", "file")
        pic = QLabel()
        pic.setPixmap(icon(icon_name, theme.success, 44).pixmap(44, 44))
        pic.setFixedSize(44, 44)

        title = QLabel(task.filename)
        title.setObjectName("title")
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sub = QLabel("  ·  ".join(x for x in (fmt_bytes(task.size) if task.size else "", task.title) if x))
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        where = QLabel(f"Saved to  {task.dest_dir}")
        where.setObjectName("muted")
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        texts = QVBoxLayout()
        texts.setSpacing(3)
        texts.addWidget(title)
        texts.addWidget(sub)
        texts.addWidget(where)
        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(texts, 1)

        self.dont_show = QCheckBox("Don't show this dialog again")
        open_btn = QPushButton("Open")
        open_btn.setObjectName("primary")
        open_btn.setDefault(True)
        open_btn.clicked.connect(lambda: (on_open(task), self.close()))
        folder_btn = QPushButton("Open folder")
        folder_btn.setAutoDefault(False)
        folder_btn.clicked.connect(lambda: (on_open_folder(task), self.close()))
        close_btn = QPushButton("Close")
        close_btn.setAutoDefault(False)
        close_btn.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.dont_show)
        buttons.addStretch(1)
        buttons.addWidget(open_btn)
        buttons.addWidget(folder_btn)
        buttons.addWidget(close_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(16)
        root.addLayout(top)
        root.addLayout(buttons)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.dont_show.isChecked():
            self._on_dont_show()
        super().closeEvent(event)
