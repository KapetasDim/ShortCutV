"""Source viewer dock panel: previews whatever's selected in the Bin, lets
you pick in/out points, add the trimmed range to the timeline, or export a
clip/frame straight from the source file. Replaces the old Clip Exporter tab
(folder/video pickers are gone -- selection now comes from the Bin panel)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from ..logging_config import get_logger
from .crop_resize_tab import CropResizeTab
from .dialogs import ask_save_filename
from .icons import icon
from .player import SharedPlayer
from .theme import COLORS
from .util import connect_button
from .widgets import RangeSlider, fmt_time
from .worker_client import WorkerJob

log = get_logger()


class SourceViewerPanel(QWidget):
    add_to_timeline = Signal(Path, str, float, float)  # path, kind, in_s, out_s

    def __init__(self, player: SharedPlayer, parent=None):
        super().__init__(parent)
        self.player = player
        self._path: Optional[Path] = None
        self._kind: str = "video"
        self._jobs: list[WorkerJob] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self._name_lbl = QLabel("No clip selected -- pick one in the Bin")
        self._name_lbl.setProperty("heading", True)
        layout.addWidget(self._name_lbl)

        self._preview_frame = QFrame()
        self._preview_frame.setStyleSheet("background-color: black;")
        self._preview_frame.setMinimumHeight(280)
        layout.addWidget(self._preview_frame, 1)

        ctrl = QHBoxLayout()
        self._play_btn = QPushButton(icon("play", COLORS["text"]), " Play")
        connect_button(self._play_btn.clicked, "Source Viewer > Play/Pause", self._toggle_play)
        ctrl.addWidget(self._play_btn)
        self._time_lbl = QLabel("--:--:--.--- / --:--:--.---")
        self._time_lbl.setProperty("mono", True)
        ctrl.addWidget(self._time_lbl)
        ctrl.addStretch(1)
        layout.addLayout(ctrl)

        self._slider = RangeSlider()
        self._slider.add_callback(self._on_range_changed)
        self._slider.add_seek_callback(self._on_seek)
        layout.addWidget(self._slider)

        bot = QHBoxLayout()
        self._in_lbl = QLabel("In:  --:--:--.---")
        self._in_lbl.setStyleSheet(f"color: {COLORS['success']};")
        bot.addWidget(self._in_lbl)
        self._out_lbl = QLabel("Out:  --:--:--.---")
        self._out_lbl.setStyleSheet(f"color: {COLORS['danger']};")
        bot.addWidget(self._out_lbl)
        bot.addStretch(1)
        layout.addLayout(bot)

        actions = QHBoxLayout()
        self._add_btn = QPushButton(icon("plus", "#06181c"), " Add to Timeline")
        self._add_btn.setProperty("variant", "primary")
        connect_button(self._add_btn.clicked, "Source Viewer > Add to Timeline", self._add_to_timeline)
        actions.addWidget(self._add_btn)
        actions.addStretch(1)
        self._frame_btn = QPushButton(icon("image", "#100c2b"), " Export Frame...")
        self._frame_btn.setProperty("variant", "violet")
        connect_button(self._frame_btn.clicked, "Source Viewer > Export Frame", self._export_frame)
        actions.addWidget(self._frame_btn)
        self._export_btn = QPushButton(icon("export", "#100c2b"), " Export Clip...")
        self._export_btn.setProperty("variant", "violet")
        connect_button(self._export_btn.clicked, "Source Viewer > Export Clip", self._export_clip)
        actions.addWidget(self._export_btn)
        layout.addLayout(actions)

        prog = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        prog.addWidget(self._progress, 1)
        self._status_lbl = QLabel("")
        self._status_lbl.setProperty("muted", True)
        prog.addWidget(self._status_lbl)
        layout.addLayout(prog)

        self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled: bool):
        for w in (self._play_btn, self._slider, self._add_btn, self._frame_btn, self._export_btn):
            w.setEnabled(enabled)

    def activate(self):
        """Call when this panel becomes the visible/active one -- points the
        shared player at this panel's preview frame."""
        self.player.set_embed_widget(self._preview_frame)
        self.player.set_tick_callback(self._on_tick)

    def deactivate(self):
        self.player.set_tick_callback(None)

    def load(self, path: Path, kind: str):
        self._path = path
        self._kind = kind
        self._name_lbl.setText(path.name)
        self._slider.start = 0.0
        self._slider.end = 1.0
        self._slider.set_playhead(0.0)
        self._status_lbl.setText("")
        self._set_controls_enabled(kind != "sound")
        if kind == "sound":
            self._status_lbl.setText("Select a sound from the Bin's context menu to add it at the playhead.")
            return
        self.player.load(path, on_ready=self._update_in_out_labels, is_image=(kind == "image"))
        log.info("Source viewer: loaded %s", path.name)

    def _on_tick(self, pos_frac, duration_s, paused):
        self._slider.set_playhead(pos_frac)
        pos_ms = pos_frac * duration_s * 1000
        self._time_lbl.setText(f"{fmt_time(pos_ms)} / {fmt_time(duration_s * 1000)}")
        self._play_btn.setIcon(icon("pause" if not paused else "play", COLORS["text"]))
        self._play_btn.setText(" Pause" if not paused else " Play")

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

    def _add_to_timeline(self):
        if self._path is None:
            return
        d = self.player.duration_s
        if self._kind == "image":
            self.add_to_timeline.emit(self._path, self._kind, 0.0, 0.0)
            return
        if d <= 0:
            return
        self.add_to_timeline.emit(self._path, self._kind, self._slider.start * d, self._slider.end * d)

    def _export_clip(self):
        if self._path is None:
            return
        d = self.player.duration_s
        if d <= 0:
            return
        in_s, out_s = self._slider.start * d, self._slider.end * d
        default_name = f"{self._path.stem}_{fmt_time(in_s * 1000).replace(':', '-').replace('.', '_')}.mp4"
        self.player.pause()
        self.player.suspend_polling()

        def on_path(out_path: Optional[Path]):
            self.player.resume_polling()
            if not out_path:
                return
            self._run_export_clip(in_s, out_s, out_path)

        ask_save_filename(self, on_path, title="Export clip as...", defaultextension=".mp4",
                           initialfile=default_name, filetypes=[("MP4 video", "*.mp4")])

    def _run_export_clip(self, in_s, out_s, out_path):
        self._export_btn.setEnabled(False)
        self._progress.setValue(0)
        self._status_lbl.setText("Exporting...")
        self._start_job("export_clip", {"path": str(self._path), "in_s": in_s, "out_s": out_s,
                                        "output": str(out_path)}, out_path, self._export_btn)

    def _start_job(self, kind: str, params: dict, out_path: Path, button):
        job = WorkerJob(kind, params, parent=self)
        self._jobs.append(job)
        job.progress.connect(lambda frac, _msg: self._progress.setValue(int(frac * 100)))
        job.finished.connect(lambda result: self._on_job_done(job, out_path, button, result))
        job.start()

    def _on_job_done(self, job: WorkerJob, out_path: Path, button, result: dict):
        if job in self._jobs:
            self._jobs.remove(job)
        button.setEnabled(True)
        if not result.get("cancelled"):
            self._status_lbl.setText(f"Saved: {out_path.name}" if result.get("ok") else "Export failed")

    def _export_frame(self):
        if self._path is None:
            return
        d = self.player.duration_s
        if d <= 0:
            return
        pos_s = self._slider.playhead * d
        default_name = f"{self._path.stem}_{fmt_time(pos_s * 1000).replace(':', '-').replace('.', '_')}.png"
        self.player.pause()
        self.player.suspend_polling()

        def on_path(out_path: Optional[Path]):
            self.player.resume_polling()
            if not out_path:
                return
            self._run_export_frame(pos_s, out_path)

        ask_save_filename(self, on_path, title="Export frame as...", defaultextension=".png",
                           initialfile=default_name, filetypes=[("PNG image", "*.png")])

    def _run_export_frame(self, pos_s, out_path):
        self._frame_btn.setEnabled(False)
        self._status_lbl.setText("Exporting frame...")
        self._start_job("export_frame", {"path": str(self._path), "pos_s": pos_s, "output": str(out_path)},
                        out_path, self._frame_btn)


class BatchCropResizeDialog(QDialog):
    """Wraps the batch crop/resize workflow (multiple files -> new cropped
    files in an output folder) in a modal dialog, reachable from the Bin's
    context menu -- replaces the old standalone Crop & Resize tab."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch Crop / Resize")
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self._tab = CropResizeTab(self, state)
        layout.addWidget(self._tab, 1)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        connect_button(close_btn.clicked, "Batch Crop/Resize > Close", self.accept)
        self.finished.connect(lambda _code: self._tab.teardown())
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def preselect_input(self, input_dir: Path):
        self._tab._input_dir = input_dir
        self._tab._input_lbl.setText(f"Input: {input_dir}")
        self._tab.state.crop_input_dir = input_dir
        self._tab._refresh_files()
