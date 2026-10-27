"""Vector icons drawn with QPainter so they match in both light and dark themes."""

from __future__ import annotations

import math
from collections.abc import Callable
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

Drawer = Callable[[QPainter, QRectF], None]


def _line(p: QPainter, r: QRectF, x1: float, y1: float, x2: float, y2: float) -> None:
    p.drawLine(
        QPointF(r.x() + r.width() * x1, r.y() + r.height() * y1),
        QPointF(r.x() + r.width() * x2, r.y() + r.height() * y2),
    )


def _pt(r: QRectF, x: float, y: float) -> QPointF:
    return QPointF(r.x() + r.width() * x, r.y() + r.height() * y)


def _sub(r: QRectF, x: float, y: float, w: float, h: float) -> QRectF:
    return QRectF(r.x() + r.width() * x, r.y() + r.height() * y, r.width() * w, r.height() * h)


def _fill(p: QPainter) -> None:
    p.setBrush(p.pen().color())


# ---------------------------------------------------------------- shapes
def _plus(p: QPainter, r: QRectF) -> None:
    _line(p, r, 0.5, 0.1, 0.5, 0.9)
    _line(p, r, 0.1, 0.5, 0.9, 0.5)


def _download(p: QPainter, r: QRectF) -> None:
    _line(p, r, 0.5, 0.05, 0.5, 0.62)
    _line(p, r, 0.25, 0.4, 0.5, 0.65)
    _line(p, r, 0.75, 0.4, 0.5, 0.65)
    _line(p, r, 0.12, 0.9, 0.88, 0.9)


def _play(p: QPainter, r: QRectF) -> None:
    _fill(p)
    p.drawPolygon(QPolygonF([_pt(r, 0.22, 0.1), _pt(r, 0.88, 0.5), _pt(r, 0.22, 0.9)]))


def _pause(p: QPainter, r: QRectF) -> None:
    _fill(p)
    p.drawRoundedRect(_sub(r, 0.2, 0.1, 0.2, 0.8), 1, 1)
    p.drawRoundedRect(_sub(r, 0.6, 0.1, 0.2, 0.8), 1, 1)


def _play_all(p: QPainter, r: QRectF) -> None:
    _fill(p)
    p.drawPolygon(QPolygonF([_pt(r, 0.08, 0.15), _pt(r, 0.5, 0.5), _pt(r, 0.08, 0.85)]))
    p.drawPolygon(QPolygonF([_pt(r, 0.52, 0.15), _pt(r, 0.94, 0.5), _pt(r, 0.52, 0.85)]))


def _pause_all(p: QPainter, r: QRectF) -> None:
    p.drawEllipse(_sub(r, 0.05, 0.05, 0.9, 0.9))
    _fill(p)
    p.drawRect(_sub(r, 0.33, 0.3, 0.12, 0.4))
    p.drawRect(_sub(r, 0.55, 0.3, 0.12, 0.4))


def _trash(p: QPainter, r: QRectF) -> None:
    _line(p, r, 0.12, 0.25, 0.88, 0.25)
    _line(p, r, 0.38, 0.12, 0.62, 0.12)
    _line(p, r, 0.38, 0.12, 0.38, 0.25)
    _line(p, r, 0.62, 0.12, 0.62, 0.25)
    path = QPainterPath()
    path.moveTo(_pt(r, 0.2, 0.25))
    path.lineTo(_pt(r, 0.25, 0.9))
    path.lineTo(_pt(r, 0.75, 0.9))
    path.lineTo(_pt(r, 0.8, 0.25))
    p.drawPath(path)
    _line(p, r, 0.42, 0.4, 0.43, 0.78)
    _line(p, r, 0.58, 0.4, 0.57, 0.78)


def _folder(p: QPainter, r: QRectF) -> None:
    path = QPainterPath()
    path.moveTo(_pt(r, 0.08, 0.3))
    path.lineTo(_pt(r, 0.08, 0.18))
    path.lineTo(_pt(r, 0.38, 0.18))
    path.lineTo(_pt(r, 0.48, 0.3))
    path.lineTo(_pt(r, 0.92, 0.3))
    path.lineTo(_pt(r, 0.92, 0.85))
    path.lineTo(_pt(r, 0.08, 0.85))
    path.closeSubpath()
    p.drawPath(path)


def _gear(p: QPainter, r: QRectF) -> None:
    c = _pt(r, 0.5, 0.5)
    rad = r.width() * 0.26
    p.drawEllipse(c, rad * 0.45, rad * 0.45)
    pen = p.pen()
    pen.setWidthF(pen.widthF() * 1.5)
    p.setPen(pen)
    for i in range(8):
        a = i * math.pi / 4
        p.drawLine(
            QPointF(c.x() + math.cos(a) * rad * 0.9, c.y() + math.sin(a) * rad * 0.9),
            QPointF(c.x() + math.cos(a) * rad * 1.55, c.y() + math.sin(a) * rad * 1.55),
        )
    p.drawEllipse(c, rad, rad)


