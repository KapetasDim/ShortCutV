from __future__ import annotations

import dataclasses
import io
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                                QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import fonts_catalog, text_render
from ..crop_box import CropBoxController
from ..curves import CURVE_PRESETS, eval_curve, fade_opacity
from ..logging_config import get_logger
from ..project import TextOverlay, TimelineProject
from .crop_widgets import _cursor_for_handle, _paint_box
from .icons import icon
from .inspector_split import make_preview_splitter
from .player import SharedPlayer
from .theme import COLORS
from .job_queue import HIGH, submit
from .util import connect_button
from .widgets import InlineColorPicker, Scrubber, pil_to_qpixmap
from .worker_client import WorkerJob

log = get_logger()


class TextOverlayEditor(QWidget):
    """The default preview composites the animated text over a single grabbed
    frame -- fine for checking position/style, but not real footage moving
    behind the text. "Bake Preview" renders the real timeline video chunk the
    overlay plays over with the text baked on, playing that back through the
    mpv-embedded transport."""

    def __init__(self, parent, project: TimelineProject, preview_player: SharedPlayer,
                 on_save: Callable[[TextOverlay], None], on_close: Callable[[], None],
                 existing: Optional[TextOverlay] = None):
        super().__init__(parent)

        self.project = project
        self.preview_player = preview_player
        self.on_save = on_save
        self.on_close = on_close
        self._existing = existing
        self._base_frame: Optional[Image.Image] = None
        self._update_timer = None
        self._grab_jobs: list["_FrameGrabJob"] = []
        # Open the still preview at the MIDPOINT of the overlay's duration, not
        # at t=0: with the default 0.25s fade-in, opacity at t=0 is exactly 0,
        # so the text would be invisible in the placement box on open. Midpoint
        # is past the fade (full opacity, full scale) so the text shows for
        # placement; the animation scrubber can still be dragged back to 0.
        self._preview_t_s = (existing.duration_s if existing else 2.0) / 2.0
        self._baked = False
        self.player: Optional[SharedPlayer] = None
        self._last_bake_path: Optional[Path] = None
        self._bake_job: Optional[WorkerJob] = None
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_text_preview"
        self._tmp_dir.mkdir(exist_ok=True)
        self._param_edits: dict[str, QLineEdit] = {}
        self._curve_label_to_key: dict[str, str] = {}

        e = existing
        default_x = e.x if e else self.project.canvas_w / 2
        default_y = e.y if e else self.project.canvas_h * 0.33
        box_w = e.box_w if e else TextOverlay.__dataclass_fields__["box_w"].default
        box_h = e.box_h if e else TextOverlay.__dataclass_fields__["box_h"].default
        self._box_ctrl = CropBoxController(self.project.canvas_w, self.project.canvas_h)
        self._box_ctrl.box.left = default_x - box_w / 2
        self._box_ctrl.box.top = default_y - box_h / 2
        self._box_ctrl.box.right = default_x + box_w / 2
        self._box_ctrl.box.bottom = default_y + box_h / 2
        self._box_ctrl.clamp()
        self._suspend_xy_sync = False

        self._build_ui()
        self._grab_frame(existing.start_s if existing else None)
        self._update_preview()

    # ------------------------------------------------------------------ UI --
    # Stacked vertically (preview on top, controls scrolling below) rather
    # than the old side-by-side left-controls/right-preview split -- see
    # ImageClipEditor's _build_ui for why that split doesn't work inside the
    # narrow Inspector dock.

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Preview (top) and controls (bottom) live in one draggable splitter --
        # drag the divider to trade space between the preview and the options.
        preview_container = QWidget()
        self._right_layout = QVBoxLayout(preview_container)
        self._right_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 4, 4, 4)
        scroll.setWidget(left)

        splitter = make_preview_splitter(preview_container, scroll, "inspector/split_text_h")
        root.addWidget(splitter, 1)

        e = self._existing

        def row(parent_layout, label, width=110):
            r = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(width)
            r.addWidget(lbl)
            parent_layout.addLayout(r)
            return r

        left_layout.addWidget(QLabel("Text:"))
        self._text_edit = QPlainTextEdit()
        self._text_edit.setFixedHeight(60)
        if e:
            self._text_edit.setPlainText(e.text)
        self._text_edit.textChanged.connect(self._on_field_change)
        left_layout.addWidget(self._text_edit)

        time_row = row(left_layout, "Start (s):")
        start_default = f"{e.start_s:.2f}" if e else f"{self.preview_player.current_pos_s():.2f}"
        self._start_edit = QLineEdit(start_default)
        self._start_edit.setFixedWidth(70)
        time_row.addWidget(self._start_edit)
        use_current_btn = QPushButton("Use current preview time")
        use_current_btn.setProperty("variant", "flat")
        connect_button(use_current_btn.clicked, "Text Overlay > Use current preview time", self._use_current_time)
        time_row.addWidget(use_current_btn)
        time_row.addStretch(1)

        dur_row = row(left_layout, "Duration (s):")
        self._duration_edit = QLineEdit(str(e.duration_s) if e else "2.0")
        self._duration_edit.setFixedWidth(70)
        dur_row.addWidget(self._duration_edit)
        dur_row.addStretch(1)

        box_cx, box_cy = self._box_ctrl.box.center
        xy_row = row(left_layout, "Position X / Y:")
        self._x_edit = QLineEdit(str(round(box_cx)))
        self._x_edit.setFixedWidth(70)
        xy_row.addWidget(self._x_edit)
        self._y_edit = QLineEdit(str(round(box_cy)))
        self._y_edit.setFixedWidth(70)
        xy_row.addWidget(self._y_edit)
        xy_row.addStretch(1)

        wrap_row = QHBoxLayout()
        self._wrap_check = QCheckBox("Wrap text to box")
        self._wrap_check.setChecked(e.wrap_enabled if e else True)
        self._wrap_check.stateChanged.connect(self._on_field_change)
        wrap_row.addWidget(self._wrap_check)
        wrap_row.addStretch(1)
        left_layout.addLayout(wrap_row)

        rot_row = row(left_layout, "Rotation (deg):")
        self._rot_edit = QLineEdit(str(e.rotation_deg) if e else "0")
        self._rot_edit.setFixedWidth(70)
        rot_row.addWidget(self._rot_edit)
        rot_row.addStretch(1)

        font_row = row(left_layout, "Font:")
        self._font_combo = QComboBox()
        fonts = fonts_catalog.list_available_fonts()
        self._font_combo.addItems(fonts)
        self._font_combo.setCurrentText(e.font_family if e else "Arial")
        self._font_combo.currentTextChanged.connect(self._on_field_change)
        font_row.addWidget(self._font_combo)
        font_row.addStretch(1)

        size_row = row(left_layout, "Size:")
        self._size_edit = QLineEdit(str(e.font_size) if e else "64")
        self._size_edit.setFixedWidth(70)
        size_row.addWidget(self._size_edit)
        size_row.addStretch(1)

        mid_section = QHBoxLayout()
        mid_left = QVBoxLayout()
        mid_section.addLayout(mid_left, 1)
        self._color = e.color if e else "#ffffff"
        self._bg_color = e.bg_color if e else "#000000"
        self._outline_color = e.outline_color if e else "#000000"
        self._active_color_target = "text"
        self._color_picker = InlineColorPicker(self, self._on_picker_change)
        self._color_picker.set_color(self._color)
        mid_section.addWidget(self._color_picker)
        left_layout.addLayout(mid_section)

        color_row = row(mid_left, "Text color:")
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(40, 24)
        self._color_btn.setStyleSheet(f"background-color: {self._color};")
        connect_button(self._color_btn.clicked, "Text Overlay > Text color swatch",
                       lambda: self._activate_color_target("text"))
        color_row.addWidget(self._color_btn)
        color_row.addStretch(1)

        style_row = QHBoxLayout()
        self._bold_check = QCheckBox("Bold")
        self._italic_check = QCheckBox("Italic")
        self._underline_check = QCheckBox("Underline")
        self._bold_check.setChecked(e.bold if e else False)
        self._italic_check.setChecked(e.italic if e else False)
        self._underline_check.setChecked(e.underline if e else False)
        for cb in (self._bold_check, self._italic_check, self._underline_check):
            cb.stateChanged.connect(self._on_field_change)
            style_row.addWidget(cb)
        style_row.addStretch(1)
        mid_left.addLayout(style_row)

        bg_row = row(mid_left, "Background:")
        self._bg_color_btn = QPushButton()
        self._bg_color_btn.setFixedSize(40, 24)
        self._bg_color_btn.setStyleSheet(f"background-color: {self._bg_color};")
        connect_button(self._bg_color_btn.clicked, "Text Overlay > Background color swatch",
                       lambda: self._activate_color_target("bg"))
        bg_row.addWidget(self._bg_color_btn)
        bg_row.addWidget(QLabel("opacity %:"))
        self._bg_opacity_edit = QLineEdit(str(round(e.bg_opacity * 100)) if e else "0")
        self._bg_opacity_edit.setFixedWidth(50)
        bg_row.addWidget(self._bg_opacity_edit)
        bg_row.addStretch(1)

        outline_row = QHBoxLayout()
        self._outline_check = QCheckBox("Outline")
        self._outline_check.setChecked(e.outline_enabled if e else False)
        self._outline_check.stateChanged.connect(self._on_field_change)
        self._outline_check.setFixedWidth(110)
        outline_row.addWidget(self._outline_check)
        self._outline_color_btn = QPushButton()
        self._outline_color_btn.setFixedSize(40, 24)
        self._outline_color_btn.setStyleSheet(f"background-color: {self._outline_color};")
        connect_button(self._outline_color_btn.clicked, "Text Overlay > Outline color swatch",
                       lambda: self._activate_color_target("outline"))
        outline_row.addWidget(self._outline_color_btn)
        outline_row.addWidget(QLabel("width:"))
        self._outline_width_edit = QLineEdit(str(e.outline_width) if e else "2")
        self._outline_width_edit.setFixedWidth(50)
        outline_row.addWidget(self._outline_width_edit)
        outline_row.addStretch(1)
        mid_left.addLayout(outline_row)

        curve_row = row(mid_left, "Size curve:")
        curve_labels = {key: label for key, (label, _params) in CURVE_PRESETS.items()}
        self._curve_label_to_key = {v: k for k, v in curve_labels.items()}
        self._curve_combo = QComboBox()
        self._curve_combo.addItems(list(curve_labels.values()))
        self._curve_combo.setCurrentText(curve_labels[e.curve_preset] if e and e.curve_preset in curve_labels
                                          else curve_labels["none"])
        self._curve_combo.currentTextChanged.connect(self._on_curve_changed)
        curve_row.addWidget(self._curve_combo)
        curve_row.addStretch(1)

        self._param_frame = QVBoxLayout()
        mid_left.addLayout(self._param_frame)
        self._rebuild_param_fields()

        fade_row = row(mid_left, "Fade in/out (s):")
        self._fade_in_edit = QLineEdit(str(e.fade_in_s) if e else "0.25")
        self._fade_in_edit.setFixedWidth(60)
        fade_row.addWidget(self._fade_in_edit)
        self._fade_out_edit = QLineEdit(str(e.fade_out_s) if e else "0.25")
        self._fade_out_edit.setFixedWidth(60)
        fade_row.addWidget(self._fade_out_edit)
        fade_row.addStretch(1)

        for edit in (self._start_edit, self._duration_edit, self._x_edit, self._y_edit, self._rot_edit,
                     self._size_edit, self._bg_opacity_edit, self._outline_width_edit,
                     self._fade_in_edit, self._fade_out_edit):
            edit.textChanged.connect(self._on_field_change)

        actions = QHBoxLayout()
        refresh_btn = QPushButton("Refresh Frame")
        connect_button(refresh_btn.clicked, "Text Overlay > Refresh Frame", lambda: self._grab_frame())
        actions.addWidget(refresh_btn)
        actions.addStretch(1)
        close_btn = QPushButton("Close")
        connect_button(close_btn.clicked, "Text Overlay > Close", self._on_close_clicked)
        actions.addWidget(close_btn)
        add_btn = QPushButton(icon("check", "#06181c"), " Update Overlay" if e else " Add to Timeline")
        add_btn.setProperty("variant", "primary")
        connect_button(add_btn.clicked, "Text Overlay > Add/Update", self._add)
        actions.addWidget(add_btn)
        left_layout.addLayout(actions)

        bake_row = QHBoxLayout()
        self._bake_btn = QPushButton(icon("export", "#100c2b"), " Bake Preview")
        self._bake_btn.setProperty("variant", "violet")
        connect_button(self._bake_btn.clicked, "Text Overlay > Bake Preview", self._bake_preview)
        bake_row.addWidget(self._bake_btn)
        self._clear_bake_btn = QPushButton("Clear Bake")
        self._clear_bake_btn.setEnabled(False)
        connect_button(self._clear_bake_btn.clicked, "Text Overlay > Clear Bake", self._clear_bake)
        bake_row.addWidget(self._clear_bake_btn)
        bake_row.addWidget(QLabel("Quality:"))
        self._bake_quality_combo = QComboBox()
        self._bake_quality_combo.addItems(["Full", "720", "480", "360", "270"])
        self._bake_quality_combo.setCurrentText("480")
        bake_row.addWidget(self._bake_quality_combo)
        bake_row.addStretch(1)
        left_layout.addLayout(bake_row)

        bake_status_row = QHBoxLayout()
        self._bake_progress = QProgressBar()
        self._bake_progress.setRange(0, 100)
        bake_status_row.addWidget(self._bake_progress, 1)
        self._bake_status_lbl = QLabel("Not baked -- preview may be laggy while scrubbing.")
        self._bake_status_lbl.setProperty("muted", True)
        bake_status_row.addWidget(self._bake_status_lbl)
        left_layout.addLayout(bake_status_row)
        left_layout.addStretch(1)

        self._live_section = QWidget()
        live_layout = QVBoxLayout(self._live_section)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_hint = QLabel("Live preview over grabbed frame (drag the box to place text):")
        live_hint.setProperty("muted", True)
        live_layout.addWidget(live_hint)
        self._preview_area = _BoxPreviewArea(self)
        live_layout.addWidget(self._preview_area, 1)   # takes the pane's free height (fluid)
        anim_hint = QLabel("Animation preview (scrub through the overlay's duration):")
        anim_hint.setProperty("muted", True)
        live_layout.addWidget(anim_hint)
        self._anim_scrubber = Scrubber()
        self._anim_scrubber.add_seek_callback(self._on_anim_scrub)
        live_layout.addWidget(self._anim_scrubber)
        self._anim_time_lbl = QLabel("0.00s / 2.00s")
        self._anim_time_lbl.setProperty("mono", True)
        live_layout.addWidget(self._anim_time_lbl)
        self._right_layout.addWidget(self._live_section, 1)

        self._baked_section = QWidget()
        baked_layout = QVBoxLayout(self._baked_section)
        baked_layout.setContentsMargins(0, 0, 0, 0)
        baked_hint = QLabel("Baked preview (actual footage behind the text):")
        baked_hint.setProperty("muted", True)
        baked_layout.addWidget(baked_hint)
        self._video_frame = QFrame()
        self._video_frame.setMinimumHeight(100)
        self._video_frame.setStyleSheet("background-color: #000;")
        baked_layout.addWidget(self._video_frame, 1)
        baked_transport = QHBoxLayout()
        self._baked_play_btn = QPushButton(icon("play", COLORS["text"]), " Play")
        connect_button(self._baked_play_btn.clicked, "Text Overlay > Play/Pause (baked)", self._toggle_baked_play)
        baked_transport.addWidget(self._baked_play_btn)
        baked_transport.addStretch(1)
        baked_layout.addLayout(baked_transport)
        self._baked_scrubber = Scrubber()
        self._baked_scrubber.add_seek_callback(self._on_baked_seek)
        self._baked_scrubber.set_locked(True)
        baked_layout.addWidget(self._baked_scrubber)
        self._baked_time_lbl = QLabel("0.00s / 0.00s")
        self._baked_time_lbl.setProperty("mono", True)
        baked_layout.addWidget(self._baked_time_lbl)
        self._right_layout.addWidget(self._baked_section, 1)
        self._baked_section.hide()

    def _rebuild_param_fields(self):
        while self._param_frame.count():
            item = self._param_frame.takeAt(0)
            layout = item.layout()
            if layout is not None:
                while layout.count():
                    sub = layout.takeAt(0)
                    if sub.widget() is not None:
                        sub.widget().deleteLater()
        self._param_edits = {}
        key = self._curve_label_to_key.get(self._curve_combo.currentText(), "none")
        _label, params = CURVE_PRESETS[key]
        e = self._existing
        existing_params = e.curve_params if (e and e.curve_preset == key) else {}
        for name, default in params.items():
            r = QHBoxLayout()
            lbl = QLabel(f"{name}:")
            lbl.setFixedWidth(110)
            r.addWidget(lbl)
            edit = QLineEdit(str(existing_params.get(name, default)))
            edit.setFixedWidth(70)
            edit.textChanged.connect(self._on_field_change)
            r.addWidget(edit)
            r.addStretch(1)
            self._param_frame.addLayout(r)
            self._param_edits[name] = edit

    def _on_curve_changed(self, _text=None):
        self._rebuild_param_fields()
        self._on_field_change()

    def _activate_color_target(self, target: str):
        self._active_color_target = target
        current = {"text": self._color, "bg": self._bg_color, "outline": self._outline_color}[target]
        self._color_picker.set_color(current)

    def _on_picker_change(self, hex_color: str):
        if self._active_color_target == "text":
            self._color = hex_color
            self._color_btn.setStyleSheet(f"background-color: {hex_color};")
        elif self._active_color_target == "bg":
            self._bg_color = hex_color
            self._bg_color_btn.setStyleSheet(f"background-color: {hex_color};")
        else:
            self._outline_color = hex_color
            self._outline_color_btn.setStyleSheet(f"background-color: {hex_color};")
        self._on_field_change()

    def set_start(self, start_s: float):
        """Called after this overlay was dragged on the timeline while this
        editor is open, so a later Update doesn't write the stale pre-drag
        start back over the move."""
        self._start_edit.setText(f"{start_s:.2f}")

    def _use_current_time(self):
        self._start_edit.setText(f"{self.preview_player.current_pos_s():.2f}")
        self._grab_frame()

    def _on_anim_scrub(self, frac: float):
        try:
            duration = max(0.05, float(self._duration_edit.text() or 2.0))
        except ValueError:
            duration = 2.0
        self._preview_t_s = frac * duration
        self._update_preview()

    # -------------------------------------------------------------- preview --

    def _grab_frame(self, pos_s: Optional[float] = None):
        """Grabs a full-res frame from the composed preview via ffmpeg. Runs
        async (QProcess, not a blocking subprocess.run call on this -- the
        GUI -- thread) since this is wired directly to the "Refresh Frame"
        button and to every field edit; a slow/stuck ffmpeg call here used to
        freeze the whole window."""
        path = self.preview_player.current_path
        if path is None:
            log.info("Text overlay editor: no composed preview loaded yet, nothing to grab")
            return
        if pos_s is None:
            pos_s = self.preview_player.current_pos_s()
        job = _FrameGrabJob(str(path), pos_s, self._on_frame_grabbed)
        self._grab_jobs.append(job)

    def _on_frame_grabbed(self, job, pos_s: float, data: bytes):
        try:
            self._grab_jobs.remove(job)
        except ValueError:
            pass
        if data:
            self._base_frame = Image.open(io.BytesIO(data)).convert("RGBA")
            log.info("Text overlay editor: grabbed preview frame at %.2fs", pos_s)
        self._update_preview()

    def invalidate_composed_preview(self):
        if self._baked:
            self._show_live_section()
        self._bake_status_lbl.setText("Timeline preview changed -- re-bake to update.")
        try:
            start_s = float(self._start_edit.text())
        except ValueError:
            start_s = None
        self._grab_frame(start_s)

    def _on_field_change(self, *_args):
        if not self._suspend_xy_sync:
            self._sync_box_from_xy_fields()
        if self._baked:
            self._show_live_section()
            self._bake_status_lbl.setText("Settings changed -- re-bake to update.")
        if self._update_timer is not None:
            self._update_timer.stop()
        from PySide6.QtCore import QTimer
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._update_preview)
        self._update_timer.start(200)

    def _sync_box_from_xy_fields(self):
        try:
            x, y = float(self._x_edit.text() or 0), float(self._y_edit.text() or 0)
        except ValueError:
            return
        self._box_ctrl.set_box_center(x, y)

    def _sync_xy_from_box(self):
        self._suspend_xy_sync = True
        try:
            cx, cy = self._box_ctrl.box.center
            self._x_edit.setText(str(round(cx)))
            self._y_edit.setText(str(round(cy)))
        finally:
            self._suspend_xy_sync = False

    def _build_overlay(self) -> Optional[TextOverlay]:
        try:
            key = self._curve_label_to_key.get(self._curve_combo.currentText(), "none")
            params = {name: float(edit.text()) for name, edit in self._param_edits.items()}
            return TextOverlay(
                text=self._text_edit.toPlainText(),
                start_s=float(self._start_edit.text() or 0),
                duration_s=max(0.05, float(self._duration_edit.text() or 2.0)),
                x=float(self._x_edit.text() or 0),
                y=float(self._y_edit.text() or 0),
                box_w=self._box_ctrl.box.w,
                box_h=self._box_ctrl.box.h,
                wrap_enabled=self._wrap_check.isChecked(),
                rotation_deg=float(self._rot_edit.text() or 0),
                font_family=self._font_combo.currentText(),
                font_size=max(1, int(float(self._size_edit.text() or 64))),
                color=self._color,
                bold=self._bold_check.isChecked(),
                italic=self._italic_check.isChecked(),
                underline=self._underline_check.isChecked(),
                bg_color=self._bg_color,
                bg_opacity=max(0.0, min(1.0, float(self._bg_opacity_edit.text() or 0) / 100.0)),
                outline_enabled=self._outline_check.isChecked(),
                outline_color=self._outline_color,
                outline_width=max(0, int(float(self._outline_width_edit.text() or 2))),
                curve_preset=key,
                curve_params=params,
                fade_in_s=max(0.0, float(self._fade_in_edit.text() or 0)),
                fade_out_s=max(0.0, float(self._fade_out_edit.text() or 0)),
            )
        except ValueError:
            return None

    def _update_preview(self):
        self._update_timer = None
        overlay = self._build_overlay()
        if overlay is None or self._base_frame is None:
            return

        t_s = max(0.0, min(self._preview_t_s, overlay.duration_s))
        t_norm = t_s / overlay.duration_s if overlay.duration_s > 0 else 0.0
        self._anim_scrubber.set_playhead(t_norm)
        self._anim_time_lbl.setText(f"{t_s:.2f}s / {overlay.duration_s:.2f}s")

        canvas_scale = self._base_frame.height / self.project.canvas_h if self.project.canvas_h else 1.0
        scale = eval_curve(overlay.curve_preset, overlay.curve_params, t_norm)
        opacity = fade_opacity(t_s, overlay.duration_s, overlay.fade_in_s, overlay.fade_out_s)
        composited = text_render.paste_overlay_on_frame(self._base_frame, overlay, scale, canvas_scale, opacity)

        # The preview area scales this to fit its current (splitter-controlled)
        # size and handles the box coordinate mapping itself -- pass the raw
        # composited frame plus the canvas dimensions the box lives in.
        self._preview_area.set_frame(pil_to_qpixmap(composited), self._box_ctrl.box,
                                     self.project.canvas_w, self.project.canvas_h)

    # ---------------------------------------------------------------- box --

    def on_box_press(self, x, y):
        s = self._preview_area.current_scale()
        if s == 0:
            return
        self._box_ctrl.begin_drag(x / s, y / s, 8 / s)

    def on_box_release(self):
        self._box_ctrl.end_drag()

    def on_box_motion(self, x, y) -> Qt.CursorShape:
        s = self._preview_area.current_scale()
        if s == 0:
            return Qt.CursorShape.CrossCursor
        if self._box_ctrl.drag_handle is not None:
            self._box_ctrl.drag_to(x / s, y / s, min_size=10 / s)
            self._sync_xy_from_box()
            self._update_preview()
        handle = self._box_ctrl.hit_test(x / s, y / s, 8 / s)
        return _cursor_for_handle(handle)

    # ---------------------------------------------------------------- bake --

    def _bake_preview(self):
        overlay = self._build_overlay()
        source_path = self.preview_player.current_path
        if overlay is None or source_path is None:
            self._bake_status_lbl.setText("Compose the timeline preview first, then bake.")
            return

        out_path = self._tmp_dir / f"preview_{id(self)}_{uuid.uuid4().hex}.mp4"
        quality = self._bake_quality_combo.currentText()
        preview_height = None if quality == "Full" else int(quality)

        self._bake_btn.setEnabled(False)
        self._bake_btn.setText(" Baking...")
        self._bake_progress.setValue(0)
        self._bake_status_lbl.setText("Rendering...")

        if self._bake_job is not None:
            self._bake_job.cancel()
        job = WorkerJob("bake_text", {"source": str(source_path), "overlay": dataclasses.asdict(overlay),
                                      "canvas_w": self.project.canvas_w, "canvas_h": self.project.canvas_h,
                                      "output": str(out_path), "preview_height": preview_height}, parent=self)
        self._bake_job = job
        job.progress.connect(lambda frac, _msg: self._bake_progress.setValue(int(frac * 100)))
        job.finished.connect(lambda result: self._on_bake_finished(job, out_path, overlay.text, result))
        job.start()

    def _on_bake_finished(self, job: WorkerJob, out_path: Path, overlay_text: str, result: dict):
        if job is not self._bake_job:
            self._delete_bake_file(out_path)
            return
        self._bake_job = None
        if result.get("cancelled"):
            self._delete_bake_file(out_path)
            return
        self._on_bake_done(bool(result.get("ok")), out_path, overlay_text)

    def _on_bake_done(self, ok: bool, out_path, overlay_text: str):
        self._bake_btn.setEnabled(True)
        self._bake_btn.setText(" Bake Preview")
        if not ok:
            self._bake_status_lbl.setText("Bake failed (see console).")
            self._delete_bake_file(out_path)
            return
        self._bake_status_lbl.setText("Baked.")
        if self.player is None:
            self.player = SharedPlayer(self)
            self.player.set_embed_widget(self._video_frame)
            self.player.set_tick_callback(self._on_baked_tick)
        self._show_baked_section()
        self.player.load(out_path, on_ready=lambda: self._baked_scrubber.set_locked(False))
        if self._last_bake_path is not None and self._last_bake_path != out_path:
            self._delete_bake_file(self._last_bake_path)
        self._last_bake_path = out_path
        log.info("Text overlay editor: baked preview for '%s'", overlay_text[:30])

    @staticmethod
    def _delete_bake_file(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.exception("Text overlay editor: failed to delete stale bake file %s", path)

    def _show_baked_section(self):
        self._baked = True
        self._clear_bake_btn.setEnabled(True)
        self._live_section.hide()
        self._baked_section.show()

    def _show_live_section(self):
        self._baked = False
        self._clear_bake_btn.setEnabled(False)
        self._baked_section.hide()
        self._live_section.show()

    def _clear_bake(self):
        if self.player is not None:
            self.player.pause()
        self._show_live_section()
        self._bake_status_lbl.setText("Not baked -- preview may be laggy while scrubbing.")

    def _on_baked_tick(self, pos_frac, duration_s, paused):
        self._baked_scrubber.set_playhead(pos_frac)
        self._baked_play_btn.setText(" Pause" if not paused else " Play")
        self._baked_play_btn.setIcon(icon("pause" if not paused else "play", COLORS["text"]))
        self._baked_time_lbl.setText(f"{pos_frac * duration_s:.2f}s / {duration_s:.2f}s")

    def _toggle_baked_play(self):
        if self.player is not None:
            self.player.play_pause()

    def _on_baked_seek(self, frac: float):
        if self.player is not None:
            self.player.seek(frac)

    # ---------------------------------------------------------------- add --

    def _add(self):
        overlay = self._build_overlay()
        if overlay is None:
            return
        self.on_save(overlay)
        verb = "updated" if self._existing else "added"
        log.info("Text overlay %s: '%s' @ %.2fs-%.2fs", verb, overlay.text[:30], overlay.start_s, overlay.end_s)
        if self._existing is None:
            self._text_edit.clear()
        self.teardown()
        self.on_close()

    # --------------------------------------------------------------- close --

    def _on_close_clicked(self):
        self.teardown()
        self.on_close()

    def teardown(self):
        if self._update_timer is not None:
            self._update_timer.stop()
            self._update_timer = None
        for job in list(self._grab_jobs):
            job.cancel()
        self._grab_jobs.clear()
        if self._bake_job is not None:
            self._bake_job.cancel()
            self._bake_job = None
        if self.player is not None:
            self.player.terminate()
            self.player = None
        if self._last_bake_path is not None:
            self._delete_bake_file(self._last_bake_path)
            self._last_bake_path = None


class _BoxPreviewArea(QWidget):
    """Composited-image preview with a draggable/resizable placement box, drawn
    scaled-to-fit its current widget size so a vertical splitter can shrink it
    freely. The box lives in project-canvas pixel space; current_scale() gives
    the displayed-pixels-per-canvas-pixel factor used to draw it and to map
    mouse positions back to canvas coordinates."""

    def __init__(self, owner: "TextOverlayEditor"):
        super().__init__()
        self._owner = owner
        self._pixmap: Optional[QPixmap] = None
        self._box = None
        self._canvas_w = 1
        self._canvas_h = 1
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(80, 60)

    def set_frame(self, pixmap: QPixmap, box, canvas_w: int, canvas_h: int):
        self._pixmap = pixmap
        self._box = box
        self._canvas_w = max(1, canvas_w)
        self._canvas_h = max(1, canvas_h)
        self.update()

    def _draw_size(self) -> tuple[float, float]:
        aw, ah = self.width(), self.height()
        if aw <= 0 or ah <= 0:
            return (0.0, 0.0)
        fit = min(aw / self._canvas_w, ah / self._canvas_h)
        return (self._canvas_w * fit, self._canvas_h * fit)

    def current_scale(self) -> float:
        """Displayed pixels per canvas pixel (0 before anything is shown)."""
        dw, _dh = self._draw_size()
        return dw / self._canvas_w if self._canvas_w else 0.0

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        dw, dh = self._draw_size()
        if self._pixmap is not None and dw > 0:
            scaled = self._pixmap.scaled(round(dw), round(dh),
                                         Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(0, 0, scaled)
        if self._box is not None:
            s = self.current_scale()
            b = self._box
            _paint_box(p, b.left * s, b.top * s, b.right * s, b.bottom * s)
        p.end()

    def mousePressEvent(self, event: QMouseEvent):
        self._owner.on_box_press(event.position().x(), event.position().y())

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._owner.on_box_release()

    def mouseMoveEvent(self, event: QMouseEvent):
        cursor = self._owner.on_box_motion(event.position().x(), event.position().y())
        self.setCursor(cursor)


class _FrameGrabJob:
    """One full-res frame grab from the composed preview, run through the
    shared job_queue at HIGH priority (it's directly user-triggered)."""

    def __init__(self, path: str, pos_s: float, on_done: Callable[["_FrameGrabJob", float, bytes], None]):
        self._handle = submit("ffmpeg", ["-v", "error", "-ss", str(pos_s), "-i", path, "-frames:v", "1",
                                         "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                              lambda _code, data: on_done(self, pos_s, data), HIGH)

    def cancel(self):
        self._handle.cancel()
