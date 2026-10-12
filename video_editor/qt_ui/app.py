from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDockWidget, QMainWindow, QMessageBox, QPlainTextEdit, QTabWidget

from .. import active_processes
from ..logging_config import get_logger
from ..project import SoundClip, TextOverlay, TimelineClip, TimelineProject, load_project, save_project
from .bin_panel import BinPanel
from .clip_trim_editor import ClipTrimEditor
from .dialogs import ask_open_filename, ask_save_filename
from .image_clip_editor import ImageClipEditor
from .inspector import InspectorPanel
from .job_queue import get_queue
from .log_bridge import attach_text_widget
from .metadata_worker import request_metadata
from .player import SharedPlayer
from .program_viewer import ProgramViewerPanel
from .scale import get_scale
from .sound_clip_editor import SoundClipEditor
from .source_viewer import BatchCropResizeDialog, SourceViewerPanel
from .text_overlay_editor import TextOverlayEditor
from .thumbnail_worker import request_thumbnail
from .timeline_widget import TimelineWidget
from .undo import ProjectUndoStack
from .util import connect_button, init_ui_marshalling

log = get_logger()

STATE_FILE = Path(__file__).parent.parent / ".state.json"
BUNDLED_DEFAULT_LAYOUT = Path(__file__).parent.parent.parent / "layout_1.vlayout"
DEFAULT_IMAGE_DURATION = 1.5

# Bake/proxy scratch dirs used by the program viewer and the sound/image/text
# editors. Each editor only cleans up its own last bake file, and only on a
# clean teardown -- a crash, or a bake superseded by a newer one before its
# file was replaced, leaves orphaned uuid-named .mp4s behind indefinitely.
_TEMP_SCRATCH_DIRS = (
    "video_editor_proxy", "video_editor_image_preview",
    "video_editor_sound_preview", "video_editor_text_preview",
)


def _sweep_temp_dirs():
    for dirname in _TEMP_SCRATCH_DIRS:
        d = Path(tempfile.gettempdir()) / dirname
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    log.exception("Startup: failed to remove stale temp file %s", f)


class _DockWidget(QDockWidget):
    """QDockWidget double-click-toggles floating by default (built into its
    own event() handling) -- disabled per explicit request: double-clicking
    a dock's title bar should do nothing. Dragging the title bar still
    floats/docks it normally; only the double-click shortcut is swallowed."""

    def event(self, event):
        if event.type() == QEvent.Type.MouseButtonDblClick:
            return True
        return super().event(event)


