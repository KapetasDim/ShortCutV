from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                                QVBoxLayout, QWidget)

from ..crop_box import ASPECT_RATIO_LABELS, ASPECT_RATIO_VALUES
from ..logging_config import get_logger
from ..project import TimelineClip
from . import media_info
from .crop_widgets import VideoCropOverlay
from .dialogs import ask_save_filename
from .icons import icon
from .job_queue import HIGH
from .player import SharedPlayer
from .theme import COLORS
from .util import connect_button
from .widgets import RangeSlider, fmt_time
from .worker_client import WorkerJob

log = get_logger()


class ClipTrimEditor(QWidget):
    """Panel for adjusting a single timeline clip's in/out points directly,
    plus exporting the trimmed range or a single frame from it. Embedded as a
    tab in the Timeline tab's right-side notebook; on_close is invoked after
    Apply or Close to hand control back to whoever opened this tab."""

    def __init__(self, parent, clip: TimelineClip, on_change: Callable[[], None], on_close: Callable[[], None]):
        super().__init__(parent)

        self.clip = clip
        self.on_change = on_change
        self.on_close = on_close
        self.player = SharedPlayer(self)
        self._crop_overlay: Optional[VideoCropOverlay] = None
        self._jobs: list[WorkerJob] = []

        # Source size comes from the shared probe cache -- normally already
        # filled when the clip was added. If not (file changed since), probe
        # asynchronously and fix the crop overlay up when it lands, instead
        # of blocking the GUI thread on ffprobe while the editor opens.
        info = media_info.cached(clip.path)
        self._src_w = (info.width if info else 0) or 1920
        self._src_h = (info.height if info else 0) or 1080
        if info is None:
            media_info.request(clip.path, self._on_source_probed, priority=HIGH)
        self._crop_top = clip.crop_top
        self._crop_bottom = clip.crop_bottom
        self._crop_left = clip.crop_left
        self._crop_right = clip.crop_right

        self._build_ui()

        self._crop_overlay = VideoCropOverlay(
            self._preview_frame, on_change=self._on_crop_overlay_change, content_rect_fn=self._video_content_rect)
        self._crop_overlay.set_crop_fractions(
            self._crop_top / self._src_h, self._crop_bottom / self._src_h,
            self._crop_left / self._src_w, self._crop_right / self._src_w,
        )
        self._update_crop_label()

        self.player.set_embed_widget(self._preview_frame)
        self.player.set_tick_callback(self._on_tick)
        self.player.load(clip.path, on_ready=self._on_ready)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        header = QLabel("Preview:")
        header.setProperty("heading", True)
        root.addWidget(header)

        crop_row = QHBoxLayout()
        crop_row.addWidget(QLabel("Crop aspect ratio:"))
        self._aspect_combo = QComboBox()
        self._aspect_combo.addItems(ASPECT_RATIO_LABELS)
        self._aspect_combo.currentTextChanged.connect(self._on_aspect_change)
        crop_row.addWidget(self._aspect_combo)
        self._crop_lbl = QLabel("")
        self._crop_lbl.setProperty("mono", True)
        crop_row.addWidget(self._crop_lbl)
        crop_row.addStretch(1)
        root.addLayout(crop_row)

        self._preview_frame = QFrame()
        self._preview_frame.setStyleSheet("background-color: black;")
        self._preview_frame.setMinimumHeight(280)
        root.addWidget(self._preview_frame, 1)

        ctrl = QHBoxLayout()
        self._play_btn = QPushButton(icon("play", COLORS["text"]), " Play")
        connect_button(self._play_btn.clicked, "Clip Trim > Play/Pause", self._toggle_play)
        ctrl.addWidget(self._play_btn)
        self._time_lbl = QLabel("--:--:--.--- / --:--:--.---")
        self._time_lbl.setProperty("mono", True)
        ctrl.addWidget(self._time_lbl)
        ctrl.addStretch(1)
        root.addLayout(ctrl)

        self._slider = RangeSlider()
        self._slider.add_callback(self._on_range_changed)
        self._slider.add_seek_callback(self._on_seek)
        root.addWidget(self._slider)

        bot = QHBoxLayout()
        self._in_lbl = QLabel("In:  --:--:--.---")
        self._in_lbl.setStyleSheet(f"color: {COLORS['success']};")
        bot.addWidget(self._in_lbl)
        self._out_lbl = QLabel("Out:  --:--:--.---")
        self._out_lbl.setStyleSheet(f"color: {COLORS['danger']};")
        bot.addWidget(self._out_lbl)
        bot.addStretch(1)
        root.addLayout(bot)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Audio volume %:"))
        self._volume_edit = QLineEdit(str(round(self.clip.audio_volume * 100)))
        self._volume_edit.setFixedWidth(60)
        vol_row.addWidget(self._volume_edit)
        vol_row.addStretch(1)
        root.addLayout(vol_row)

        actions = QHBoxLayout()
        apply_btn = QPushButton(icon("check", "#06181c"), " Apply to Timeline")
        apply_btn.setProperty("variant", "primary")
        connect_button(apply_btn.clicked, "Clip Trim > Apply to Timeline", self._apply)
        actions.addWidget(apply_btn)
        actions.addStretch(1)
        close_btn = QPushButton("Close")
        connect_button(close_btn.clicked, "Clip Trim > Close", self._on_close_clicked)
        actions.addWidget(close_btn)
        export_frame_btn = QPushButton(icon("image", "#100c2b"), " Export Frame...")
        export_frame_btn.setProperty("variant", "violet")
        connect_button(export_frame_btn.clicked, "Clip Trim > Export Frame", self._export_frame)
        actions.addWidget(export_frame_btn)
        export_clip_btn = QPushButton(icon("export", "#100c2b"), " Export Clip...")
        export_clip_btn.setProperty("variant", "violet")
        connect_button(export_clip_btn.clicked, "Clip Trim > Export Clip", self._export_clip)
        actions.addWidget(export_clip_btn)
        root.addLayout(actions)

        prog = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        prog.addWidget(self._progress, 1)
        self._status_lbl = QLabel("")
        self._status_lbl.setProperty("muted", True)
        prog.addWidget(self._status_lbl)
        root.addLayout(prog)

    # -------------------------------------------------------------- crop --

    def _video_content_rect(self, aw: int, ah: int) -> tuple[int, int, int, int]:
        scale = min(aw / self._src_w, ah / self._src_h)
        rw, rh = max(1, round(self._src_w * scale)), max(1, round(self._src_h * scale))
        return ((aw - rw) // 2, (ah - rh) // 2, rw, rh)

    def _on_aspect_change(self, _text=None):
        self._crop_overlay.set_aspect_ratio(ASPECT_RATIO_VALUES.get(self._aspect_combo.currentText()))

    def _on_crop_overlay_change(self, top_f: float, bottom_f: float, left_f: float, right_f: float):
        self._crop_top = round(top_f * self._src_h)
        self._crop_bottom = round(bottom_f * self._src_h)
        self._crop_left = round(left_f * self._src_w)
        self._crop_right = round(right_f * self._src_w)
        self._update_crop_label()

    def _update_crop_label(self):
        out_w = self._src_w - self._crop_left - self._crop_right
        out_h = self._src_h - self._crop_top - self._crop_bottom
        self._crop_lbl.setText(f"top={self._crop_top}px  bottom={self._crop_bottom}px  "
                                f"left={self._crop_left}px  right={self._crop_right}px  ->  {out_w}x{out_h}")

    def _on_source_probed(self, info: media_info.MediaInfo):
        if self._crop_overlay is None or not (info.width and info.height):
            return
        self._src_w, self._src_h = info.width, info.height
        self._crop_overlay.set_crop_fractions(
            self._crop_top / self._src_h, self._crop_bottom / self._src_h,
            self._crop_left / self._src_w, self._crop_right / self._src_w)
        self._update_crop_label()

    # ------------------------------------------------------------- loading --

    def _on_ready(self):
        d = self.player.duration_s
        if d <= 0:
            return
        self._slider.start = max(0.0, min(1.0, self.clip.in_s / d))
        self._slider.end = max(self._slider.start + 0.001, min(1.0, self.clip.out_s / d))
        self._slider.set_playhead(self._slider.start)
        self._update_in_out_labels()

    def _on_tick(self, pos_frac, duration_s, paused):
        self._slider.set_playhead(pos_frac)
        pos_ms = pos_frac * duration_s * 1000
        self._time_lbl.setText(f"{fmt_time(pos_ms)} / {fmt_time(duration_s * 1000)}")
        self._play_btn.setText(" Pause" if not paused else " Play")
        self._play_btn.setIcon(icon("pause" if not paused else "play", COLORS["text"]))

    # ---------------------------------------------------------------- logic --

    def _toggle_play(self):
        self.player.play_pause()

    def _on_range_changed(self, _start, _end):
        self._update_in_out_labels()

    def _on_seek(self, frac: float):
        self.player.seek(frac)

    def _update_in_out_labels(self):
        d = self.player.duration_s
        if d <= 0:
            return
        self._in_lbl.setText(f"In:  {fmt_time(self._slider.start * d * 1000)}")
        self._out_lbl.setText(f"Out:  {fmt_time(self._slider.end * d * 1000)}")

    def _apply(self):
        d = self.player.duration_s
        if d <= 0:
            return
        self.clip.in_s = self._slider.start * d
        self.clip.out_s = self._slider.end * d
        self.clip.crop_top = self._crop_top
        self.clip.crop_bottom = self._crop_bottom
        self.clip.crop_left = self._crop_left
        self.clip.crop_right = self._crop_right
        try:
            self.clip.audio_volume = max(0.0, min(2.0, float(self._volume_edit.text() or 0) / 100.0))
        except ValueError:
            pass
        self.on_change()
        log.info("Clip trim editor: applied %.2fs-%.2fs to %s",
                  self.clip.in_s, self.clip.out_s, self.clip.path.name)
        self.teardown()
        self.on_close()

    def _export_clip(self):
        d = self.player.duration_s
        if d <= 0:
            return
        in_s = self._slider.start * d
        out_s = self._slider.end * d
        default_name = f"{self.clip.path.stem}_{fmt_time(in_s * 1000).replace(':', '-').replace('.', '_')}.mp4"
        self.player.pause()
        self.player.suspend_polling()

        def on_path(out_path: Optional[Path]):
            self.player.resume_polling()
            if not out_path:
                return
            self._run_export_clip(in_s, out_s, out_path)

        ask_save_filename(self, on_path, title="Export clip as...", defaultextension=".mp4",
                           initialfile=default_name, filetypes=[("MP4 video", "*.mp4")])

    def _run_export_clip(self, in_s: float, out_s: float, out_path: Path):
        self._progress.setValue(0)
        self._status_lbl.setText("Exporting...")
        self._start_job("export_clip", {"path": str(self.clip.path), "in_s": in_s, "out_s": out_s,
                                        "output": str(out_path)}, out_path)

    def _start_job(self, kind: str, params: dict, out_path: Path):
        job = WorkerJob(kind, params, parent=self)
        self._jobs.append(job)
        job.progress.connect(lambda frac, _msg: self._progress.setValue(int(frac * 100)))
        job.finished.connect(lambda result: self._on_job_done(job, out_path, result))
        job.start()

    def _on_job_done(self, job: WorkerJob, out_path: Path, result: dict):
        if job in self._jobs:
            self._jobs.remove(job)
        if result.get("cancelled"):
            return
        self._status_lbl.setText(f"Saved: {out_path.name}" if result.get("ok")
                                 else "Export failed (see console)")

    def _export_frame(self):
        d = self.player.duration_s
        if d <= 0:
            return
        pos_s = self._slider.playhead * d
        default_name = f"{self.clip.path.stem}_{fmt_time(pos_s * 1000).replace(':', '-').replace('.', '_')}.png"
        self.player.pause()
        self.player.suspend_polling()

        def on_path(out_path: Optional[Path]):
            self.player.resume_polling()
            if not out_path:
                return
            self._run_export_frame(pos_s, out_path)

        ask_save_filename(self, on_path, title="Export frame as...", defaultextension=".png",
                           initialfile=default_name, filetypes=[("PNG image", "*.png")])

    def _run_export_frame(self, pos_s: float, out_path: Path):
        self._status_lbl.setText("Exporting frame...")
        self._start_job("export_frame", {"path": str(self.clip.path), "pos_s": pos_s,
                                         "output": str(out_path)}, out_path)

    # --------------------------------------------------------------- close --

    def _on_close_clicked(self):
        self.teardown()
        self.on_close()

    def teardown(self):
        """Idempotent cleanup of non-Qt resources (the mpv subprocess, the
        floating crop-overlay window). A running export keeps going -- it
        writes to a file the user picked, independent of this editor."""
        self.player.terminate()
        if self._crop_overlay is not None:
            self._crop_overlay.destroy()
            self._crop_overlay = None
