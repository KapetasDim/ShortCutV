"""Small outline icon set drawn directly with QPainter (no bundled image
assets, no third-party icon package) so the app has zero extra runtime deps
and every icon can be recolored to match the active theme on demand.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap

_CACHE: dict[tuple[str, str, int], QIcon] = {}

Draw = None  # set below (Callable[[QPainter, QRectF], None])


def _pen(painter: QPainter, color: str, width: float = 1.6):
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)


def _draw_play(p: QPainter, r: QRectF, color: str):
    _pen(p, color)
    p.setBrush(color)
    path = QPainterPath()
    m = r.width() * 0.22
    path.moveTo(r.left() + m, r.top() + m * 0.6)
    path.lineTo(r.left() + m, r.bottom() - m * 0.6)
    path.lineTo(r.right() - m * 0.7, r.center().y())
    path.closeSubpath()
    p.drawPath(path)


def _draw_pause(p: QPainter, r: QRectF, color: str):
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    w = r.width() * 0.22
    p.drawRoundedRect(QRectF(r.left() + r.width() * 0.22, r.top() + r.height() * 0.18, w, r.height() * 0.64), 1.5, 1.5)
    p.drawRoundedRect(QRectF(r.right() - r.width() * 0.22 - w, r.top() + r.height() * 0.18, w, r.height() * 0.64), 1.5, 1.5)


def _draw_folder(p: QPainter, r: QRectF, color: str):
    _pen(p, color)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    x0, y0 = r.left() + 2, r.top() + r.height() * 0.32
    path.moveTo(x0, y0)
    path.lineTo(x0, r.bottom() - 2)
    path.lineTo(r.right() - 2, r.bottom() - 2)
    path.lineTo(r.right() - 2, y0)
    path.lineTo(r.left() + r.width() * 0.45, y0)
    path.lineTo(r.left() + r.width() * 0.35, r.top() + 2)
    path.lineTo(x0 + 2, r.top() + 2)
    path.closeSubpath()
    p.drawPath(path)


def _draw_plus(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 2.0)
    cx, cy = r.center().x(), r.center().y()
    m = r.width() * 0.24
    p.drawLine(QPointF(cx, r.top() + m), QPointF(cx, r.bottom() - m))
    p.drawLine(QPointF(r.left() + m, cy), QPointF(r.right() - m, cy))


def _draw_close(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.8)
    m = r.width() * 0.26
    p.drawLine(QPointF(r.left() + m, r.top() + m), QPointF(r.right() - m, r.bottom() - m))
    p.drawLine(QPointF(r.right() - m, r.top() + m), QPointF(r.left() + m, r.bottom() - m))


def _draw_edit(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.6)
    path = QPainterPath()
    path.moveTo(r.left() + r.width() * 0.18, r.bottom() - r.height() * 0.18)
    path.lineTo(r.left() + r.width() * 0.62, r.top() + r.height() * 0.2)
    path.lineTo(r.right() - r.width() * 0.14, r.top() + r.height() * 0.36)
    path.lineTo(r.left() + r.width() * 0.34, r.bottom() - r.height() * 0.02)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(r.left() + r.width() * 0.18, r.bottom() - r.height() * 0.18),
               QPointF(r.left() + r.width() * 0.1, r.bottom() - 1))


def _draw_trash(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.5)
    top = r.top() + r.height() * 0.22
    p.drawLine(QPointF(r.left() + 2, top), QPointF(r.right() - 2, top))
    p.drawLine(QPointF(r.left() + r.width() * 0.36, top), QPointF(r.left() + r.width() * 0.4, r.top() + 1))
    p.drawLine(QPointF(r.right() - r.width() * 0.36, top), QPointF(r.right() - r.width() * 0.4, r.top() + 1))
    p.drawLine(QPointF(r.left() + r.width() * 0.4, r.top() + 1), QPointF(r.right() - r.width() * 0.4, r.top() + 1))
    path = QPainterPath()
    path.moveTo(r.left() + r.width() * 0.22, top)
    path.lineTo(r.left() + r.width() * 0.28, r.bottom() - 2)
    path.lineTo(r.right() - r.width() * 0.28, r.bottom() - 2)
    path.lineTo(r.right() - r.width() * 0.22, top)
    p.drawPath(path)


def _draw_save(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.5)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 2, 2)
    p.drawRect(QRectF(r.left() + r.width() * 0.28, r.top() + 2, r.width() * 0.44, r.height() * 0.32))
    p.drawRect(QRectF(r.left() + r.width() * 0.24, r.bottom() - r.height() * 0.42,
                       r.width() * 0.52, r.height() * 0.36))


def _draw_load(p: QPainter, r: QRectF, color: str):
    _draw_folder(p, r, color)


def _draw_refresh(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.6)
    p.setBrush(Qt.NoBrush)
    rr = r.adjusted(3, 3, -3, -3)
    p.drawArc(rr, 40 * 16, 260 * 16)
    tip = QPainterPath()
    ang_x, ang_y = rr.right(), rr.center().y() - rr.height() * 0.28
    tip.moveTo(ang_x, ang_y)
    tip.lineTo(ang_x - 5, ang_y - 4)
    tip.moveTo(ang_x, ang_y)
    tip.lineTo(ang_x + 1, ang_y + 5)
    p.drawPath(tip)


def _draw_export(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.6)
    cx = r.center().x()
    p.drawLine(QPointF(cx, r.top() + 1), QPointF(cx, r.bottom() - r.height() * 0.32))
    p.drawLine(QPointF(cx, r.top() + 1), QPointF(cx - r.width() * 0.16, r.top() + r.height() * 0.22))
    p.drawLine(QPointF(cx, r.top() + 1), QPointF(cx + r.width() * 0.16, r.top() + r.height() * 0.22))
    p.drawLine(QPointF(r.left() + 2, r.bottom() - 2), QPointF(r.right() - 2, r.bottom() - 2))


def _draw_scissors(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.5)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(r.left() + 1, r.top() + r.height() * 0.55, r.width() * 0.3, r.width() * 0.3))
    p.drawEllipse(QRectF(r.left() + 1, r.top() + r.height() * 0.15, r.width() * 0.3, r.width() * 0.3))
    p.drawLine(QPointF(r.left() + r.width() * 0.3, r.top() + r.height() * 0.3),
               QPointF(r.right() - 1, r.bottom() - 1))
    p.drawLine(QPointF(r.left() + r.width() * 0.3, r.bottom() - r.height() * 0.3),
               QPointF(r.right() - 1, r.top() + 1))


def _draw_text(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.7)
    cx = r.center().x()
    p.drawLine(QPointF(r.left() + 1, r.top() + 2), QPointF(r.right() - 1, r.top() + 2))
    p.drawLine(QPointF(cx, r.top() + 2), QPointF(cx, r.bottom() - 1))


def _draw_sound(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.5)
    p.setBrush(color)
    p.drawEllipse(QRectF(r.left() + 1, r.bottom() - r.height() * 0.32, r.width() * 0.26, r.height() * 0.26))
    p.drawEllipse(QRectF(r.left() + r.width() * 0.5, r.bottom() - r.height() * 0.42, r.width() * 0.26, r.height() * 0.26))
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(r.left() + r.width() * 0.24, r.bottom() - r.height() * 0.2),
               QPointF(r.left() + r.width() * 0.24, r.top() + 1))
    p.drawLine(QPointF(r.left() + r.width() * 0.74, r.bottom() - r.height() * 0.3),
               QPointF(r.left() + r.width() * 0.74, r.top() + r.height() * 0.14))
    p.drawLine(QPointF(r.left() + r.width() * 0.24, r.top() + 1),
               QPointF(r.left() + r.width() * 0.74, r.top() + r.height() * 0.14))


def _draw_crop(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.6)
    m = r.width() * 0.22
    p.drawLine(QPointF(r.left() + m, r.top() + 1), QPointF(r.left() + m, r.bottom() - m))
    p.drawLine(QPointF(r.left() + m, r.bottom() - m), QPointF(r.right() - 1, r.bottom() - m))
    p.drawLine(QPointF(r.right() - m, r.top() + m), QPointF(r.right() - m, r.bottom() - 1))
    p.drawLine(QPointF(r.left() + 1, r.top() + m), QPointF(r.right() - m, r.top() + m))


def _draw_image(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.5)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 2, 2)
    p.drawEllipse(QRectF(r.left() + r.width() * 0.22, r.top() + r.height() * 0.24, r.width() * 0.16, r.width() * 0.16))
    path = QPainterPath()
    path.moveTo(r.left() + 2, r.bottom() - r.height() * 0.28)
    path.lineTo(r.left() + r.width() * 0.4, r.top() + r.height() * 0.45)
    path.lineTo(r.left() + r.width() * 0.62, r.bottom() - r.height() * 0.35)
    path.lineTo(r.left() + r.width() * 0.78, r.top() + r.height() * 0.55)
    path.lineTo(r.right() - 2, r.bottom() - r.height() * 0.15)
    p.drawPath(path)


def _draw_duplicate(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.5)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(r.left() + 1, r.top() + 1, r.width() * 0.66, r.height() * 0.66), 2, 2)
    p.drawRoundedRect(QRectF(r.right() - r.width() * 0.66 - 1, r.bottom() - r.height() * 0.66 - 1,
                              r.width() * 0.66, r.height() * 0.66), 2, 2)


def _draw_chevron_down(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 1.6)
    cx, cy = r.center().x(), r.center().y()
    m = r.width() * 0.22
    p.drawLine(QPointF(cx - m, cy - m * 0.4), QPointF(cx, cy + m * 0.5))
    p.drawLine(QPointF(cx, cy + m * 0.5), QPointF(cx + m, cy - m * 0.4))


def _draw_check(p: QPainter, r: QRectF, color: str):
    _pen(p, color, 2.0)
    p.drawLine(QPointF(r.left() + r.width() * 0.2, r.center().y()),
               QPointF(r.left() + r.width() * 0.42, r.bottom() - r.height() * 0.22))
    p.drawLine(QPointF(r.left() + r.width() * 0.42, r.bottom() - r.height() * 0.22),
               QPointF(r.right() - r.width() * 0.18, r.top() + r.height() * 0.22))


_DRAWERS = {
    "play": _draw_play,
    "pause": _draw_pause,
    "folder": _draw_folder,
    "plus": _draw_plus,
    "close": _draw_close,
    "edit": _draw_edit,
    "trash": _draw_trash,
    "save": _draw_save,
    "load": _draw_load,
    "refresh": _draw_refresh,
    "export": _draw_export,
    "scissors": _draw_scissors,
    "text": _draw_text,
    "sound": _draw_sound,
    "crop": _draw_crop,
    "image": _draw_image,
    "duplicate": _draw_duplicate,
    "chevron_down": _draw_chevron_down,
    "check": _draw_check,
}


def icon(name: str, color: str = "#eceeef", size: int = 16) -> QIcon:
    key = (name, color, size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    drawer = _DRAWERS.get(name)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    if drawer is not None:
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        drawer(painter, QRectF(0, 0, size, size), color)
        painter.end()
    result = QIcon(pixmap)
    _CACHE[key] = result
    return result
