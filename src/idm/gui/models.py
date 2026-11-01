"""Qt models over the manager's task list: the table model, a search/sort proxy, and the
progress/segment-map delegate."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSize, QSortFilterProxyModel, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..engine import DownloadManager, DownloadTask, Settings, Status
from .details import paint_segment_map
from .icons import CATEGORY_ICONS, icon
from .theme import Theme
from .util import fmt_bytes, fmt_eta, fmt_speed, fmt_time, short_error

COLUMNS = ["File name", "Size", "Status", "Progress", "Speed", "Time left", "Description", "Added"]
COL_NAME, COL_SIZE, COL_STATUS, COL_PROGRESS, COL_SPEED, COL_ETA, COL_DESC, COL_ADDED = range(8)
ROW_HEIGHT = 44

TaskRole = Qt.ItemDataRole.UserRole + 1
SortRole = Qt.ItemDataRole.UserRole + 2

STATUS_LABEL = {
    Status.QUEUED: "Queued",
    Status.DOWNLOADING: "Downloading",
    Status.PAUSED: "Paused",
    Status.COMPLETED: "Complete",
    Status.ERROR: "Error",
}
STATUS_ORDER = {Status.DOWNLOADING: 0, Status.QUEUED: 1, Status.PAUSED: 2, Status.ERROR: 3, Status.COMPLETED: 4}


def status_text(task: DownloadTask) -> str:
    """Short, sortable status for the Status column: 'Downloading', 'Error · HTTP 403', ..."""
    label = STATUS_LABEL[task.status]
    if task.status == Status.ERROR and task.error:
        return f"{label} · {short_error(task.error)}"
    if task.status == Status.DOWNLOADING and task.note:
        return f"{label} · {task.note}"
    return label


def description_text(task: DownloadTask) -> str:
    """Description column: the page/video title when known, else the site the file comes from."""
    if task.title and task.title != task.filename:
        return task.title
    if task.kind == "torrent":
        return "Torrent"
    from urllib.parse import urlparse

    host = urlparse(task.origin or task.url).hostname or ""
    return host[4:] if host.startswith("www.") else host


class TaskFilter:
    """Which tasks the main list shows. `name` is a category name or one of the pseudo-filters."""

    ALL = "All downloads"
    ACTIVE = "Unfinished"
    DONE = "Finished"
    TORRENTS = "Torrents"
    PSEUDO = (ALL, ACTIVE, DONE)

    def __init__(self, name: str = ALL) -> None:
        self.name = name

    def accepts(self, task: DownloadTask, settings: Settings) -> bool:
        if self.name == self.ALL:
            return True
        if self.name == self.ACTIVE:
            return task.status != Status.COMPLETED
        if self.name == self.DONE:
            return task.status == Status.COMPLETED
        if self.name == self.TORRENTS:
            return task.kind == "torrent"
        if task.kind == "torrent":
            return False  # a torrent's folder name has no extension; keep it out of "Documents" etc.
        return settings.category_for(task.filename) == self.name


def icon_name_for(task: DownloadTask, settings: Settings) -> str:
    if task.kind == "media":
        return "video"
    if task.kind == "torrent":
        return "torrent"
    if task.kind == "mega":
        return "cloud"
    cat = settings.category_for(task.filename)
    return CATEGORY_ICONS.get(cat or "", "file")


class TaskTableModel(QAbstractTableModel):
    def __init__(self, manager: DownloadManager, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.theme = theme
        self.filter = TaskFilter()
        self._tasks: list[DownloadTask] = []

    # ------------------------------------------------------------ refresh
    def refresh(self) -> None:
        settings = self.manager.settings
        tasks = [t for t in self.manager.snapshot() if self.filter.accepts(t, settings)]
        if [t.id for t in tasks] != [t.id for t in self._tasks]:
            self.beginResetModel()
            self._tasks = tasks
            self.endResetModel()
            return
        self._tasks = tasks
        if tasks:
            self.dataChanged.emit(self.index(0, COL_NAME), self.index(len(tasks) - 1, COL_DESC), [])

    def set_filter(self, flt: TaskFilter) -> None:
        self.filter = flt
        self.beginResetModel()
        settings = self.manager.settings
        self._tasks = [t for t in self.manager.snapshot() if flt.accepts(t, settings)]
        self.endResetModel()

    def task_at(self, row: int) -> DownloadTask | None:
        return self._tasks[row] if 0 <= row < len(self._tasks) else None

    def row_of(self, task_id: str) -> int:
        for i, t in enumerate(self._tasks):
            if t.id == task_id:
                return i
        return -1

    # ------------------------------------------------------- Qt interface
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._tasks)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if section in (COL_SIZE, COL_SPEED, COL_ETA):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        task = self.task_at(index.row())
        if task is None:
            return None
        col = index.column()
        if role == TaskRole:
            return task
        if role == SortRole:
            if col == COL_NAME:
                return task.filename.lower()
            if col == COL_SIZE:
                return task.size or 0
            if col in (COL_STATUS, COL_PROGRESS):
                return STATUS_ORDER[task.status] * 1000 - int((task.progress or 0) * 999)
            if col == COL_DESC:
                return description_text(task).lower()
            if col == COL_SPEED:
                return task.speed
            if col == COL_ETA:
                return task.eta if task.eta is not None else 1e12
            if col == COL_ADDED:
                return task.created_at
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_NAME:
                return task.filename
            if col == COL_SIZE:
                return fmt_bytes(task.size) if task.size else "—"
            if col == COL_STATUS:
                return status_text(task)
            if col == COL_PROGRESS:
                return STATUS_LABEL[task.status]  # painted by ProgressDelegate; text for accessibility
            if col == COL_DESC:
                return description_text(task)
            if col == COL_SPEED:
                return fmt_speed(task.speed) if task.status == Status.DOWNLOADING else ""
            if col == COL_ETA:
                return fmt_eta(task.eta)
            if col == COL_ADDED:
                return fmt_time(task.created_at)
        if role == Qt.ItemDataRole.DecorationRole and col == COL_NAME:
            color = self.theme.danger if task.status == Status.ERROR else self.theme.muted
            return icon(icon_name_for(task, self.manager.settings), color, 20)
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [task.filename, task.origin or task.url, f"Saved to: {task.dest_dir}"]
            if task.error:
                lines.append(f"Error: {task.error}")
            return "\n".join(lines)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL_SIZE, COL_SPEED, COL_ETA):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole:
            if task.status == Status.ERROR:
                return self.theme.danger
            if col == COL_STATUS and task.status != Status.QUEUED:
                return self.theme.status_color(task.status)
            if col in (COL_SIZE, COL_SPEED, COL_ETA, COL_ADDED, COL_DESC):
                return self.theme.muted
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(0, ROW_HEIGHT)
        return None


class TaskProxyModel(QSortFilterProxyModel):
    """Search-as-you-type over name and URL, plus column sorting via SortRole."""

    def __init__(self, source: TaskTableModel, parent=None) -> None:
        super().__init__(parent)
        self.setSourceModel(source)
        self.setSortRole(SortRole)
        self.setDynamicSortFilter(True)
        self._needle = ""

    def set_search(self, text: str) -> None:
        self._needle = text.strip().lower()
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:  # noqa: N802
        if not self._needle:
            return True
        source = self.sourceModel()
        assert isinstance(source, TaskTableModel)
        task = source.task_at(source_row)
        if task is None:
            return False
        hay = f"{task.filename} {task.url} {task.title}".lower()
        return all(word in hay for word in self._needle.split())

    def task_at(self, proxy_row: int) -> DownloadTask | None:
        idx = self.index(proxy_row, 0)
        return idx.data(TaskRole) if idx.isValid() else None

    def row_of(self, task_id: str) -> int:
        for r in range(self.rowCount()):
            t = self.task_at(r)
            if t is not None and t.id == task_id:
                return r
        return -1


class ProgressDelegate(QStyledItemDelegate):
    """Progress column: status text on top, segment map bar underneath."""

    def __init__(self, theme: Theme, parent=None, manager: DownloadManager | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.manager = manager

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(200, ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        task: DownloadTask | None = index.data(TaskRole)
        if task is None:
            super().paint(painter, option, index)
            return
        t = self.theme
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, t.accent_soft)
        elif index.row() % 2 == 1:
            painter.fillRect(option.rect, t.alt)

        rect = QRectF(option.rect).adjusted(8, 6, -8, -6)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        font = QFont(option.font)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_h = fm.height()
        color = t.status_color(task.status) if task.status != Status.QUEUED else t.muted
        left = STATUS_LABEL[task.status]
        if task.status == Status.ERROR:
            left = f"Error · {task.error or 'unknown'}"
        elif task.progress is not None and task.status != Status.COMPLETED:
            left = f"{task.progress * 100:.1f}%  ·  {left}"
        elif task.status == Status.DOWNLOADING:
            left = f"{fmt_bytes(task.downloaded)}  ·  {left}"
        if task.note and task.status in (Status.DOWNLOADING, Status.COMPLETED):
            left = f"{left} · {task.note}"
        if task.status == Status.QUEUED and self.manager is not None:
            sched = self.manager.schedule_note()
            if sched.startswith("starts"):
                left = f"{left} · scheduler {sched}"
        painter.setPen(color)
        painter.drawText(
            QRectF(rect.x(), rect.y(), rect.width(), text_h),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            fm.elidedText(left, Qt.TextElideMode.ElideRight, int(rect.width())),
        )

        bar = QRectF(rect.x(), rect.y() + text_h + 4, rect.width(), max(6.0, rect.height() - text_h - 4))
        paint_segment_map(painter, bar, task, t, radius=3.0)
        painter.restore()
