"""Reusable custom-painted Qt widgets: RangeSlider (two-handle in/out
trimmer), Scrubber (single-handle seek bar), InlineColorPicker (embedded HSV
picker), and ScrollableFrame (a thin QScrollArea wrapper exposing the same
`.body` / `.clear()` shape the rest of the app's tab code is written against).
"""
from __future__ import annotations

import colorsys
import io

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from .scale import get_scale
from .theme import COLORS


def fmt_time(ms: float) -> str:
    ms = max(0, int(ms))
    s, millis = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{millis:03d}"


def bytes_to_image(png_bytes: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(png_bytes))


def pil_to_qpixmap(img) -> QPixmap:
    img = img.convert("RGBA")
    qimg = QImage(img.tobytes("raw", "RGBA"), img.width, img.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class RangeSlider(QWidget):
    """Two-handle range trimmer (in/out points) with a scrub playhead.
    Positions reported as 0.0-1.0 fractions."""

    changed = Signal(float, float)     # start, end
    seeked = Signal(float)             # frac, while scrubbing the track

    def __init__(self, parent=None):
        super().__init__(parent)
        scale = get_scale()
        self.TRACK_Y = scale.range_slider_track_y
        self.TRACK_H = scale.range_slider_track_h
        self.HANDLE_R = scale.range_slider_handle_r
        self._hit_tolerance = scale.range_slider_hit_tolerance
        self.setFixedHeight(scale.range_slider_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

        self.start = 0.0
        self.end = 1.0
        self.playhead = 0.0
        self._drag = None  # "start" | "end" | "seek"

    def apply_scale(self):
        """Re-reads the active UI-scale profile's tokens -- call after a
        1080p/4K switch so this widget's pixel geometry updates in place."""
        scale = get_scale()
        self.TRACK_Y = scale.range_slider_track_y
        self.TRACK_H = scale.range_slider_track_h
        self.HANDLE_R = scale.range_slider_handle_r
        self._hit_tolerance = scale.range_slider_hit_tolerance
        self.setFixedHeight(scale.range_slider_height)
        self.update()

    def add_callback(self, fn):
        self.changed.connect(lambda s, e: fn(s, e))

    def add_seek_callback(self, fn):
        self.seeked.connect(fn)

    def set_playhead(self, frac: float):
        self.playhead = max(0.0, min(1.0, frac))
        self.update()

    def _to_x(self, frac: float) -> float:
        m = self.HANDLE_R
        return m + frac * (self.width() - 2 * m)

    def _to_frac(self, x: float) -> float:
        m = self.HANDLE_R
        return max(0.0, min(1.0, (x - m) / max(1, self.width() - 2 * m)))

    def mousePressEvent(self, event: QMouseEvent):
        x = event.position().x()
        sx, ex = self._to_x(self.start), self._to_x(self.end)
        ds, de = abs(x - sx), abs(x - ex)
        if ds < self._hit_tolerance or de < self._hit_tolerance:
            self._drag = "start" if ds <= de else "end"
        else:
            self._drag = "seek"
            f = self._to_frac(x)
            self.playhead = f
            self.update()
            self.seeked.emit(f)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag is None:
            return
        f = self._to_frac(event.position().x())
        if self._drag == "start":
            self.start = max(0.0, min(f, self.end - 0.001))
            self.update()
            self.changed.emit(self.start, self.end)
        elif self._drag == "end":
            self.end = max(self.start + 0.001, min(f, 1.0))
            self.update()
            self.changed.emit(self.start, self.end)
        elif self._drag == "seek":
            self.playhead = f
            self.update()
            self.seeked.emit(f)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag = None

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        ty, th, r = self.TRACK_Y, self.TRACK_H, self.HANDLE_R
        cy = ty + th / 2

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLORS["surface_alt"]))
        p.drawRoundedRect(QRectF(r, ty, w - 2 * r, th), 3, 3)

        sx, ex = self._to_x(self.start), self._to_x(self.end)
        p.setBrush(QColor(COLORS["accent"]))
        p.drawRoundedRect(QRectF(sx, ty, ex - sx, th), 3, 3)

        px = self._to_x(self.playhead)
        p.setPen(QPen(QColor("white"), 2))
        p.drawLine(QPointF(px, 2), QPointF(px, self.height() - 2))

        p.setPen(QPen(QColor("white"), 1))
        p.setBrush(QColor(COLORS["success"]))
        p.drawEllipse(QPointF(sx, cy), r, r)
        p.setBrush(QColor(COLORS["danger"]))
        p.drawEllipse(QPointF(ex, cy), r, r)
        p.end()


