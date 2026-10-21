"""Program viewer dock panel: plays back the composed/rendered timeline
(proxy or final), and hosts the Build Preview / Render Final Video actions."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from ..logging_config import get_logger
from ..project import project_to_dict
from . import media_info
from .dialogs import ask_save_filename
from .icons import icon
from .player import SharedPlayer
from .theme import COLORS
from .util import connect_button
from .widgets import Scrubber, fmt_time
from .worker_client import WorkerJob

log = get_logger()


class ProgramViewerPanel(QWidget):
    composed_recomposed = Signal()  # a fresh proxy/final render just finished loading

    def __init__(self, preview_player: SharedPlayer, parent=None):
        super().__init__(parent)
        self.preview_player = preview_player
        self._job: Optional[WorkerJob] = None
        # "proxy" | "final" | None -- which render (if any) is in flight (or,
        # for "final", waiting on its save dialog). Guards against a proxy
        # Build and a final Render running concurrently.
        self._render_kind: Optional[str] = None
        self._proxy_path: Optional[Path] = None  # currently loaded proxy file
        self.has_composed_preview = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        hint = QLabel("Not rendered yet")
        hint.setProperty("muted", True)
        self._composed_lbl = hint
        layout.addWidget(hint)

        self._preview_frame = QFrame()
        self._preview_frame.setStyleSheet("background-color: black;")
        self._preview_frame.setMinimumHeight(280)
        layout.addWidget(self._preview_frame, 1)

        transport = QHBoxLayout()
        self._play_btn = QPushButton(icon("play", COLORS["text"]), " Play")
        connect_button(self._play_btn.clicked, "Program Viewer > Play/Pause", self._toggle_play)
        transport.addWidget(self._play_btn)
        self._time_lbl = QLabel("--:--:--.--- / --:--:--.---")
        self._time_lbl.setProperty("mono", True)
        transport.addWidget(self._time_lbl)
        transport.addStretch(1)
        layout.addLayout(transport)

        self._scrubber = Scrubber()
        self._scrubber.add_seek_callback(self._on_seek)
        self._scrubber.set_locked(True)
        layout.addWidget(self._scrubber)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("Proxy quality:"))
        self._proxy_height_combo = QComboBox()
        self._proxy_height_combo.addItems(["720", "480", "360", "270"])
        self._proxy_height_combo.setCurrentText("270")
        actions.addWidget(self._proxy_height_combo)
        self._build_btn = QPushButton(icon("play", "#100c2b"), " Build Preview")
        self._build_btn.setProperty("variant", "violet")
        connect_button(self._build_btn.clicked, "Program Viewer > Build Preview", self._on_build_clicked)
        actions.addWidget(self._build_btn)
        actions.addStretch(1)
        self._render_btn = QPushButton(icon("export", "#06181c"), " Render Final Video")
        self._render_btn.setProperty("variant", "primary")
        connect_button(self._render_btn.clicked, "Program Viewer > Render Final Video", self._render_final)
        actions.addWidget(self._render_btn)
        layout.addLayout(actions)

        prog = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        prog.addWidget(self._progress, 1)
        self._status_lbl = QLabel("")
        self._status_lbl.setProperty("muted", True)
        prog.addWidget(self._status_lbl)
        layout.addLayout(prog)

        self._get_project = None
        self._tmp_dir = None

    def bind(self, get_project, tmp_dir: Path):
        """`get_project` is a zero-arg callable returning the live
        TimelineProject -- late-bound since the workspace owns it and may
        swap it wholesale on undo/redo/load."""
        self._get_project = get_project
        self._tmp_dir = tmp_dir

    def activate(self):
        self.preview_player.set_embed_widget(self._preview_frame)
        self.preview_player.set_tick_callback(self._on_tick)

    def deactivate(self):
        self.preview_player.set_tick_callback(None)

    def stop_active_work(self):
        if self._render_kind == "proxy":
            self._cancel_render()
        self.preview_player.pause()
        self.preview_player.suspend_polling()

    def resume_polling(self):
        self.preview_player.resume_polling()

    def _on_tick(self, pos_frac, duration_s, paused):
        self._scrubber.set_playhead(pos_frac)
        self._play_btn.setIcon(icon("pause" if not paused else "play", COLORS["text"]))
        self._play_btn.setText(" Pause" if not paused else " Play")
        self._time_lbl.setText(f"{fmt_time(pos_frac * duration_s * 1000)} / {fmt_time(duration_s * 1000)}")

    def _toggle_play(self):
        self.preview_player.play_pause()

    def _on_seek(self, frac: float):
        self.preview_player.seek(frac)
        d = self.preview_player.duration_s
        if d > 0:
            self._time_lbl.setText(f"{fmt_time(frac * d * 1000)} / {fmt_time(d * 1000)}")

    def _on_build_clicked(self):
        if self._render_kind == "proxy":
            self._cancel_render()
            return
        if self._render_kind == "final":
            self._status_lbl.setText("A final render is already in progress.")
            return
        self._render(proxy=True)

    def _cancel_render(self):
        job, self._job = self._job, None
        if job is not None:
            job.cancel()
        self._render_kind = None
        self._set_build_button(building=False)
        self._status_lbl.setText("Cancelled.")

    def _set_build_button(self, building: bool):
        self._build_btn.setText(" Cancel" if building else " Build Preview")
        self._build_btn.setProperty("variant", "danger" if building else "violet")
        self._restyle(self._build_btn)

    @staticmethod
    def _restyle(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _render_final(self):
        if self._render_kind is not None:
            self._status_lbl.setText(
                "Cancel the current build first." if self._render_kind == "proxy"
                else "A final render is already in progress.")
            return
        project = self._get_project()
        if not project.clips:
            self._status_lbl.setText("Timeline is empty.")
            return
        # Claimed immediately (not just once the render actually starts) so a
        # second Render-Final click can't open a second save dialog, or start
        # a proxy Build, while this one is still waiting on the file picker.
        self._render_kind = "final"
        self.stop_active_work()

        def done(out_path: Optional[Path]):
            self.resume_polling()
            if not out_path:
                self._render_kind = None
                return
            self._start_render(False, out_path)

        ask_save_filename(self, done, title="Render final video as...",
                           defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])

    def _render(self, proxy: bool):
        project = self._get_project()
        if not project.clips:
            self._status_lbl.setText("Timeline is empty.")
            return
        # A fresh file per build: the preview player can keep the previous
        # proxy open (and keep playing it) while the new one renders, and
        # nothing ever has to overwrite a file mpv still holds open.
        self._start_render(proxy, self._tmp_dir / f"proxy_{uuid.uuid4().hex[:12]}.mp4")

    def _start_render(self, proxy: bool, out_path: Path):
        project = self._get_project()
        # project_to_dict is a full deep copy: edits made while the render is
        # running can't leak into it half-applied.
        params = {
            "project": project_to_dict(project),
            "output": str(out_path),
            "proxy": proxy,
            "proxy_height": int(self._proxy_height_combo.currentText()),
            "probe_hints": media_info.probe_hints(c.source for c in project.clips if not c.is_image),
        }

        self._progress.setValue(0)
        self._status_lbl.setText("Rendering proxy..." if proxy else "Rendering final video...")
        self._render_kind = "proxy" if proxy else "final"
        if proxy:
            self._set_build_button(building=True)

        job = WorkerJob("render_timeline", params, parent=self)
        self._job = job
        job.progress.connect(lambda frac, msg: self._on_render_progress(job, frac, msg))
        job.finished.connect(lambda result: self._on_render_done(job, proxy, out_path, result))
        job.start()

    def _on_render_progress(self, job: WorkerJob, frac: float, msg: str):
        if job is self._job:
            self._progress.setValue(int(frac * 100))
            if msg:
                self._status_lbl.setText(msg)

    def _on_render_done(self, job: WorkerJob, proxy: bool, out_path: Path, result: dict):
        if result.get("cancelled") or not result.get("ok"):
            # Never leave a truncated output file behind.
            out_path.unlink(missing_ok=True)
        # A cancel-then-rebuild replaces self._job; a superseded render's
        # completion must not touch UI state the new one owns.
        if job is not self._job:
            return
        self._job = None
        if proxy:
            self._set_build_button(building=False)
        self._render_kind = None
        if result.get("cancelled"):
            return
        if not result.get("ok"):
            err = result.get("error")
            self._status_lbl.setText(f"Render error: {err}" if err else "Render failed (see log).")
            return
        self._progress.setValue(100)
        label = "(proxy)" if proxy else "(final)"
        self._status_lbl.setText("Preview updated." if proxy else f"Saved: {out_path.name}")
        self._composed_lbl.setText(f"{out_path.name} {label}")
        self.has_composed_preview = True
        self._scrubber.set_locked(False)
        previous_proxy = self._proxy_path
        self.preview_player.load(out_path)
        if proxy:
            self._proxy_path = out_path
            if previous_proxy is not None and previous_proxy != out_path:
                try:
                    previous_proxy.unlink(missing_ok=True)
                except OSError:
                    # Still open (e.g. an editor bake is reading it) -- the
                    # startup temp sweep removes it next launch.
                    log.info("Program viewer: old proxy %s still in use, left for startup sweep",
                             previous_proxy.name)
        self.composed_recomposed.emit()

    def reset_for_new_project(self):
        self.has_composed_preview = False
        self._composed_lbl.setText("Not rendered yet")
        self._scrubber.set_locked(True)
