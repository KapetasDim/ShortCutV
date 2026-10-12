from __future__ import annotations

import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                                QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import ffmpeg_ops
from ..logging_config import get_logger
from ..project import SoundClip, TimelineProject
from .icons import icon
from .inspector_split import make_preview_splitter
from .metadata_worker import request_metadata
from .player import SharedPlayer
from .theme import COLORS
from .util import connect_button, run_on_ui
from .widgets import Scrubber

log = get_logger()


class SoundClipEditor(QWidget):
    """Sound clips have no cheap live-recompute preview (any real
    volume/speed/loop/fade processing needs ffmpeg), so unlike the image/text
    editors this only has one preview mode: bake."""

    def __init__(self, parent, project: TimelineProject, preview_player: SharedPlayer, source: str,
                 on_save: Callable[[SoundClip], None], on_close: Callable[[], None],
                 existing: Optional[SoundClip] = None):
        super().__init__(parent)

        self.project = project
        self.preview_player = preview_player
        self.source = source
        self.on_save = on_save
        self.on_close = on_close
        self._existing = existing
        self._baked = False
        self.player: Optional[SharedPlayer] = None
        self._last_bake_path: Optional[Path] = None
        self._duration_user_edited = False
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_sound_preview"
        self._tmp_dir.mkdir(exist_ok=True)

        self._build_ui()
        if existing is None:
            self._probe_default_duration()

    # ------------------------------------------------------------------ UI --
    # Stacked vertically (preview on top, controls scrolling below) rather
    # than the old side-by-side left-controls/right-preview split -- see
    # ImageClipEditor's _build_ui for why that split doesn't work inside the
    # narrow Inspector dock.

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Preview (top) and controls (bottom) share one draggable splitter.
        preview_container = QWidget()
        right_layout = QVBoxLayout(preview_container)
        right_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 4, 4, 4)
        scroll.setWidget(left)

        splitter = make_preview_splitter(preview_container, scroll, "inspector/split_sound_h")
        root.addWidget(splitter, 1)

        e = self._existing

        src_lbl = QLabel(f"Source: {Path(self.source).name}")
        src_lbl.setProperty("muted", True)
        left_layout.addWidget(src_lbl)

        def row(label, width=140):
            r = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(width)
            r.addWidget(lbl)
            left_layout.addLayout(r)
            return r

        time_row = row("Start on timeline (s):")
        start_default = f"{e.start_s:.2f}" if e else f"{self.preview_player.current_pos_s():.2f}"
        self._start_edit = QLineEdit(start_default)
        self._start_edit.setFixedWidth(80)
        time_row.addWidget(self._start_edit)
        use_current_btn = QPushButton("Use current preview time")
        use_current_btn.setProperty("variant", "flat")
        connect_button(use_current_btn.clicked, "Sound Clip > Use current preview time", self._use_current_time)
        time_row.addWidget(use_current_btn)
        time_row.addStretch(1)

        offset_row = row("Source offset (s):")
        self._clip_start_edit = QLineEdit(str(e.clip_start_s) if e else "0.0")
        self._clip_start_edit.setFixedWidth(80)
        offset_row.addWidget(self._clip_start_edit)
        offset_row.addStretch(1)

        seg_row = row("Segment length (s):")
        self._clip_duration_edit = QLineEdit(str(e.clip_duration_s) if e else "5.00")
        self._clip_duration_edit.setFixedWidth(80)
        if e is None:
            # Placeholder above gets replaced with the real source duration
            # once _probe_default_duration()'s async probe lands -- unless
            # the user has already typed their own value in the meantime.
            self._clip_duration_edit.textEdited.connect(lambda *_: setattr(self, "_duration_user_edited", True))
        seg_row.addWidget(self._clip_duration_edit)
        seg_row.addStretch(1)

        vol_row = row("Volume %:")
        self._volume_edit = QLineEdit(str(round((e.volume if e else 1.0) * 100)))
        self._volume_edit.setFixedWidth(80)
        vol_row.addWidget(self._volume_edit)
        vol_row.addStretch(1)

        speed_row = row("Speed/pitch %:")
        self._speed_edit = QLineEdit(str(round((e.speed if e else 1.0) * 100)))
        self._speed_edit.setFixedWidth(80)
        speed_row.addWidget(self._speed_edit)
        speed_row.addStretch(1)

        repeat_row = row("Repeat count:")
        self._repeat_edit = QLineEdit(str(e.repeat_count if e else 1))
        self._repeat_edit.setFixedWidth(60)
        repeat_row.addWidget(self._repeat_edit)
        self._repeat_infinite_check = QCheckBox("Loop until timeline ends")
        self._repeat_infinite_check.setChecked(e.repeat_infinite if e else False)
        self._repeat_infinite_check.stateChanged.connect(self._on_infinite_toggled)
        repeat_row.addWidget(self._repeat_infinite_check)
        repeat_row.addStretch(1)
        self._on_infinite_toggled()

        fade_row = row("Fade in/out (s):")
        self._fade_in_edit = QLineEdit(str(e.fade_in_s if e else 0.0))
        self._fade_in_edit.setFixedWidth(60)
        fade_row.addWidget(self._fade_in_edit)
        self._fade_out_edit = QLineEdit(str(e.fade_out_s if e else 0.0))
        self._fade_out_edit.setFixedWidth(60)
        fade_row.addWidget(self._fade_out_edit)
        fade_row.addStretch(1)

        for edit in (self._clip_start_edit, self._clip_duration_edit, self._volume_edit, self._speed_edit,
                     self._repeat_edit, self._fade_in_edit, self._fade_out_edit, self._start_edit):
            edit.textChanged.connect(self._on_field_change)

        actions = QHBoxLayout()
        add_btn = QPushButton(icon("check", "#06181c"), " Update Sound" if e else " Add to Timeline")
        add_btn.setProperty("variant", "primary")
        connect_button(add_btn.clicked, "Sound Clip > Add/Update", self._add)
        actions.addWidget(add_btn)
        actions.addStretch(1)
        close_btn = QPushButton("Close")
        connect_button(close_btn.clicked, "Sound Clip > Close", self._on_close_clicked)
        actions.addWidget(close_btn)
        left_layout.addLayout(actions)

        bake_row = QHBoxLayout()
        self._bake_btn = QPushButton(icon("export", "#100c2b"), " Bake Preview")
        self._bake_btn.setProperty("variant", "violet")
        connect_button(self._bake_btn.clicked, "Sound Clip > Bake Preview", self._bake_preview)
        bake_row.addWidget(self._bake_btn)
        self._clear_bake_btn = QPushButton("Clear Bake")
        self._clear_bake_btn.setEnabled(False)
        connect_button(self._clear_bake_btn.clicked, "Sound Clip > Clear Bake", self._clear_bake)
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
        self._bake_status_lbl = QLabel("Not baked -- click Bake Preview to hear it.")
        self._bake_status_lbl.setProperty("muted", True)
        bake_status_row.addWidget(self._bake_status_lbl)
        left_layout.addLayout(bake_status_row)
        left_layout.addStretch(1)

        self._empty_section = QWidget()
        self._empty_section.setMinimumHeight(100)
        empty_layout = QVBoxLayout(self._empty_section)
        empty_lbl = QLabel("(nothing baked yet)")
        empty_lbl.setProperty("muted", True)
        empty_layout.addStretch(1)
        empty_layout.addWidget(empty_lbl, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(1)
        right_layout.addWidget(self._empty_section, 1)

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
        connect_button(self._baked_play_btn.clicked, "Sound Clip > Play/Pause (baked)", self._toggle_baked_play)
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
        right_layout.addWidget(self._baked_section, 1)
        self._baked_section.hide()

    def _probe_default_duration(self):
        path = Path(self.source)
        if not path.exists():
            return

        def on_probe(_key, duration_s, _width, _height):
            if self._duration_user_edited or duration_s <= 0:
                return
            self._clip_duration_edit.setText(f"{max(0.05, duration_s):.2f}")

        request_metadata(self.source, path, "sound", on_probe)

    def _on_infinite_toggled(self, *_args):
        self._repeat_edit.setEnabled(not self._repeat_infinite_check.isChecked())
        self._on_field_change()

    def _use_current_time(self):
        self._start_edit.setText(f"{self.preview_player.current_pos_s():.2f}")

    # -------------------------------------------------------------- build --

    def _build_sound(self) -> Optional[SoundClip]:
        try:
            return SoundClip(
                source=self.source,
                start_s=max(0.0, float(self._start_edit.text() or 0)),
                clip_start_s=max(0.0, float(self._clip_start_edit.text() or 0)),
                clip_duration_s=max(0.05, float(self._clip_duration_edit.text() or 0.05)),
                volume=max(0.0, min(2.0, float(self._volume_edit.text() or 0) / 100.0)),
                speed=max(0.1, min(4.0, float(self._speed_edit.text() or 100) / 100.0)),
                repeat_count=max(1, int(float(self._repeat_edit.text() or 1))),
                repeat_infinite=self._repeat_infinite_check.isChecked(),
                fade_in_s=max(0.0, float(self._fade_in_edit.text() or 0)),
                fade_out_s=max(0.0, float(self._fade_out_edit.text() or 0)),
            )
        except ValueError:
            return None

    def invalidate_composed_preview(self):
        if self._baked:
            self._show_empty_section()
        self._bake_status_lbl.setText("Timeline preview changed -- re-bake to update.")

    def _on_field_change(self, *_args):
        if self._baked:
            self._show_empty_section()
            self._bake_status_lbl.setText("Settings changed -- re-bake to update.")

    # ---------------------------------------------------------------- bake --

    def _bake_preview(self):
        sound = self._build_sound()
        source_path = self.preview_player.current_path
        if sound is None or source_path is None:
            self._bake_status_lbl.setText("Compose the timeline preview first, then bake.")
            return

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
                ok = ffmpeg_ops.render_sound_clip_preview(
                    source_path, sound, out_path, preview_height=preview_height, progress_cb=on_progress)
            except Exception:
                log.exception("Sound clip editor: bake failed for %s", self.source)
                ok = False
            run_on_ui(lambda: self._on_bake_done(ok, out_path))

        threading.Thread(target=run, daemon=True).start()

    def _on_bake_done(self, ok: bool, out_path: Path):
        self._bake_btn.setEnabled(True)
        self._bake_btn.setText(" Bake Preview")
        if not ok:
            self._bake_status_lbl.setText("Bake failed (nothing to play at this position, or see console).")
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
        log.info("Sound clip editor: baked preview for %s", Path(self.source).name)

    @staticmethod
    def _delete_bake_file(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.exception("Sound clip editor: failed to delete stale bake file %s", path)

    def _show_baked_section(self):
        self._baked = True
        self._clear_bake_btn.setEnabled(True)
        self._empty_section.hide()
        self._baked_section.show()

    def _show_empty_section(self):
        self._baked = False
        self._clear_bake_btn.setEnabled(False)
        self._baked_section.hide()
        self._empty_section.show()

    def _clear_bake(self):
        # Actually release the mpv instance and delete the bake file --
        # pausing alone (the old behavior) kept both alive despite "Clear
        # Bake" implying the file is released. _on_bake_done() already
        # handles self.player being None by recreating it on the next bake.
        if self.player is not None:
            self.player.terminate()
            self.player = None
        if self._last_bake_path is not None:
            self._delete_bake_file(self._last_bake_path)
            self._last_bake_path = None
        self._show_empty_section()
        self._bake_status_lbl.setText("Not baked -- click Bake Preview to hear it.")

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
        sound = self._build_sound()
        if sound is None:
            return
        self.on_save(sound)
        verb = "updated" if self._existing else "added"
        log.info("Sound clip %s: '%s' @ %.2fs", verb, Path(sound.source).name, sound.start_s)
        self.teardown()
        self.on_close()

    # --------------------------------------------------------------- close --

    def _on_close_clicked(self):
        self.teardown()
        self.on_close()

    def teardown(self):
        if self.player is not None:
            self.player.terminate()
            self.player = None
        if self._last_bake_path is not None:
            self._delete_bake_file(self._last_bake_path)
            self._last_bake_path = None