@dataclass
class AppState:
    crop_input_dir: Optional[Path] = None
    crop_output_dir: Optional[Path] = None
    clip_input_dir: Optional[Path] = None
    clip_output_dir: Optional[Path] = None

    @classmethod
    def load(cls) -> "AppState":
        if not STATE_FILE.exists():
            return cls()
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return cls(**{k: (Path(v) if v else None) for k, v in raw.items()})
        except Exception:
            log.exception("Failed to load saved app state, starting fresh")
            return cls()

    def save(self):
        try:
            raw = {k: (str(v) if v else None) for k, v in asdict(self).items()}
            STATE_FILE.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except OSError:
            log.exception("Failed to save app state to %s", STATE_FILE)


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        # Establish GUI-thread ownership of the run_on_ui marshaller before
        # anything can spawn a worker thread that calls it (render/ffmpeg
        # callbacks) -- otherwise queued UI callbacks, including mpv loads,
        # can end up executing on a worker thread and crash libmpv.
        init_ui_marshalling()
        self.setWindowTitle("ShortCutV")
        w, h = get_scale().app_window_geometry.split("x")
        self.resize(int(w), int(h))

        self.state_data = AppState.load()
        self.project = TimelineProject()
        self.player = SharedPlayer(self)                    # Bin/Source viewer preview
        self.preview_player = SharedPlayer(self, window_x_offset=480)  # Program (composed) viewer preview
        self.undo = ProjectUndoStack(apply=self._apply_project_snapshot)
        self.settings = QSettings("ShortCutV", "ShortCutV")
        _sweep_temp_dirs()
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_proxy"
        self._tmp_dir.mkdir(exist_ok=True)

        self._build_panels()
        self._build_docks()
        self._apply_default_layout()
        self._build_menu_and_toolbar()
        self._build_shortcuts()
        self._build_loading_indicator()
        self._wire_signals()

        self.player.set_embed_widget(self.source_viewer._preview_frame)
        self.player.set_tick_callback(self.source_viewer._on_tick)
        self.preview_player.set_embed_widget(self.program_viewer._preview_frame)
        self.preview_player.set_tick_callback(self.program_viewer._on_tick)
        self.program_viewer.bind(lambda: self.project, self._tmp_dir)

        self.bin_panel.set_folders(self.state_data.clip_input_dir, self.state_data.clip_output_dir)
        self._refresh_timeline_widget()

        if not self._restore_last_layout():
            self.showMaximized()
        else:
            self.show()
        # restoreState() (inside _restore_last_layout) re-applies whatever tab
        # position was saved from a previous run, undoing the setTabPosition
        # call in _build_docks -- so pin it again here, after restore.
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        log.info("App started")

    # -------------------------------------------------------------- panels --

    def _build_panels(self):
        self.bin_panel = BinPanel(self.state_data)
        self.source_viewer = SourceViewerPanel(self.player)
        self.program_viewer = ProgramViewerPanel(self.preview_player)
        # The Source viewer lives inside the Inspector (previewing/trimming a
        # Bin selection is itself a form of inspecting it) rather than in its
        # own dock -- the Inspector shows it by default and swaps in a
        # per-item editor whenever something is selected on the Timeline.
        self.inspector = InspectorPanel(self.source_viewer)
        self.timeline_widget = TimelineWidget(
            on_reorder=self._on_reorder, on_remove_clip=self._on_remove_clip,
            on_transition_change=self._on_transition_change, on_edit_clip=self._on_edit_clip,
            on_move_sound=self._on_sound_moved, on_move_text=self._on_text_moved,
            on_drop_bin_item=self._on_drop_bin_item)
        self.timeline_widget.selection_changed.connect(self._on_selection_changed)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setProperty("mono", True)
        self.log_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        attach_text_widget(self.log_text)

    def _build_docks(self):
        def make_dock(title, widget, object_name):
            dock = _DockWidget(title, self)
            dock.setObjectName(object_name)
            dock.setWidget(widget)
            dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                              | QDockWidget.DockWidgetFeature.DockWidgetFloatable
                              | QDockWidget.DockWidgetFeature.DockWidgetClosable)
            # Dragging a floating dock's title bar back into the window can
            # land it in a zero-size slot if the space it used to occupy was
            # reclaimed by its neighbors while it was floating, making it
            # appear to just vanish. Nudge it back to a visible size
            # whenever it stops floating.
            dock.topLevelChanged.connect(lambda floating, d=dock: self._on_dock_top_level_changed(d, floating))
            return dock

        self.bin_dock = make_dock("Bin", self.bin_panel, "dock_bin")
        self.program_dock = make_dock("Program Viewer", self.program_viewer, "dock_program")
        self.inspector_dock = make_dock("Inspector", self.inspector, "dock_inspector")
        self.timeline_dock = make_dock("Timeline", self.timeline_widget, "dock_timeline")
        self.log_dock = make_dock("Log", self.log_text, "dock_log")
        self.setDockNestingEnabled(True)
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)

    def _apply_default_layout(self):
        """(Re)arranges every dock into the default positions/sizes: the
        bundled layout_1.vlayout if it's present and loads cleanly, else the
        hardcoded arrangement below. Used both at startup (when there's no
        remembered last-session layout) and by View > Reset Layout."""
        if self._load_layout_file(BUNDLED_DEFAULT_LAYOUT):
            return
        self._apply_hardcoded_default_layout()

    def _apply_hardcoded_default_layout(self):
        """(Re)arranges every dock into the default positions/sizes. Split
        out from dock creation so "Reset Layout" can call this directly
        without rebuilding the panels themselves."""
        # Bottom corners belong to the left/right areas (Qt's default, made
        # explicit here) so the Bin/Inspector columns run the full window
        # height and only the central program-viewer column is split with
        # the Timeline below it -- otherwise the Timeline dock can end up
        # claiming the full window width along the bottom and squeezing
        # everything above it.
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)

        for dock in (self.bin_dock, self.program_dock, self.inspector_dock, self.timeline_dock, self.log_dock):
            dock.setFloating(False)
            dock.show()

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.bin_dock)
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.program_dock)
        self.splitDockWidget(self.program_dock, self.timeline_dock, Qt.Orientation.Vertical)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.tabifyDockWidget(self.bin_dock, self.log_dock)
        self.bin_dock.raise_()

        self.resizeDocks([self.bin_dock, self.program_dock, self.inspector_dock],
                          [280, 640, 480], Qt.Orientation.Horizontal)
        # The Timeline widget already caps its own height (its lanes have a
        # fixed pixel total), so giving it a generous nominal share here just
        # means the remainder reliably goes to the Program viewer above it.
        self.resizeDocks([self.program_dock, self.timeline_dock], [900, 260], Qt.Orientation.Vertical)

    def _on_dock_top_level_changed(self, dock: QDockWidget, floating: bool):
        if floating:
            return
        dock.show()
        dock.raise_()
        # Deferred: the re-dock geometry isn't final until after this signal
        # returns, so checking dock.width()/height() immediately would still
        # see its old (floating) size.
        QTimer.singleShot(0, lambda d=dock: self._ensure_dock_visible_size(d))

    def _ensure_dock_visible_size(self, dock: QDockWidget):
        if dock.isFloating() or not dock.isVisible():
            return
        if dock.width() < 40:
            self.resizeDocks([dock], [280], Qt.Orientation.Horizontal)
        if dock.height() < 40:
            self.resizeDocks([dock], [280], Qt.Orientation.Vertical)

    def _build_menu_and_toolbar(self):
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        new_action = file_menu.addAction("New Project")
        connect_button(new_action.triggered, "File > New Project", self._new_project)
        open_action = file_menu.addAction("Open Project...")
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        connect_button(open_action.triggered, "File > Open Project", self._load_project)
        save_action = file_menu.addAction("Save Project...")
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        connect_button(save_action.triggered, "File > Save Project", self._save_project)
        file_menu.addSeparator()
        render_action = file_menu.addAction("Render Final Video...")
        render_action.setShortcut("Ctrl+E")
        connect_button(render_action.triggered, "File > Render Final Video", self.program_viewer._render_final)
        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        connect_button(exit_action.triggered, "File > Exit", self.close)

        edit_menu = menu.addMenu("&Edit")
        self.undo_action = self.undo.qt_stack.createUndoAction(self, "Undo")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        # QUndoStack wires this action's undo() call internally when it's
        # created via createUndoAction() -- this second connection is just
        # for the click log, it doesn't replace or race the real one.
        connect_button(self.undo_action.triggered, "Edit > Undo", lambda: None)
        edit_menu.addAction(self.undo_action)
        self.redo_action = self.undo.qt_stack.createRedoAction(self, "Redo")
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        connect_button(self.redo_action.triggered, "Edit > Redo", lambda: None)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        delete_action = edit_menu.addAction("Delete Selected")
        delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        connect_button(delete_action.triggered, "Edit > Delete Selected", self._delete_selected)
        duplicate_action = edit_menu.addAction("Duplicate Selected")
        duplicate_action.setShortcut("Ctrl+D")
        connect_button(duplicate_action.triggered, "Edit > Duplicate Selected", self._duplicate_selected)

        view_menu = menu.addMenu("&View")
        for dock in (self.bin_dock, self.program_dock, self.inspector_dock, self.timeline_dock, self.log_dock):
            view_menu.addAction(dock.toggleViewAction())
        view_menu.addSeparator()
        save_layout_action = view_menu.addAction("Save Layout As...")
        connect_button(save_layout_action.triggered, "View > Save Layout As...", self._save_layout_to_file)
        load_layout_action = view_menu.addAction("Load Layout...")
        connect_button(load_layout_action.triggered, "View > Load Layout...", self._load_layout_from_file)
        reset_layout_action = view_menu.addAction("Reset Layout to Default")
        connect_button(reset_layout_action.triggered, "View > Reset Layout to Default", self._apply_default_layout)

        help_menu = menu.addMenu("&Help")
        about_action = help_menu.addAction("About")
        connect_button(about_action.triggered, "Help > About", self._show_about)

    def _build_loading_indicator(self):
        """A status-bar label reflecting the shared background-job queue
        (thumbnails/metadata/waveforms). Hidden when nothing is outstanding,
        so the user gets a clear "still loading" signal after dropping a large
        folder without a permanent chrome element the rest of the time."""
        from PySide6.QtWidgets import QLabel
        self._loading_lbl = QLabel("")
        self._loading_lbl.setProperty("muted", True)
        self.statusBar().addPermanentWidget(self._loading_lbl)
        get_queue().counts_changed.connect(self._on_jobs_changed)

    def _on_jobs_changed(self, active: int, queued: int):
        outstanding = active + queued
        if outstanding <= 0:
            self._loading_lbl.setText("")
        else:
            self._loading_lbl.setText(f"Loading {outstanding} item(s)...")

    def _build_shortcuts(self):
        space_action = QAction(self)
        space_action.setShortcut(Qt.Key.Key_Space)
        connect_button(space_action.triggered, "Shortcut: Space (play/pause)", self._toggle_active_playback)
        self.addAction(space_action)

    def _toggle_active_playback(self):
        if self.program_viewer.has_composed_preview:
            self.preview_player.play_pause()
        else:
            self.player.play_pause()

    def _show_about(self):
        QMessageBox.about(self, "ShortCutV",
                           "ShortCutV -- a modern, dockable timeline editor built on PySide6.")

    # ------------------------------------------------------------- signals --

    def _wire_signals(self):
        self.bin_panel.item_selected.connect(self._on_bin_item_selected)
        self.bin_panel.add_to_timeline_requested.connect(self._on_bin_add_requested)
        self.bin_panel.batch_crop_requested.connect(self._on_batch_crop_requested)
        self.source_viewer.add_to_timeline.connect(self._on_source_add_requested)
        self.program_viewer.composed_recomposed.connect(self._on_composed_recomposed)

    def _on_bin_item_selected(self, path: Path, kind: str):
        # Picking something in the Bin always shows the Source viewer in the
        # Inspector, interrupting whatever timeline-item editor was open.
        if self.timeline_widget.selected is not None:
            self.timeline_widget.selected = None
            self.timeline_widget.redraw()
        self.inspector.clear_editor()
        self.source_viewer.load(path, kind)

    def _on_bin_add_requested(self, path: Path, kind: str):
        if kind == "sound":
            self._add_sound(path)
            return
        self._add_clip(path, kind, None, None)

    def _on_source_add_requested(self, path: Path, kind: str, in_s: float, out_s: float):
        self._add_clip(path, kind, in_s, out_s)

    def _on_batch_crop_requested(self, paths: list):
        dialog = BatchCropResizeDialog(self.state_data, self)
        if self.bin_panel.input_dir is not None:
            dialog.preselect_input(self.bin_panel.input_dir)
        dialog.exec()

    # ---------------------------------------------------------- timeline ops --

    def _add_clip(self, path: Path, kind: str, in_s: Optional[float], out_s: Optional[float]):
        if kind == "image":
            try:
                with Image.open(path) as img:
                    width, height = img.size
            except Exception:
                log.exception("Workspace: failed to add %s to timeline (unreadable/corrupt source?)", path)
                self.program_viewer._status_lbl.setText(f"Could not add {path.name} (see log).")
                return
            clip = TimelineClip(source=str(path), in_s=0.0, out_s=DEFAULT_IMAGE_DURATION, is_image=True)
            self._finish_add_clip(path, clip, width, height)
            return

        # ffprobe runs off the job queue instead of blocking this (GUI)
        # thread -- a slow/corrupt file or a network drive used to freeze the
        # whole app for up to 20s on every add.
        def on_probe(_key, duration_s, width, height):
            if duration_s <= 0 and not width and not height:
                log.error("Workspace: failed to add %s to timeline (unreadable/corrupt source?)", path)
                self.program_viewer._status_lbl.setText(f"Could not add {path.name} (see log).")
                return
            clip = TimelineClip(source=str(path), in_s=in_s or 0.0, out_s=out_s or duration_s)
            self._finish_add_clip(path, clip, width, height)

        request_metadata(path, path, "video", on_probe)

    def _finish_add_clip(self, path: Path, clip: TimelineClip, width: int, height: int):
        is_first_clip = not self.project.clips
        self.undo.begin(self.project)
        self.project.clips.append(clip)
        if is_first_clip and width and height:
            width, height = width // 2 * 2, height // 2 * 2
            self.project.canvas_w, self.project.canvas_h = width, height
            log.info("Workspace: output canvas defaulted to %dx%d (first clip's resolution)", width, height)
        self.undo.commit(f"Add {path.name}", self.project)
        self._refresh_timeline_widget()
        self._request_thumbnail(len(self.project.clips) - 1)
        log.info("Workspace: added %s", path.name)

    def _add_sound(self, path: Path):
        # See _add_clip: probing off the job queue instead of blocking the
        # GUI thread.
        def on_probe(_key, duration_s, _width, _height):
            duration_s = max(0.05, duration_s) if duration_s > 0 else 5.0
            self.undo.begin(self.project)
            start_s = self.preview_player.current_pos_s() if self.program_viewer.has_composed_preview else 0.0
            self.project.sound_clips.append(SoundClip(source=str(path), start_s=start_s, clip_duration_s=duration_s))
            self.undo.commit(f"Add sound {path.name}", self.project)
            self._refresh_timeline_widget()
            log.info("Workspace: added sound %s @ %.2fs", path.name, start_s)

        request_metadata(path, path, "sound", on_probe)

    def _on_reorder(self, from_idx: int, to_idx: int):
        self.undo.begin(self.project)
        clip = self.project.clips.pop(from_idx)
        self.project.clips.insert(to_idx, clip)
        self.undo.commit("Reorder clip", self.project)
        # A reorder doesn't change any clip's content, so its thumbnail is
        # still valid -- just permute the cached thumbnails to follow the move
        # instead of re-running ffmpeg for every clip on the timeline.
        self.timeline_widget.move_thumbnail(from_idx, to_idx)
        self.timeline_widget.set_project_data(
            self.project.clips, self.project.text_overlays, self.project.sound_clips,
            preserve_thumbs=True)

    def _on_remove_clip(self, index: int):
        if not (0 <= index < len(self.project.clips)):
            return
        self.undo.begin(self.project)
        removed = self.project.clips.pop(index)
        self.undo.commit(f"Remove {Path(removed.source).name}", self.project)
        self._refresh_timeline_widget()
        for i in range(index, len(self.project.clips)):
            self._request_thumbnail(i)
        log.info("Workspace: removed %s from timeline", Path(removed.source).name)

    def _on_transition_change(self, clip: TimelineClip, transition_type, duration, direction="left"):
        self.undo.begin(self.project)
        clip.transition_type = transition_type
        clip.transition_duration = duration
        clip.transition_direction = direction
        self.undo.commit("Change transition", self.project)

    def _on_edit_clip(self, index: int):
        self.timeline_widget.selected = ("clip", index)
        self.timeline_widget.selection_changed.emit(self.timeline_widget.selected)
        self.timeline_widget.redraw()

    def _on_sound_moved(self, index: int):
        self.undo.commit("Move sound", self.project)
        self._refresh_timeline_widget()

    def _on_text_moved(self, index: int):
        self.undo.commit("Move text overlay", self.project)
        self._refresh_timeline_widget()

    def _on_drop_bin_item(self, raw: str):
        path_str, kind = raw.rsplit("|", 1)
        path = Path(path_str)
        if kind == "sound":
            self._add_sound(path)
        else:
            self._add_clip(path, kind, None, None)

    def _delete_selected(self):
        sel = self.timeline_widget.selected
        if sel is None:
            return
        kind, index = sel
        if kind == "clip":
            self._on_remove_clip(index)
        elif kind == "text":
            self.undo.begin(self.project)
            if 0 <= index < len(self.project.text_overlays):
                self.project.text_overlays.pop(index)
            self.undo.commit("Remove text overlay", self.project)
            self.timeline_widget.selected = None
            self._refresh_timeline_widget()
        elif kind == "sound":
            self.undo.begin(self.project)
            if 0 <= index < len(self.project.sound_clips):
                self.project.sound_clips.pop(index)
            self.undo.commit("Remove sound", self.project)
            self.timeline_widget.selected = None
            self._refresh_timeline_widget()

    def _duplicate_selected(self):
        sel = self.timeline_widget.selected
        if sel is None:
            return
        kind, index = sel
        if kind != "text":
            return
        if not (0 <= index < len(self.project.text_overlays)):
            return
        self.undo.begin(self.project)
        duplicate = copy.deepcopy(self.project.text_overlays[index])
        self.project.text_overlays.insert(index + 1, duplicate)
        self.undo.commit("Duplicate text overlay", self.project)
        self.timeline_widget.selected = ("text", index + 1)
        self._refresh_timeline_widget()
        self.timeline_widget.selection_changed.emit(self.timeline_widget.selected)

    # ----------------------------------------------------------- selection --

    def _on_selection_changed(self, selection):
        log.info("[UI] Timeline selection changed: %s", selection)
        try:
            self._on_selection_changed_inner(selection)
        except Exception:
            # Constructing an editor can hit ffmpeg/ffprobe (e.g. a corrupt
            # or slow-to-probe source file) -- letting that exception escape
            # this slot is exactly the kind of thing that used to take the
            # whole app down or leave it stuck. Log it and fall back to an
            # empty Inspector instead.
            log.exception("[UI] ERROR building editor for selection")
            self.inspector.clear_editor()

    def _on_selection_changed_inner(self, selection):
        if selection is None:
            self.inspector.clear()
            return
        kind, index = selection
        if kind == "clip":
            if not (0 <= index < len(self.project.clips)):
                return
            clip = self.project.clips[index]
            self.undo.begin(self.project)
            if clip.is_image:
                editor = ImageClipEditor(
                    self.inspector, clip, self.project.canvas_w, self.project.canvas_h,
                    on_change=lambda: self._on_inspector_clip_changed(index),
                    on_close=lambda: self._deselect())
            else:
                editor = ClipTrimEditor(
                    self.inspector, clip, on_change=lambda: self._on_inspector_clip_changed(index),
                    on_close=lambda: self._deselect())
            self.inspector.show_editor(editor, teardown=editor.teardown)
        elif kind == "text":
            if not (0 <= index < len(self.project.text_overlays)):
                return
            overlay = self.project.text_overlays[index]
            self.undo.begin(self.project)
            editor = TextOverlayEditor(
                self.inspector, self.project, self.preview_player,
                on_save=lambda o: self._on_inspector_text_saved(index, o),
                on_close=lambda: self._deselect(), existing=overlay)
            self.inspector.show_editor(editor, teardown=editor.teardown)
        elif kind == "sound":
            if not (0 <= index < len(self.project.sound_clips)):
                return
            sound = self.project.sound_clips[index]
            self.undo.begin(self.project)
            editor = SoundClipEditor(
                self.inspector, self.project, self.preview_player, sound.source,
                on_save=lambda s: self._on_inspector_sound_saved(index, s),
                on_close=lambda: self._deselect(), existing=sound)
            self.inspector.show_editor(editor, teardown=editor.teardown)

    def _deselect(self):
        self.timeline_widget.selected = None
        self.timeline_widget.selection_changed.emit(None)
        self.timeline_widget.redraw()

    def _on_inspector_clip_changed(self, index: int):
        self.undo.commit("Edit clip", self.project)
        self._refresh_timeline_widget()
        self._request_thumbnail(index)

    def _on_inspector_text_saved(self, index: int, overlay: TextOverlay):
        if 0 <= index < len(self.project.text_overlays):
            self.project.text_overlays[index] = overlay
        self.undo.commit("Edit text overlay", self.project)
        self._refresh_timeline_widget()

    def _on_inspector_sound_saved(self, index: int, sound: SoundClip):
        if 0 <= index < len(self.project.sound_clips):
            self.project.sound_clips[index] = sound
        self.undo.commit("Edit sound", self.project)
        self._refresh_timeline_widget()

    def _on_composed_recomposed(self):
        current = self.inspector._current_editor
        if isinstance(current, (TextOverlayEditor, SoundClipEditor)):
            current.invalidate_composed_preview()

    # -------------------------------------------------------------- helpers --

    def _refresh_timeline_widget(self):
        self.timeline_widget.set_project_data(
            self.project.clips, self.project.text_overlays, self.project.sound_clips)

    def _request_thumbnail(self, index: int):
        if index >= len(self.project.clips):
            return
        clip = self.project.clips[index]
        request_thumbnail(index, clip,
                           lambda i, data: self.timeline_widget.set_thumbnail(i, data, clip))

    # ---------------------------------------------------------- undo/redo --

    def _apply_project_snapshot(self, project: TimelineProject):
        self.project = project
        self.inspector.clear()
        self.timeline_widget.selected = None
        self._refresh_timeline_widget()
        for i in range(len(self.project.clips)):
            self._request_thumbnail(i)

    # ------------------------------------------------------------- projects --

    def _new_project(self):
        self.project = TimelineProject()
        self.undo.qt_stack.clear()
        self.inspector.clear()
        self.program_viewer.reset_for_new_project()
        self.timeline_widget.reset_waveform_state()
        self._refresh_timeline_widget()
        log.info("Workspace: new project started")

    def _save_project(self):
        log.info("Workspace: Save Project clicked, calling stop_active_work()...")
        self.program_viewer.stop_active_work()
        log.info("Workspace: stop_active_work() done, opening save dialog...")

        def on_path(path: Optional[Path]):
            log.info("Workspace: save dialog callback fired (path=%s)", path)
            self.program_viewer.resume_polling()
            if not path:
                return
            self.project.lib_input_dir = str(self.bin_panel.input_dir) if self.bin_panel.input_dir else None
            self.project.lib_output_dir = str(self.bin_panel.output_dir) if self.bin_panel.output_dir else None
            try:
                save_project(self.project, path)
                self.program_viewer._status_lbl.setText(f"Project saved: {path.name}")
            except Exception:
                log.exception("Workspace: save failed")
                self.program_viewer._status_lbl.setText("Save failed (see console).")

        ask_save_filename(self, on_path, title="Save project as...", defaultextension=".json",
                           filetypes=[("Project JSON", "*.json")])

    def _load_project(self):
        self.program_viewer.stop_active_work()

        def on_path(path: Optional[Path]):
            self.program_viewer.resume_polling()
            if not path:
                return
            try:
                loaded = load_project(path)
            except Exception:
                log.exception("Workspace: load failed")
                self.program_viewer._status_lbl.setText("Load failed (see console).")
                return
            self.project = loaded
            self.undo.qt_stack.clear()
            self.inspector.clear()
            if loaded.lib_input_dir or loaded.lib_output_dir:
                self.bin_panel.set_folders(
                    Path(loaded.lib_input_dir) if loaded.lib_input_dir else None,
                    Path(loaded.lib_output_dir) if loaded.lib_output_dir else None)
            self.program_viewer.reset_for_new_project()
            self.timeline_widget.reset_waveform_state()
            self._refresh_timeline_widget()
            for i in range(len(self.project.clips)):
                self._request_thumbnail(i)
            self.program_viewer._status_lbl.setText(f"Project loaded: {path.name}")

        ask_open_filename(self, on_path, title="Load project...", filetypes=[("Project JSON", "*.json")])

    # ----------------------------------------------------------------- layout --

    def _restore_last_layout(self) -> bool:
        """Restores the layout this window had when it was last closed, if
        any was saved. Returns whether one was found and applied."""
        geometry = self.settings.value("window/geometry")
        state = self.settings.value("window/state")
        if geometry is None or state is None:
            return False
        try:
            self.restoreGeometry(geometry)
            self.restoreState(state)
        except Exception:
            log.exception("Workspace: failed to restore last window layout")
            return False
        return True

    def _save_layout_to_file(self):
        def on_path(path: Optional[Path]):
            if not path:
                return
            try:
                data = {"geometry": bytes(self.saveGeometry()).hex(), "state": bytes(self.saveState()).hex()}
                path.write_text(json.dumps(data), encoding="utf-8")
                log.info("Workspace: layout saved to %s", path)
            except OSError:
                log.exception("Workspace: failed to save layout to %s", path)

        ask_save_filename(self, on_path, title="Save layout as...", defaultextension=".vlayout",
                           filetypes=[("ShortCutV layout", "*.vlayout")])

    def _load_layout_file(self, path: Path) -> bool:
        """Applies a .vlayout JSON file's geometry/state to this window.
        Returns whether it loaded successfully."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.restoreGeometry(bytes.fromhex(data["geometry"]))
            self.restoreState(bytes.fromhex(data["state"]))
            log.info("Workspace: layout loaded from %s", path)
            return True
        except Exception:
            log.exception("Workspace: failed to load layout from %s", path)
            return False

    def _load_layout_from_file(self):
        def on_path(path: Optional[Path]):
            if not path:
                return
            if not self._load_layout_file(path):
                QMessageBox.warning(self, "Load Layout", "Could not load that layout file (see log).")

        ask_open_filename(self, on_path, title="Load layout...", filetypes=[("ShortCutV layout", "*.vlayout")])

    # ------------------------------------------------------------------ close --

    def closeEvent(self, event):
        log.info("App closing")
        # Nothing else stops these on its own: an open Inspector editor still
        # owns its own mpv player + bake temp file (teardown only runs when
        # the editor is closed/replaced, never on app close), a live render's
        # ffmpeg child is a daemon-thread-owned subprocess that outlives the
        # thread if not told to stop, and Qt destroys still-running
        # QProcess job-queue entries out from under themselves otherwise.
        if self.program_viewer._render_kind is not None:
            self.program_viewer._cancel_render()
        self.inspector.clear()
        get_queue().shutdown()
        # Exports and batch crop/resize run in daemon threads with no
        # per-widget handle the way renders have -- this is the only place
        # left that can still reach and kill them.
        active_processes.kill_all()
        self.player.terminate()
        self.preview_player.terminate()
        self.state_data.clip_input_dir = self.bin_panel.input_dir
        self.state_data.clip_output_dir = self.bin_panel.output_dir
        self.state_data.save()
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        super().closeEvent(event)