def _details(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(_sub(r, 0.1, 0.15, 0.8, 0.7), 2, 2)
    _line(p, r, 0.1, 0.58, 0.9, 0.58)


def _list(p: QPainter, r: QRectF) -> None:
    for y in (0.25, 0.5, 0.75):
        _line(p, r, 0.15, y, 0.85, y)


def _clock(p: QPainter, r: QRectF) -> None:
    p.drawEllipse(_sub(r, 0.1, 0.1, 0.8, 0.8))
    _line(p, r, 0.5, 0.5, 0.5, 0.27)
    _line(p, r, 0.5, 0.5, 0.66, 0.6)


def _check(p: QPainter, r: QRectF) -> None:
    path = QPainterPath()
    path.moveTo(_pt(r, 0.15, 0.52))
    path.lineTo(_pt(r, 0.4, 0.78))
    path.lineTo(_pt(r, 0.86, 0.26))
    p.drawPath(path)


def _archive(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(_sub(r, 0.12, 0.12, 0.76, 0.76), 2, 2)
    pen = p.pen()
    pen.setStyle(Qt.PenStyle.DotLine)
    p.setPen(pen)
    _line(p, r, 0.5, 0.15, 0.5, 0.6)
    pen.setStyle(Qt.PenStyle.SolidLine)
    p.setPen(pen)
    p.drawRect(_sub(r, 0.42, 0.6, 0.16, 0.2))


def _document(p: QPainter, r: QRectF) -> None:
    path = QPainterPath()
    path.moveTo(_pt(r, 0.2, 0.08))
    path.lineTo(_pt(r, 0.62, 0.08))
    path.lineTo(_pt(r, 0.8, 0.26))
    path.lineTo(_pt(r, 0.8, 0.92))
    path.lineTo(_pt(r, 0.2, 0.92))
    path.closeSubpath()
    p.drawPath(path)
    _line(p, r, 0.62, 0.08, 0.62, 0.26)
    _line(p, r, 0.62, 0.26, 0.8, 0.26)
    _line(p, r, 0.33, 0.5, 0.67, 0.5)
    _line(p, r, 0.33, 0.66, 0.67, 0.66)


def _music(p: QPainter, r: QRectF) -> None:
    _line(p, r, 0.62, 0.12, 0.62, 0.7)
    _line(p, r, 0.62, 0.12, 0.85, 0.2)
    _fill(p)
    p.drawEllipse(_sub(r, 0.33, 0.6, 0.3, 0.26))


def _program(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(_sub(r, 0.1, 0.15, 0.8, 0.7), 2, 2)
    _line(p, r, 0.1, 0.35, 0.9, 0.35)
    _fill(p)
    p.drawEllipse(_sub(r, 0.17, 0.21, 0.08, 0.08))
    p.drawEllipse(_sub(r, 0.3, 0.21, 0.08, 0.08))


def _video(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(_sub(r, 0.08, 0.2, 0.6, 0.6), 2, 2)
    _fill(p)
    p.drawPolygon(QPolygonF([_pt(r, 0.7, 0.4), _pt(r, 0.92, 0.26), _pt(r, 0.92, 0.74), _pt(r, 0.7, 0.6)]))


def _image(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(_sub(r, 0.1, 0.15, 0.8, 0.7), 2, 2)
    path = QPainterPath()
    path.moveTo(_pt(r, 0.15, 0.8))
    path.lineTo(_pt(r, 0.4, 0.5))
    path.lineTo(_pt(r, 0.58, 0.68))
    path.lineTo(_pt(r, 0.7, 0.58))
    path.lineTo(_pt(r, 0.85, 0.8))
    p.drawPath(path)
    _fill(p)
    p.drawEllipse(_sub(r, 0.62, 0.27, 0.14, 0.14))


def _file(p: QPainter, r: QRectF) -> None:
    path = QPainterPath()
    path.moveTo(_pt(r, 0.22, 0.08))
    path.lineTo(_pt(r, 0.62, 0.08))
    path.lineTo(_pt(r, 0.78, 0.24))
    path.lineTo(_pt(r, 0.78, 0.92))
    path.lineTo(_pt(r, 0.22, 0.92))
    path.closeSubpath()
    p.drawPath(path)


def _paste(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(_sub(r, 0.18, 0.15, 0.64, 0.75), 2, 2)
    _fill(p)
    p.drawRoundedRect(_sub(r, 0.36, 0.08, 0.28, 0.14), 1, 1)


def _warning(p: QPainter, r: QRectF) -> None:
    p.drawPolygon(QPolygonF([_pt(r, 0.5, 0.1), _pt(r, 0.92, 0.88), _pt(r, 0.08, 0.88)]))
    _line(p, r, 0.5, 0.38, 0.5, 0.62)
    _fill(p)
    p.drawEllipse(_sub(r, 0.46, 0.7, 0.08, 0.08))


SHAPES: dict[str, Drawer] = {
    "plus": _plus,
    "download": _download,
    "play": _play,
    "pause": _pause,
    "play-all": _play_all,
    "pause-all": _pause_all,
    "trash": _trash,
    "folder": _folder,
    "gear": _gear,
    "details": _details,
    "list": _list,
    "clock": _clock,
    "check": _check,
    "archive": _archive,
    "document": _document,
    "music": _music,
    "program": _program,
    "video": _video,
    "image": _image,
    "file": _file,
    "paste": _paste,
    "warning": _warning,
}

CATEGORY_ICONS = {
    "Compressed": "archive",
    "Documents": "document",
    "Music": "music",
    "Programs": "program",
    "Video": "video",
    "Images": "image",
}


def pixmap(name: str, color: QColor, size: int = 24, stroke: float = 0.085) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, size * stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    SHAPES.get(name, _file)(p, QRectF(0, 0, size, size))
    p.end()
    return pm


@lru_cache(maxsize=256)
def _icon_cached(name: str, rgba: int, size: int) -> QIcon:
    color = QColor.fromRgba(rgba)
    icon = QIcon()
    for s in (size, size * 2):
        icon.addPixmap(pixmap(name, color, s), QIcon.Mode.Normal)
        faded = QColor(color)
        faded.setAlpha(90)
        icon.addPixmap(pixmap(name, faded, s), QIcon.Mode.Disabled)
    return icon


def icon(name: str, color: QColor, size: int = 24) -> QIcon:
    return _icon_cached(name, color.rgba(), size)
