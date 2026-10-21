from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                                QPushButton, QSplitter, QVBoxLayout, QWidget)

from ..crop_box import ASPECT_RATIO_LABELS, ASPECT_RATIO_VALUES
from ..logging_config import get_logger
from ..media_types import IMAGE_EXTS, VIDEO_EXTS
from .crop_widgets import CropCanvas
from .dialogs import ask_directory
from .icons import icon
from .theme import COLORS
from .job_queue import HIGH, JobHandle, submit
from .util import connect_button
from .widgets import ScrollableFrame, bytes_to_image
from .worker_client import WorkerJob

log = get_logger()


class CropResizeTab(QWidget):
    def __init__(self, parent, state):
        super().__init__(parent)
        self.state = state  # AppState, for chained folder defaults

        self._input_dir: Optional[Path] = None
        self._output_dir: Optional[Path] = None
        self._checks: dict[str, QCheckBox] = {}
        self._files: list[Path] = []
        self._preview_job: Optional[JobHandle] = None
        self._run_job: Optional[WorkerJob] = None

        self._build_ui()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        menu = QWidget()
        menu_layout = QVBoxLayout(menu)
        menu_layout.setContentsMargins(4, 4, 12, 4)
        splitter.addWidget(menu)

        preview_pane = QWidget()
        preview_layout = QVBoxLayout(preview_pane)
        preview_layout.setContentsMargins(12, 4, 4, 4)
        splitter.addWidget(preview_pane)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        folders_row = QHBoxLayout()
        self._input_lbl = QLineEdit("Input: (none)")
        self._input_lbl.setReadOnly(True)
        folders_row.addWidget(self._input_lbl, 1)
        browse_in = QPushButton(icon("folder", COLORS["text"]), " Browse input...")
        connect_button(browse_in.clicked, "Crop/Resize > Browse input", self._pick_input)
        folders_row.addWidget(browse_in)
        menu_layout.addLayout(folders_row)

        folders_row2 = QHBoxLayout()
        self._output_lbl = QLineEdit("Output: (none)")
        self._output_lbl.setReadOnly(True)
        folders_row2.addWidget(self._output_lbl, 1)
        browse_out = QPushButton(icon("folder", COLORS["text"]), " Browse output...")
        connect_button(browse_out.clicked, "Crop/Resize > Browse output", self._pick_output)
        folders_row2.addWidget(browse_out)
        menu_layout.addLayout(folders_row2)

        clips_label = QLabel("Clips to apply to:")
        clips_label.setProperty("heading", True)
        menu_layout.addWidget(clips_label)
        self._checklist = ScrollableFrame(height=220)
        menu_layout.addWidget(self._checklist)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Target W:"))
        self._target_w = QLineEdit()
        self._target_w.setFixedWidth(70)
        size_row.addWidget(self._target_w)
        size_row.addWidget(QLabel("Target H:"))
        self._target_h = QLineEdit()
        self._target_h.setFixedWidth(70)
        size_row.addWidget(self._target_h)
        hint = QLabel("(blank = keep cropped size)")
        hint.setProperty("muted", True)
        size_row.addWidget(hint)
        size_row.addStretch(1)
        menu_layout.addLayout(size_row)

        self._run_btn = QPushButton(icon("crop", "#06181c"), " Run Crop/Resize")
        self._run_btn.setProperty("variant", "primary")
        connect_button(self._run_btn.clicked, "Crop/Resize > Run Crop/Resize", self._run)
        menu_layout.addWidget(self._run_btn)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        menu_layout.addWidget(self._progress)
        self._status_lbl = QLabel("")
        self._status_lbl.setProperty("muted", True)
        menu_layout.addWidget(self._status_lbl)
        menu_layout.addStretch(1)

        preview_header = QHBoxLayout()
        header_lbl = QLabel("Crop preview (drag box):")
        header_lbl.setProperty("heading", True)
        preview_header.addWidget(header_lbl)
        preview_header.addSpacing(16)
        preview_header.addWidget(QLabel("Aspect ratio:"))
        self._aspect_combo = QComboBox()
        self._aspect_combo.addItems(ASPECT_RATIO_LABELS)
        self._aspect_combo.currentTextChanged.connect(self._on_aspect_change)
        preview_header.addWidget(self._aspect_combo)
        preview_header.addStretch(1)
        preview_layout.addLayout(preview_header)

        self._crop_canvas = CropCanvas()
        preview_layout.addWidget(self._crop_canvas, 1, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        preview_layout.addStretch(1)
        self._preview_pane = preview_pane

    def resizeEvent(self, event):
        super().resizeEvent(event)
        avail_w = self._preview_pane.width() - 24
        avail_h = self._preview_pane.height() - 60
        self._crop_canvas.set_max_size(avail_w, avail_h)

    def rebuild(self):
        """Re-syncs this tab's widgets to the current UI-scale profile (layout
        is stylesheet/DPI driven now, so only the custom-painted crop canvas
        needs its pixel geometry explicitly refreshed)."""
        self._crop_canvas.apply_scale()
        if self._input_dir is not None:
            self._input_lbl.setText(f"Input: {self._input_dir}")
        if self._output_dir is not None:
            self._output_lbl.setText(f"Output: {self._output_dir}")

    # -------------------------------------------------------------- folders --

    def _pick_input(self):
        def done(d: Optional[Path]):
            if not d:
                return
            self._input_dir = d
            self._input_lbl.setText(f"Input: {d}")
            self.state.crop_input_dir = self._input_dir
            self._refresh_files()

        ask_directory(self, done, title="Select input folder")

    def _pick_output(self):
        def done(d: Optional[Path]):
            if not d:
                return
            self._output_dir = d
            self._output_lbl.setText(f"Output: {d}")
            self.state.crop_output_dir = self._output_dir

        ask_directory(self, done, title="Select output folder")

    def _refresh_files(self):
        if not self._input_dir:
            return
        self._files = sorted(
            p for p in self._input_dir.iterdir()
            if p.suffix.lower() in VIDEO_EXTS or p.suffix.lower() in IMAGE_EXTS
        )
        self._checklist.clear()
        self._checks = {}
        for f in self._files:
            cb = QCheckBox(f.name)
            cb.setChecked(True)
            self._checks[f.name] = cb
            self._checklist.add_widget(cb)
        if self._files:
            self._load_preview_frame(self._files[0])
        log.info("Crop/Resize: found %d files in %s", len(self._files), self._input_dir)

    def _on_aspect_change(self, _text=None):
        ratio = ASPECT_RATIO_VALUES.get(self._aspect_combo.currentText())
        self._crop_canvas.set_aspect_ratio(ratio)

    def _load_preview_frame(self, path: Path):
        if self._preview_job is not None:
            self._preview_job.cancel()
            self._preview_job = None
        if path.suffix.lower() not in VIDEO_EXTS:
            try:
                self._crop_canvas.set_image(Image.open(path))
            except Exception:
                log.exception("Crop/Resize: failed to load preview frame for %s", path)
                self._status_lbl.setText(f"Could not preview {path.name} (see log).")
            return

        # Async (job_queue/QProcess) -- this used to be a blocking ffmpeg
        # call on the GUI thread every time a folder was picked.
        def done(_code: int, data: bytes):
            self._preview_job = None
            if not data:
                self._status_lbl.setText(f"Could not preview {path.name} (see log).")
                return
            try:
                self._crop_canvas.set_image(bytes_to_image(data))
            except Exception:
                log.exception("Crop/Resize: failed to decode preview frame for %s", path)

        self._preview_job = submit("ffmpeg", ["-v", "error", "-ss", "0", "-i", str(path), "-frames:v", "1",
                                              "-vf", "scale=1200:-2", "-f", "image2pipe", "-vcodec", "png",
                                              "pipe:1"], done, HIGH)

    # ---------------------------------------------------------------- run --

    def _run(self):
        if not self._input_dir or not self._output_dir:
            self._status_lbl.setText("Pick input and output folders first.")
            return
        selected = [f for f in self._files if self._checks[f.name].isChecked()]
        if not selected:
            self._status_lbl.setText("No clips selected.")
            return

        crop_fractions = self._crop_canvas.get_crop_fractions()
        try:
            tw = int(self._target_w.text()) if self._target_w.text().strip() else None
            th = int(self._target_h.text()) if self._target_h.text().strip() else None
        except ValueError:
            self._status_lbl.setText("Target W/H must be integers.")
            return

        videos = [f for f in selected if f.suffix.lower() in VIDEO_EXTS]
        images = [f for f in selected if f.suffix.lower() in IMAGE_EXTS]

        self._run_btn.setEnabled(False)
        self._progress.setValue(0)
        self._status_lbl.setText("Running...")
        log.info("Crop/Resize batch start: %d videos, %d images", len(videos), len(images))

        job = WorkerJob("crop_resize", {
            "videos": [str(v) for v in videos], "images": [str(i) for i in images],
            "output_dir": str(self._output_dir), "crop_fractions": list(crop_fractions),
            "target_w": tw, "target_h": th},
            # Parented to the app, not this dialog: closing the dialog lets the
            # batch finish in the background, as it always has.
            parent=QCoreApplication.instance())
        self._run_job = job
        job.progress.connect(lambda frac, msg: (self._progress.setValue(int(frac * 100)),
                                                self._status_lbl.setText(msg)))
        job.finished.connect(lambda result: self._on_run_done(len(selected), result))
        job.start()

    def _on_run_done(self, n_selected: int, result: dict):
        log.info("Crop/Resize batch done: %s", result)
        try:
            self._show_run_result(n_selected, result)
        except RuntimeError:
            pass  # dialog was closed while the batch ran

    def _show_run_result(self, n_selected: int, result: dict):
        self._run_job = None
        self._run_btn.setEnabled(True)
        if result.get("cancelled"):
            self._status_lbl.setText("Cancelled.")
            return
        failed = result.get("failed", [])
        processed = result.get("processed", 0 if result.get("error") else n_selected - len(failed))
        msg = f"Done. {processed}/{n_selected} file(s) processed."
        if failed:
            msg += f" Failed: {', '.join(failed[:5])}{'...' if len(failed) > 5 else ''} (see log)."
        elif result.get("error"):
            msg = f"Failed: {result['error']}"
        self._status_lbl.setText(msg)

    def teardown(self):
        if self._preview_job is not None:
            self._preview_job.cancel()
            self._preview_job = None
