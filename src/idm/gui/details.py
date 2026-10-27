"""Segment map widget, speed graph, and the details pane shown under the download list."""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from ..engine import DownloadTask, Status
from .icons import icon
from .theme import Theme
from .util import fmt_bytes, fmt_eta, fmt_speed, fmt_time


def paint_segment_map(p: QPainter, rect: QRectF, task: DownloadTask, theme: Theme, radius: float = 3.0) -> None:
    """Draw the per-segment progress of `task` into `rect`: track + one filled run per segment."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(theme.track)
    p.drawRoundedRect(rect, radius, radius)

    color = theme.status_color(task.status)
    if task.status == Status.COMPLETED or not task.size:
        if task.status == Status.COMPLETED:
            p.setBrush(color)
            p.drawRoundedRect(rect, radius, radius)
        elif task.status == Status.DOWNLOADING:
            frac = (task.downloaded % (64 * 1024 * 1024)) / (64 * 1024 * 1024)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width() * max(frac, 0.02), rect.height()), radius, radius)
        p.restore()
        return

    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.setClipPath(path)
    size = task.size
    w = rect.width()
    for seg in task.segments:
        if seg.downloaded <= 0:
            continue
        x0 = rect.x() + w * (seg.start / size)
        x1 = rect.x() + w * ((seg.start + seg.downloaded) / size)
        p.setBrush(color)
        p.drawRect(QRectF(x0, rect.y(), max(1.0, x1 - x0), rect.height()))
    if len(task.segments) > 1 and w > 60:
        sep = QColor(theme.bg)
        sep.setAlpha(170)
        p.setBrush(sep)
        for seg in task.segments[1:]:
            x = rect.x() + w * (seg.start / size)
            p.drawRect(QRectF(x - 0.5, rect.y(), 1.0, rect.height()))
    p.restore()


class SegmentMap(QWidget):
    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.task: DownloadTask | None = None
        self.setFixedHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_task(self, task: DownloadTask | None) -> None:
        self.task = task
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if not self.task:
            return
        p = QPainter(self)
        paint_segment_map(p, QRectF(self.rect()).adjusted(0, 2, 0, -2), self.task, self.theme, radius=5.0)
        p.end()


class SpeedGraph(QWidget):
    """Last 60 seconds of transfer speed for one task, as a filled line."""

    WINDOW = 60.0

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.samples: deque[tuple[float, float]] = deque()
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def reset(self) -> None:
        self.samples.clear()
        self.update()

    def add_sample(self, speed: float) -> None:
        now = time.monotonic()
        self.samples.append((now, speed))
        cutoff = now - self.WINDOW
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.theme.track)
        p.drawRoundedRect(r, 5, 5)
        if len(self.samples) < 2:
            p.end()
            return
        now = time.monotonic()
        peak = max(s for _, s in self.samples) or 1.0
        path = QPainterPath()
        pts = []
        for t, s in self.samples:
            x = r.x() + r.width() * (1 - (now - t) / self.WINDOW)
            y = r.bottom() - (r.height() - 4) * (s / peak) - 2
            pts.append(QPointF(max(r.x(), x), y))
        path.moveTo(pts[0].x(), r.bottom())
        for pt in pts:
            path.lineTo(pt)
        path.lineTo(pts[-1].x(), r.bottom())
        path.closeSubpath()
        fill = QColor(self.theme.accent)
        fill.setAlpha(70)
        p.setBrush(fill)
        p.drawPath(path)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self.theme.accent, 1.5))
        line = QPainterPath()
        line.moveTo(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)
        p.drawPath(line)
        p.setPen(self.theme.muted)
        p.drawText(
            r.adjusted(8, 2, -8, 0),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            f"last 60 s · peak {fmt_speed(peak)}",
        )
        p.end()


class DetailsPane(QFrame):
    """Everything about the selected download, with the actions you need most often."""

    open_file = Signal()
    open_folder = Signal()
    copy_link = Signal()
    toggle_pause = Signal()
    remove = Signal()

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("details")
        self.theme = theme
        self.task: DownloadTask | None = None
        self._graph_task_id: str | None = None

        self.title = QLabel()
        self.title.setObjectName("title")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary = QLabel()
        self.summary.setObjectName("muted")
        self.url = QLabel()
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.setObjectName("muted")
        self.path = QLabel()
        self.path.setObjectName("link")
        self.path.setCursor(Qt.CursorShape.PointingHandCursor)
        self.path.setToolTip("Show in folder")
        self.path.mousePressEvent = lambda _e: self.open_folder.emit()  # type: ignore[method-assign]
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        self.error.hide()
        self.segments = SegmentMap(theme)
        self.segments_label = QLabel()
        self.segments_label.setObjectName("muted")
        self.graph = SpeedGraph(theme)

        # Action buttons
        self.btn_open = self._button("folder", "Open", self.open_file, "Open the file (Enter)")
        self.btn_folder = self._button("folder", "Show in folder", self.open_folder, "Reveal the file in Explorer")
        self.btn_copy = self._button("paste", "Copy link", self.copy_link, "Copy the download URL")
        self.btn_toggle = self._button("pause", "Pause", self.toggle_pause, "Pause or resume (Space)")
        self.btn_remove = self._button("trash", "Remove", self.remove, "Remove from the list (Delete)")
        actions = QHBoxLayout()
        actions.setSpacing(6)
        for b in (self.btn_open, self.btn_folder, self.btn_copy, self.btn_toggle, self.btn_remove):
            actions.addWidget(b)
        actions.addStretch(1)

        grid = QGridLayout(self)
        grid.setContentsMargins(16, 10, 16, 10)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addLayout(actions)
        grid.addLayout(head, 0, 0, 1, 2)
        grid.addWidget(self.summary, 1, 0, 1, 2)
        grid.addWidget(self._key("URL"), 2, 0)
        grid.addWidget(self.url, 2, 1)
        grid.addWidget(self._key("Saved to"), 3, 0)
        grid.addWidget(self.path, 3, 1)
        grid.addWidget(self._key("Segments"), 4, 0)
        seg_row = QHBoxLayout()
        seg_row.setSpacing(10)
        seg_row.addWidget(self.segments, 1)
        seg_row.addWidget(self.segments_label)
        grid.addLayout(seg_row, 4, 1)
        grid.addWidget(self._key("Speed"), 5, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.graph, 5, 1)
        grid.addWidget(self.error, 6, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        self.setFixedHeight(self.sizeHint().height() + 8)

    def _button(self, icon_name: str, text: str, signal: Signal, tip: str) -> QPushButton:
        b = QPushButton(icon(icon_name, self.theme.text, 16), text)
        b.setObjectName("detailsButton")
        b.setAutoDefault(False)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(signal.emit)
        return b

    @staticmethod
    def _key(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return lbl

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.segments.theme = theme
        self.graph.theme = theme
        for b, name in (
            (self.btn_open, "folder"),
            (self.btn_folder, "folder"),
            (self.btn_copy, "paste"),
            (self.btn_remove, "trash"),
        ):
            b.setIcon(icon(name, theme.text, 16))
        self.refresh()

    def set_task(self, task: DownloadTask | None) -> None:
        self.task = task
        self.segments.set_task(task)
        if task is None or task.id != self._graph_task_id:
            self.graph.reset()
            self._graph_task_id = task.id if task else None
        self.refresh()

    def refresh(self) -> None:
        t = self.task
        has = t is not None
        for b in (self.btn_open, self.btn_folder, self.btn_copy, self.btn_toggle, self.btn_remove):
            b.setVisible(has)
        if t is None:
            self.title.setText("No download selected")
            self.summary.setText("Select a download to see its details and actions.")
            self.url.setText("")
            self.path.setText("")
            self.segments_label.setText("")
            self.error.hide()
            return
        self.title.setText(t.filename)
        parts = [fmt_bytes(t.size) if t.size else "unknown size"]
        if t.status == Status.DOWNLOADING:
            if t.progress is not None:
                parts.append(f"{t.progress * 100:.1f}% done")
            parts.append(fmt_bytes(t.downloaded) + " received")
            if t.speed:
                parts.append(fmt_speed(t.speed))
            if t.eta is not None:
                parts.append(fmt_eta(t.eta) + " left")
            self.graph.add_sample(t.speed)
        elif t.status == Status.COMPLETED:
            parts.append("completed " + fmt_time(t.completed_at))
        elif t.status == Status.PAUSED:
            parts.append(f"paused at {fmt_bytes(t.downloaded)}")
            parts.append("resumable" if t.resumable else "will restart from the beginning")
        elif t.status == Status.QUEUED:
            parts.append("waiting in queue")
        if t.kind == "media" and t.title:
            parts.append(t.title)
        self.summary.setText("  ·  ".join(parts))
        self.url.setText(_elide(t.url, 110))
        self.url.setToolTip(t.url)
        self.path.setText(str(t.final_path if t.status == Status.COMPLETED else t.dest_dir))
        active = sum(1 for s in t.segments if not s.done)
        if t.status == Status.DOWNLOADING:
            self.segments_label.setText(f"{active} active / {len(t.segments)} total · {t.connections} max")
        else:
            self.segments_label.setText(f"{len(t.segments)} segments" if t.segments else "")
        if t.error:
            self.error.setText(f"Error: {t.error}")
            self.error.show()
        else:
            self.error.hide()
        running = t.status in (Status.DOWNLOADING, Status.QUEUED)
        self.btn_toggle.setText("Pause" if running else ("Retry" if t.status == Status.ERROR else "Resume"))
        self.btn_toggle.setIcon(icon("pause" if running else "play", self.theme.text, 16))
        self.btn_toggle.setVisible(t.status != Status.COMPLETED)
        self.btn_open.setText("Open" if t.status == Status.COMPLETED else "Open folder")
        self.btn_folder.setVisible(t.status == Status.COMPLETED)
        self.segments.update()


def _elide(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"
