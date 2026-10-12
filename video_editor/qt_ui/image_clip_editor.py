from __future__ import annotations

import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                                QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import ffmpeg_ops, image_render
from ..curves import CURVE_PRESETS, eval_curve
from ..logging_config import get_logger
from ..project import TimelineClip
from .icons import icon
from .inspector_split import make_preview_splitter
from .player import SharedPlayer
from .theme import COLORS
from .util import connect_button, run_on_ui
from .widgets import Scrubber, pil_to_qpixmap

log = get_logger()


class ImageClipEditor(QWidget):
    """Settings panel for an image-as-clip timeline entry: how long it stays
    on screen and whether/how it Ken-Burns zooms. "Bake Preview" renders the
    animation to a short silent .mp4 and switches to playing that back
    through the mpv-embedded transport, for smooth scrubbing/playback."""

    def __init__(self, parent, clip: TimelineClip, canvas_w: int, canvas_h: int,
                 on_change: Callable[[], None], on_close: Callable[[], None]):
        super().__init__(parent)

        self.clip = clip
        self.canvas_w = max(1, canvas_w)
        self.canvas_h = max(1, canvas_h)
        self.on_change = on_change
        self.on_close = on_close
        self._fitted: Optional[Image.Image] = None
        self._preview_t_s = 0.0
        self._baked = False
        self.player: Optional[SharedPlayer] = None
        self._last_bake_path: Optional[Path] = None
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_image_preview"
        self._tmp_dir.mkdir(exist_ok=True)
        self._param_edits: dict[str, QLineEdit] = {}
        self._curve_label_to_key: dict[str, str] = {}

        self._build_ui()
        self._load_source()
        self._update_preview()

    # ------------------------------------------------------------------ UI --
    # Stacked vertically (preview on top, controls scrolling below) rather
    # than the old side-by-side left-controls/right-preview split -- that
    # split assumed a wide standalone editor window; hosted inside the
    # narrow Inspector dock it forced a wide minimum width that fought the
    # dock's own layout and made the whole panel's height behave erratically.

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Preview (top) and controls (bottom) share one draggable splitter.
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

        splitter = make_preview_splitter(preview_container, scroll, "inspector/split_image_h")
        root.addWidget(splitter, 1)

        def row(parent_layout, label):
            r = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(110)
            r.addWidget(lbl)
            parent_layout.addLayout(r)
            return r

        dur_row = row(left_layout, "Duration (s):")
        self._duration_edit = QLineEdit(f"{self.clip.duration:.2f}")
        self._duration_edit.setFixedWidth(80)
        self._duration_edit.textChanged.connect(self._on_field_change)
        dur_row.addWidget(self._duration_edit)
        dur_row.addStretch(1)

        self._zoom_check = QCheckBox("Zoom in over duration (Ken Burns)")
        self._zoom_check.setChecked(self.clip.image_zoom_enabled)
        self._zoom_check.stateChanged.connect(self._on_field_change)
        left_layout.addWidget(self._zoom_check)

        zf_row = row(left_layout, "Zoom factor:")
        self._zoom_factor_edit = QLineEdit(str(self.clip.image_zoom_factor))
        self._zoom_factor_edit.setFixedWidth(80)
        self._zoom_factor_edit.textChanged.connect(self._on_field_change)
        zf_row.addWidget(self._zoom_factor_edit)
        zf_row.addStretch(1)

        curve_row = row(left_layout, "Zoom curve:")
        curve_labels = {key: label for key, (label, _params) in CURVE_PRESETS.items()}
        self._curve_label_to_key = {v: k for k, v in curve_labels.items()}
        self._curve_combo = QComboBox()
        self._curve_combo.addItems(list(curve_labels.values()))
        self._curve_combo.setCurrentText(curve_labels.get(self.clip.image_zoom_curve, curve_labels["none"]))
        self._curve_combo.currentTextChanged.connect(self._on_curve_changed)
        curve_row.addWidget(self._curve_combo)
        curve_row.addStretch(1)

        self._param_frame = QVBoxLayout()
        left_layout.addLayout(self._param_frame)
        self._rebuild_param_fields()

        actions = QHBoxLayout()
        apply_btn = QPushButton(icon("check", "#06181c"), " Apply to Timeline")
        apply_btn.setProperty("variant", "primary")
        connect_button(apply_btn.clicked, "Image Clip > Apply to Timeline", self._apply)
        actions.addWidget(apply_btn)
        actions.addStretch(1)
        close_btn = QPushButton("Close")
        connect_button(close_btn.clicked, "Image Clip > Close", self._on_close_clicked)
        actions.addWidget(close_btn)
        left_layout.addLayout(actions)

        bake_row = QHBoxLayout()
        self._bake_btn = QPushButton(icon("export", "#100c2b"), " Bake Preview")
        self._bake_btn.setProperty("variant", "violet")
        connect_button(self._bake_btn.clicked, "Image Clip > Bake Preview", self._bake_preview)
        bake_row.addWidget(self._bake_btn)
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
        live_hint = QLabel("Preview (contain-fit to output canvas):")
        live_hint.setProperty("muted", True)
        live_layout.addWidget(live_hint)
        self._preview_lbl = _FluidImage()   # scales to fit the splitter-controlled pane
        live_layout.addWidget(self._preview_lbl, 1)
        zoom_hint = QLabel("Zoom preview (scrub through the clip's duration):")
        zoom_hint.setProperty("muted", True)
        live_layout.addWidget(zoom_hint)
        self._scrubber = Scrubber()
        self._scrubber.add_seek_callback(self._on_scrub)
        live_layout.addWidget(self._scrubber)
        self._time_lbl = QLabel("0.00s / 1.50s")
        self._time_lbl.setProperty("mono", True)
        live_layout.addWidget(self._time_lbl)
        self._right_layout.addWidget(self._live_section, 1)

        self._baked_section = QWidget()
        baked_layout = QVBoxLayout(self._baked_section)
        baked_layout.setContentsMargins(0, 0, 0, 0)
        baked_hint = QLabel("Baked preview:")
        baked_hint.setProperty("muted", True)
        baked_layout.addWidget(baked_hint)
        self._video_frame = QFrame()
        self._video_frame.setMinimumHeight(100)
        self._video_frame.setStyleSheet("background-color: #000;")
        baked_layout.addWidget(self._video_frame, 1)
        baked_transport = QHBoxLayout()
        self._baked_play_btn = QPushButton(icon("play", COLORS["text"]), " Play")
        connect_button(self._baked_play_btn.clicked, "Image Clip > Play/Pause (baked)", self._toggle_baked_play)
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
            w = item.widget()
            if w is not None:
                w.deleteLater()
            layout = item.layout()
            if layout is not None:
                while layout.count():
                    sub = layout.takeAt(0)
                    if sub.widget() is not None:
                        sub.widget().deleteLater()
        self._param_edits = {}
        key = self._curve_label_to_key.get(self._curve_combo.currentText(), "none")
        _label, params = CURVE_PRESETS[key]
        existing_params = self.clip.image_zoom_curve_params if self.clip.image_zoom_curve == key else {}
        for name, default in params.items():
            r = QHBoxLayout()
            lbl = QLabel(f"{name}:")
            lbl.setFixedWidth(110)
            r.addWidget(lbl)
            edit = QLineEdit(str(existing_params.get(name, default)))
            edit.setFixedWidth(80)
            edit.textChanged.connect(self._on_field_change)
            r.addWidget(edit)
            r.addStretch(1)
            self._param_frame.addLayout(r)
            self._param_edits[name] = edit

    def _on_curve_changed(self, _text=None):
        self._rebuild_param_fields()
        self._on_field_change()

    def _on_field_change(self, *_args):
        if self._baked:
            self._show_live_section()
            self._bake_status_lbl.setText("Settings changed -- re-bake to update.")
        self._update_preview()

    def _on_scrub(self, frac: float):
        try:
            duration = max(0.05, float(self._duration_edit.text() or 1.5))
        except ValueError:
            duration = 1.5
        self._preview_t_s = frac * duration
        self._update_preview()

    # -------------------------------------------------------------- preview --

    def _load_source(self):
        try:
            source = Image.open(self.clip.path)
            self._fitted = image_render.fit_canvas(source, self.canvas_w, self.canvas_h)
        except Exception:
            log.exception("Image clip editor: failed to load %s", self.clip.path)
            self._fitted = None

    def _current_settings(self):
        try:
            duration = max(0.05, float(self._duration_edit.text() or 1.5))
            zoom_factor = max(1.0, float(self._zoom_factor_edit.text() or 1.2))
            params = {name: float(edit.text()) for name, edit in self._param_edits.items()}
        except ValueError:
            return None
        key = self._curve_label_to_key.get(self._curve_combo.currentText(), "none")
        return {
            "duration": duration,
            "zoom_enabled": self._zoom_check.isChecked(),
            "zoom_factor": zoom_factor,
            "curve": key,
            "params": params,
        }

    def _update_preview(self):
        if self._fitted is None:
            return
        settings = self._current_settings()
        if settings is None:
            return

        t_s = max(0.0, min(self._preview_t_s, settings["duration"]))
        t_norm = t_s / settings["duration"] if settings["duration"] > 0 else 0.0
        self._scrubber.set_playhead(t_norm)
        self._time_lbl.setText(f"{t_s:.2f}s / {settings['duration']:.2f}s")

        if settings["zoom_enabled"]:
            progress = eval_curve(settings["curve"], settings["params"], t_norm)
            scale = 1.0 + (settings["zoom_factor"] - 1.0) * progress
            frame = image_render.compute_zoom_frame(self._fitted, self.canvas_w, self.canvas_h, scale)
        else:
            frame = self._fitted

        # _FluidImage scales this to fit its (splitter-controlled) size.
        self._preview_lbl.set_pixmap(pil_to_qpixmap(frame))

    # ---------------------------------------------------------------- bake --

    def _bake_preview(self):
        settings = self._current_settings()
        if settings is None or self._fitted is None:
            return
        tmp_clip = TimelineClip(
            source=str(self.clip.path), in_s=0.0, out_s=settings["duration"], is_image=True,
            image_zoom_enabled=settings["zoom_enabled"], image_zoom_factor=settings["zoom_factor"],
            image_zoom_curve=settings["curve"], image_zoom_curve_params=settings["params"],
        )
        out_path = self._tmp_dir / f"preview_{id(self)}_{uuid.uuid4().hex}.mp4"
        quality = self._bake_quality_combo.currentText()
        preview_height = None if quality == "Full" else int(quality)

        self._bake_btn.setEnabled(False)
        self._bake_btn.setText(" Baking...")
        self._bake_progress.setValue(0)
        self._bake_status_lbl.setText("Rendering...")

        def on_progress(frac, _label):
            run_on_ui(lambda: self._bake_progress.setValue(int(frac * 100)))

        def run():
            try:
                ok = ffmpeg_ops.render_image_clip_preview(
                    tmp_clip, self.canvas_w, self.canvas_h, out_path,
                    preview_height=preview_height, progress_cb=on_progress)
            except Exception:
                log.exception("Image clip editor: bake failed for %s", self.clip.path)
                ok = False
            run_on_ui(lambda: self._on_bake_done(ok, out_path))

        threading.Thread(target=run, daemon=True).start()

    def _on_bake_done(self, ok: bool, out_path: Path):
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
        log.info("Image clip editor: baked preview for %s", self.clip.path.name)

    @staticmethod
    def _delete_bake_file(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.exception("Image clip editor: failed to delete stale bake file %s", path)

    def _show_baked_section(self):
        self._baked = True
        self._live_section.hide()
        self._baked_section.show()

    def _show_live_section(self):
        self._baked = False
        self._baked_section.hide()
        self._live_section.show()

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

    # ---------------------------------------------------------------- apply --

    def _apply(self):
        settings = self._current_settings()
        if settings is None:
            return
        self.clip.out_s = self.clip.in_s + settings["duration"]
        self.clip.image_zoom_enabled = settings["zoom_enabled"]
        self.clip.image_zoom_factor = settings["zoom_factor"]
        self.clip.image_zoom_curve = settings["curve"]
        self.clip.image_zoom_curve_params = settings["params"]
        self.on_change()
        log.info("Image clip editor: applied duration=%.2fs zoom=%s to %s",
                  settings["duration"], settings["zoom_enabled"], self.clip.path.name)
        self.teardown()
        self.on_close()

    # --------------------------------------------------------------- close --

    def _on_close_clicked(self):
        self.teardown()
        self.on_close()

    def teardown(self):
        if self.player is not None:
            self.player.terminate()
        if self._last_bake_path is not None:
            self._delete_bake_file(self._last_bake_path)
            self._last_bake_path = None


class _FluidImage(QWidget):
    """Shows a pixmap scaled to fit the widget's current size (aspect kept, top-
    left aligned), so a vertical splitter can shrink the preview freely instead
    of the image forcing a fixed height."""

    def __init__(self):
        super().__init__()
        self._pixmap: Optional[QPixmap] = None
        self.setStyleSheet("background-color: #000;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(80, 60)

    def set_pixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        if self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(0, 0, scaled)
        p.end()