class Scrubber(QWidget):
    """Single-handle seek bar: click/drag anywhere to seek. Used for library
    preview and timeline proxy playback, where there's no in/out trimming."""

    seeked = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        scale = get_scale()
        self.TRACK_Y = scale.scrubber_track_y
        self.TRACK_H = scale.scrubber_track_h
        self.HANDLE_R = scale.scrubber_handle_r
        self.setFixedHeight(scale.scrubber_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.playhead = 0.0
        self._dragging = False
        self._locked = False

    def apply_scale(self):
        scale = get_scale()
        self.TRACK_Y = scale.scrubber_track_y
        self.TRACK_H = scale.scrubber_track_h
        self.HANDLE_R = scale.scrubber_handle_r
        self.setFixedHeight(scale.scrubber_height)
        self.update()

    def add_seek_callback(self, fn):
        self.seeked.connect(fn)

    def set_locked(self, locked: bool):
        self._locked = locked
        if locked:
            self._dragging = False
            self.playhead = 0.0
        self.update()

    def set_playhead(self, frac: float):
        if self._dragging or self._locked:
            return
        self.playhead = max(0.0, min(1.0, frac))
        self.update()

    def _to_x(self, frac: float) -> float:
        m = self.HANDLE_R
        return m + frac * (self.width() - 2 * m)

    def _to_frac(self, x: float) -> float:
        m = self.HANDLE_R
        return max(0.0, min(1.0, (x - m) / max(1, self.width() - 2 * m)))

    def mousePressEvent(self, event: QMouseEvent):
        if self._locked:
            return
        self._dragging = True
        f = self._to_frac(event.position().x())
        self.playhead = f
        self.update()
        self.seeked.emit(f)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._locked or not self._dragging:
            return
        f = self._to_frac(event.position().x())
        self.playhead = f
        self.update()
        self.seeked.emit(f)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._dragging = False

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        ty, th, r = self.TRACK_Y, self.TRACK_H, self.HANDLE_R
        cy = ty + th / 2

        fill = COLORS["text_dim"] if self._locked else COLORS["accent"]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLORS["surface"] if self._locked else COLORS["surface_alt"]))
        p.drawRoundedRect(QRectF(r, ty, w - 2 * r, th), 3, 3)

        px = self._to_x(self.playhead)
        if not self._locked:
            p.setBrush(QColor(fill))
            p.drawRoundedRect(QRectF(r, ty, px - r, th), 3, 3)

        p.setPen(QPen(QColor(COLORS["border_strong"] if self._locked else "white"), 1))
        p.setBrush(QColor(fill))
        p.drawEllipse(QPointF(px, cy), r, r)
        p.end()


