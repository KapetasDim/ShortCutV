"""Program viewer dock panel: plays back the composed/rendered timeline
(proxy or final), and hosts the Build Preview / Render Final Video actions."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import ffmpeg_ops
from ..logging_config import get_logger
from ..project import TimelineClip
from .dialogs import ask_save_filename
from .icons import icon
from .player import SharedPlayer
from .theme import COLORS
from .util import connect_button, run_on_ui
from .widgets import Scrubber, fmt_time

log = get_logger()


class ProgramViewerPanel(QWidget):
    composed_recomposed = Signal()  # a fresh proxy/final render just finished loading

    def __init__(self, preview_player: SharedPlayer, parent=None):
        super().__init__(parent)
        self.preview_player = preview_player
        self._rendering = False
        self._render_proc_holder: dict = {}
        # "proxy" | "final" | None -- which render (if any) currently owns
        # _render_proc_holder. Guards against a proxy Build and a final
        # Render running concurrently (they'd race on the same ffmpeg
        # progress/cancel state) and lets closeEvent find a live render to
        # kill.
        self._render_kind: Optional[str] = None
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
        if self._rendering:
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
        self._render_proc_holder["cancelled"] = True
        proc = self._render_proc_holder.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.exception("Program viewer: failed to terminate render process")
        self._rendering = False
        self._render_kind = None
        self._build_btn.setText(" Build Preview")
        self._build_btn.setProperty("variant", "violet")
        self._restyle(self._build_btn)
        self._status_lbl.setText("Cancelled.")

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
        out_path = self._tmp_dir / "proxy_preview.mp4"
        if proxy:
            # ffmpeg is about to overwrite this exact file; the preview
            # player may still have it open (paused, not unloaded) from the
            # last proxy render, which can make the overwrite fail or hand
            # mpv a half-written file on Windows -- fully release it first.
            self.preview_player.terminate()
        self._start_render(proxy, out_path)

    def _start_render(self, proxy: bool, out_path: Path):
        project = self._get_project()
        clips_snapshot = list(project.clips)
        canvas_w, canvas_h = project.canvas_w, project.canvas_h
        proxy_height = int(self._proxy_height_combo.currentText())

        self._progress.setValue(0)
        self._status_lbl.setText("Rendering proxy..." if proxy else "Rendering final video...")

        proc_holder: dict = {}
        self._render_proc_holder = proc_holder
        self._render_kind = "proxy" if proxy else "final"
        if proxy:
            self._rendering = True
            self._build_btn.setText(" Cancel")
            self._build_btn.setProperty("variant", "danger")
            self._restyle(self._build_btn)

        def on_progress(frac, _label):
            run_on_ui(lambda: self._progress.setValue(int(frac * 100)))

        text_overlays_snapshot = list(project.text_overlays)
        sound_clips_snapshot = list(project.sound_clips)

        def run():
            err = None
            try:
                ok = ffmpeg_ops.render_timeline(clips_snapshot, canvas_w, canvas_h, out_path,
                                                 proxy=proxy, proxy_height=proxy_height,
                                                 progress_cb=on_progress, proc_holder=proc_holder,
                                                 text_overlays=text_overlays_snapshot,
                                                 sound_clips=sound_clips_snapshot,
                                                 intro_type=project.intro_transition_type,
                                                 intro_duration=project.intro_transition_duration,
                                                 intro_direction=project.intro_transition_direction,
                                                 outro_type=project.outro_transition_type,
                                                 outro_duration=project.outro_transition_duration,
                                                 outro_direction=project.outro_transition_direction)
            except Exception as e:  # noqa: BLE001
                log.exception("Program viewer: render crashed before/during ffmpeg")
                ok, err = False, e

            def done():
                # A cancel-then-rebuild (or a second render kind starting
                # before this thread noticed) replaces _render_proc_holder
                # with a fresh dict for the new render -- if that happened,
                # this completion belongs to a superseded render and must not
                # touch UI state the new one already owns.
                if proc_holder is not self._render_proc_holder:
                    return
                if proxy:
                    self._rendering = False
                    self._build_btn.setText(" Build Preview")
                    self._build_btn.setProperty("variant", "violet")
                    self._restyle(self._build_btn)
                self._render_kind = None
                if err is not None:
                    self._status_lbl.setText(f"Render error: {err}")
                elif ok:
                    label = "(proxy)" if proxy else "(final)"
                    self._status_lbl.setText("Preview updated." if proxy else f"Saved: {out_path.name}")
                    self._composed_lbl.setText(f"{out_path.name} {label}")
                    self.has_composed_preview = True
                    self._scrubber.set_locked(False)
                    self.preview_player.load(out_path)
                    self.composed_recomposed.emit()
                elif not proc_holder.get("cancelled"):
                    self._status_lbl.setText("Render failed (see console).")
            run_on_ui(done)

        threading.Thread(target=run, daemon=True).start()

    def reset_for_new_project(self):
        self.has_composed_preview = False
        self._composed_lbl.setText("Not rendered yet")
        self._scrubber.set_locked(True)