class InlineColorPicker(QWidget):
    """Full HSV picker (saturation/value gradient square + hue strip) embedded
    directly in a panel, instead of the OS's native color-chooser popup."""

    changed = Signal(str)

    def __init__(self, parent, on_change):
        super().__init__(parent)
        scale = get_scale()
        self.SQ = scale.color_picker_sq
        self.HUE_H = scale.color_picker_hue_h
        self._marker_r = scale.color_picker_marker_r
        self._h, self._s, self._v = 0.0, 0.0, 1.0
        self._suspend = False
        self.changed.connect(on_change)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        label = QLabel("Color picker:")
        label.setProperty("muted", True)
        layout.addWidget(label)

        body = QWidget(self)
        from PySide6.QtWidgets import QHBoxLayout
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)
        layout.addWidget(body)

        self._sv_label = _ClickableImage(self.SQ, self.SQ, self._on_sv_click)
        body_layout.addWidget(self._sv_label)

        right = QVBoxLayout()
        self._swatch = QFrame()
        self._swatch.setFixedSize(scale.color_picker_swatch, scale.color_picker_swatch)
        self._swatch.setStyleSheet(f"background-color: #ffffff; border: 1px solid {COLORS['border_strong']};")
        right.addWidget(self._swatch)
        right.addStretch(1)
        body_layout.addLayout(right)

        self._hue_label = _ClickableImage(self.SQ, self.HUE_H, self._on_hue_click)
        layout.addWidget(self._hue_label)

        hex_row = QWidget()
        from PySide6.QtWidgets import QHBoxLayout as _HB
        hex_layout = _HB(hex_row)
        hex_layout.setContentsMargins(0, 0, 0, 0)
        hex_layout.addWidget(QLabel("Hex:"))
        self._hex_edit = QLineEdit("#ffffff")
        self._hex_edit.setFixedWidth(80)
        self._hex_edit.editingFinished.connect(self._on_hex_entered)
        hex_layout.addWidget(self._hex_edit)
        hex_layout.addStretch(1)
        layout.addWidget(hex_row)

        self._draw_hue_strip()
        self._redraw_sv_square()

    def _draw_hue_strip(self):
        img = QImage(self.SQ, self.HUE_H, QImage.Format.Format_RGB32)
        for x in range(self.SQ):
            r, g, b = colorsys.hsv_to_rgb(x / self.SQ, 1.0, 1.0)
            color = QColor(int(r * 255), int(g * 255), int(b * 255)).rgb()
            for y in range(self.HUE_H):
                img.setPixel(x, y, color)
        pm = QPixmap.fromImage(img)
        self._hue_label.set_base_pixmap(pm)
        self._draw_hue_marker()

    def _redraw_sv_square(self):
        img = QImage(self.SQ, self.SQ, QImage.Format.Format_RGB32)
        h = self._h
        for y in range(self.SQ):
            v = 1.0 - y / (self.SQ - 1)
            for x in range(self.SQ):
                s = x / (self.SQ - 1)
                r, g, b = colorsys.hsv_to_rgb(h, s, v)
                img.setPixel(x, y, QColor(int(r * 255), int(g * 255), int(b * 255)).rgb())
        pm = QPixmap.fromImage(img)
        self._sv_label.set_base_pixmap(pm)
        self._draw_sv_marker()

    def _draw_sv_marker(self):
        x = self._s * (self.SQ - 1)
        y = (1 - self._v) * (self.SQ - 1)
        self._sv_label.set_marker(x, y, self._marker_r)

    def _draw_hue_marker(self):
        hx = self._h * self.SQ
        self._hue_label.set_marker(hx, self.HUE_H / 2, None)

    def _on_sv_click(self, x, y):
        self._s = max(0.0, min(1.0, x / (self.SQ - 1)))
        self._v = max(0.0, min(1.0, 1 - y / (self.SQ - 1)))
        self._apply_change()

    def _on_hue_click(self, x, _y):
        self._h = max(0.0, min(0.999, x / self.SQ))
        self._redraw_sv_square()
        self._apply_change()

    def _apply_change(self):
        self._draw_sv_marker()
        self._draw_hue_marker()
        hexcolor = self._current_hex()
        self._hex_edit.setText(hexcolor)
        self._swatch.setStyleSheet(f"background-color: {hexcolor}; border: 1px solid {COLORS['border_strong']};")
        if not self._suspend:
            self.changed.emit(hexcolor)

    def _on_hex_entered(self):
        text = self._hex_edit.text()
        if len(text.lstrip("#")) == 6:
            self.set_color(text)
            self.changed.emit(self._current_hex())

    def _current_hex(self) -> str:
        r, g, b = colorsys.hsv_to_rgb(self._h, self._s, self._v)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    def set_color(self, hex_color: str):
        self._suspend = True
        try:
            h = hex_color.lstrip("#")
            if len(h) == 6:
                r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
                self._h, self._s, self._v = colorsys.rgb_to_hsv(r, g, b)
                self._redraw_sv_square()
                self._draw_hue_marker()
                self._hex_edit.setText(f"#{h}")
                self._swatch.setStyleSheet(f"background-color: #{h}; border: 1px solid {COLORS['border_strong']};")
        finally:
            self._suspend = False


class _ClickableImage(QLabel):
    """QLabel that shows a base QPixmap plus a small crosshair/marker overlay,
    and reports clicks/drags as (x, y) pixel coordinates within itself --
    the shared building block behind InlineColorPicker's two gradients."""

    def __init__(self, w: int, h: int, on_click):
        super().__init__()
        self.setFixedSize(w, h)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._base = QPixmap(w, h)
        self._on_click = on_click
        self._marker = None  # (x, y, radius or None for a vertical line)

    def set_base_pixmap(self, pm: QPixmap):
        self._base = pm
        self._repaint()

    def set_marker(self, x, y, radius):
        self._marker = (x, y, radius)
        self._repaint()

    def _repaint(self):
        pm = QPixmap(self._base)
        if self._marker is not None:
            x, y, radius = self._marker
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            if radius is None:
                p.setPen(QPen(QColor("black"), 3))
                p.drawLine(QPointF(x, 0), QPointF(x, pm.height()))
                p.setPen(QPen(QColor("white"), 1))
                p.drawLine(QPointF(x, 0), QPointF(x, pm.height()))
            else:
                p.setPen(QPen(QColor("black"), 3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(x, y), radius, radius)
                p.setPen(QPen(QColor("white"), 1))
                p.drawEllipse(QPointF(x, y), radius, radius)
            p.end()
        self.setPixmap(pm)

    def mousePressEvent(self, event: QMouseEvent):
        self._on_click(event.position().x(), event.position().y())

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._on_click(event.position().x(), event.position().y())


class ScrollableFrame(QScrollArea):
    """Vertically scrollable container. Add child widgets to `.body_layout`,
    or reach into `.body` directly -- kept close to the old Tk ScrollableFrame's
    `.body` / `.clear()` shape so tab code ports over with minimal changes."""

    def __init__(self, parent=None, height=None, width=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if height is not None:
            self.setFixedHeight(height)
        if width is not None:
            self.setFixedWidth(width)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(2, 2, 2, 2)
        self.body_layout.setSpacing(2)
        self.body_layout.addStretch(1)
        self.setWidget(self.body)

    def clear(self):
        while self.body_layout.count() > 1:
            item = self.body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def add_widget(self, widget: QWidget):
        self.body_layout.insertWidget(self.body_layout.count() - 1, widget)
