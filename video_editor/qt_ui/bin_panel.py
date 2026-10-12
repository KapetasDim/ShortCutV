"""Bin (media library) dock panel: browses the two configured source folders
plus anything dropped in directly from Explorer, lets you preview a clip in
the Source viewer, drag it onto the Timeline, or batch-crop/resize a
selection. A sortable detail list (name/type/duration/resolution/source)
rather than a plain filename list, so it stays useful once a folder has more
than a handful of files and the panel is narrow."""
from __future__ import annotations

import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPushButton,
                                QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..logging_config import get_logger
from .crop_resize_tab import IMAGE_EXTS
from .dialogs import ask_directory
from .icons import icon
from .metadata_worker import request_metadata
from .theme import COLORS
from .timeline_widget import BIN_ITEM_MIME
from .util import connect_button

log = get_logger()

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
VIDEO_EXTS = {".mp4"}
_ICON_BY_KIND = {"video": "scissors", "image": "image", "sound": "sound"}
COL_NAME, COL_TYPE, COL_DURATION, COL_RESOLUTION, COL_SOURCE = range(5)

# Keyed by (str(path), mtime_ns) -> (duration_s, width, height), oldest
# access first. refresh() runs on every folder pick, external drop, manual
# refresh, and twice at startup -- without this, each one re-probes every
# file with ffprobe even though nothing changed, piling up pointless
# subprocess launches ahead of whatever probe the user's current action
# actually needs answered. Capped LRU-style like the probe/waveform caches;
# a failed probe (0, 0, 0) is deliberately never cached, so a transient
# failure ("--"/"--" in the Bin columns) gets retried on the next refresh
# instead of sticking until the file's mtime happens to change.
_METADATA_CACHE_MAX = 2000
_METADATA_CACHE: "OrderedDict[tuple[str, int], tuple[float, int, int]]" = OrderedDict()


def _metadata_cache_key(path: Path) -> Optional[tuple[str, int]]:
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None


class BinPanel(QWidget):
    item_selected = Signal(Path, str)             # path, kind ("video"|"image"|"sound")
    add_to_timeline_requested = Signal(Path, str)
    batch_crop_requested = Signal(list)            # list[Path]

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._input_dir: Optional[Path] = None
        self._output_dir: Optional[Path] = None
        self._external_paths: list[Path] = []
        self._items: list[tuple[Path, str, str]] = []  # (path, kind, tag)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        folders = QHBoxLayout()
        self._in_lbl = QLineEdit("Input: (none)")
        self._in_lbl.setReadOnly(True)
        folders.addWidget(self._in_lbl, 1)
        browse_in = QPushButton(icon("folder", COLORS["text"]), "")
        browse_in.setToolTip("Browse input folder")
        connect_button(browse_in.clicked, "Bin > Browse input folder", self._pick_input)
        folders.addWidget(browse_in)
        layout.addLayout(folders)

        folders2 = QHBoxLayout()
        self._out_lbl = QLineEdit("Output: (none)")
        self._out_lbl.setReadOnly(True)
        folders2.addWidget(self._out_lbl, 1)
        browse_out = QPushButton(icon("folder", COLORS["text"]), "")
        browse_out.setToolTip("Browse output folder")
        connect_button(browse_out.clicked, "Bin > Browse output folder", self._pick_output)
        folders2.addWidget(browse_out)
        layout.addLayout(folders2)

        header = QHBoxLayout()
        hint = QLabel("Drag onto the timeline, or drop files here to import")
        hint.setProperty("muted", True)
        header.addWidget(hint)
        header.addStretch(1)
        refresh_btn = QPushButton(icon("refresh", COLORS["text"]), "")
        refresh_btn.setToolTip("Refresh")
        connect_button(refresh_btn.clicked, "Bin > Refresh", self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        self._tree = _BinTree(self)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self._tree, 1)

        add_btn = QPushButton(icon("plus", "#06181c"), " Add selected -> Timeline")
        add_btn.setProperty("variant", "primary")
        connect_button(add_btn.clicked, "Bin > Add selected -> Timeline", self._add_selected_to_timeline)
        layout.addWidget(add_btn)

    # -------------------------------------------------------------- folders --

    def _pick_input(self):
        default = str(self.state.clip_input_dir) if self.state.clip_input_dir else ""

        def done(d):
            if not d:
                return
            self._input_dir = d
            self._in_lbl.setText(f"Input: {d}")
            self.state.clip_input_dir = d
            self.refresh()

        ask_directory(self, done, title="Select input folder", initialdir=default)

    def _pick_output(self):
        default = str(self.state.clip_output_dir) if self.state.clip_output_dir else ""

        def done(d):
            if not d:
                return
            self._output_dir = d
            self._out_lbl.setText(f"Output: {d}")
            self.state.clip_output_dir = d
            self.refresh()

        ask_directory(self, done, title="Select output folder", initialdir=default)

    def set_folders(self, input_dir: Optional[Path], output_dir: Optional[Path]):
        if input_dir is not None:
            self._input_dir = input_dir
            self._in_lbl.setText(f"Input: {input_dir}")
        if output_dir is not None:
            self._output_dir = output_dir
            self._out_lbl.setText(f"Output: {output_dir}")
        self.refresh()

    @property
    def input_dir(self):
        return self._input_dir

    @property
    def output_dir(self):
        return self._output_dir

    # ---------------------------------------------------------------- items --

    def import_external_files(self, paths: list[Path]):
        for p in paths:
            if p.is_file() and p not in self._external_paths:
                self._external_paths.append(p)
        self.refresh()
        log.info("Bin: imported %d external file(s)", len(paths))

    def refresh(self):
        self._items = []
        for tag, d in (("in", self._input_dir), ("out", self._output_dir)):
            if d and d.exists():
                files = list(d.iterdir())
                self._collect(files, tag)
        self._collect(self._external_paths, "ext")

        self._tree.setSortingEnabled(False)
        self._tree.clear()
        for path, kind, tag in self._items:
            item = QTreeWidgetItem([path.name, kind, "...", "...", tag])
            item.setIcon(COL_NAME, icon(_ICON_BY_KIND.get(kind, "image"), COLORS["text"]))
            item.setToolTip(COL_NAME, str(path))
            item.setData(0, Qt.ItemDataRole.UserRole, (path, kind))
            self._tree.addTopLevelItem(item)
            self._request_item_metadata(item, path, kind)
        self._tree.setSortingEnabled(True)
        log.info("Bin: %d items", len(self._items))

    def _request_item_metadata(self, item: QTreeWidgetItem, path: Path, kind: str):
        cache_key = _metadata_cache_key(path)
        cached = _METADATA_CACHE.get(cache_key) if cache_key is not None else None
        if cached is not None:
            _METADATA_CACHE.move_to_end(cache_key)
            self._on_metadata_ready(item, *cached)
            return

        def on_done(_item: QTreeWidgetItem, duration_s: float, width: int, height: int):
            if cache_key is not None and (duration_s > 0 or width or height):
                _METADATA_CACHE[cache_key] = (duration_s, width, height)
                _METADATA_CACHE.move_to_end(cache_key)
                while len(_METADATA_CACHE) > _METADATA_CACHE_MAX:
                    _METADATA_CACHE.popitem(last=False)
            self._on_metadata_ready(_item, duration_s, width, height)

        # The item itself (not a row index) is the key -- sorting can
        # reorder rows any time after this, including while a probe for
        # an earlier-added item is still in flight.
        request_metadata(item, path, kind, on_done)

    @staticmethod
    def _on_metadata_ready(item: QTreeWidgetItem, duration_s: float, width: int, height: int):
        try:
            item.setText(COL_DURATION, _fmt_duration(duration_s) if duration_s > 0 else "--")
            item.setText(COL_RESOLUTION, f"{width}x{height}" if width and height else "--")
        except RuntimeError:
            pass  # item's row was cleared (e.g. Bin refreshed) before its probe returned

    def _collect(self, files: list[Path], tag: str):
        videos = sorted(p for p in files if p.suffix.lower() in VIDEO_EXTS)
        images = sorted(p for p in files if p.suffix.lower() in IMAGE_EXTS)
        sounds = sorted(p for p in files if p.suffix.lower() in AUDIO_EXTS)
        for f in videos:
            self._items.append((f, "video", tag))
        for f in images:
            self._items.append((f, "image", tag))
        for f in sounds:
            self._items.append((f, "sound", tag))

    def _on_selection_changed(self):
        item = self._tree.currentItem()
        if item is None:
            return
        path, kind = item.data(0, Qt.ItemDataRole.UserRole)
        self.item_selected.emit(path, kind)

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int):
        path, kind = item.data(0, Qt.ItemDataRole.UserRole)
        self.add_to_timeline_requested.emit(path, kind)

    def _add_selected_to_timeline(self):
        item = self._tree.currentItem()
        if item is None:
            return
        path, kind = item.data(0, Qt.ItemDataRole.UserRole)
        self.add_to_timeline_requested.emit(path, kind)

    def selected_paths(self) -> list[Path]:
        return [it.data(0, Qt.ItemDataRole.UserRole)[0] for it in self._tree.selectedItems()]


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class _BinTree(QTreeWidget):
    def __init__(self, owner: BinPanel):
        super().__init__()
        self._owner = owner
        self.setColumnCount(5)
        self.setHeaderLabels(["Name", "Type", "Duration", "Resolution", "Source"])
        header = self.header()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col in (COL_TYPE, COL_DURATION, COL_RESOLUTION, COL_SOURCE):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        add_action = menu.addAction("Add to Timeline")
        preview_action = menu.addAction("Preview in Source viewer")
        menu.addSeparator()
        batch_action = menu.addAction("Batch Crop/Resize selected...")
        menu.addSeparator()
        reveal_action = menu.addAction("Reveal in Explorer")
        chosen = menu.exec(self.mapToGlobal(pos))
        path, kind = item.data(0, Qt.ItemDataRole.UserRole)
        if chosen == add_action:
            self._owner.add_to_timeline_requested.emit(path, kind)
        elif chosen == preview_action:
            self._owner.item_selected.emit(path, kind)
        elif chosen == batch_action:
            self._owner.batch_crop_requested.emit(self._owner.selected_paths())
        elif chosen == reveal_action:
            subprocess.Popen(["explorer", "/select,", str(path)])

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if item is None:
            return
        path, kind = item.data(0, Qt.ItemDataRole.UserRole)
        mime = QMimeData()
        mime.setData(BIN_ITEM_MIME, f"{path}|{kind}".encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            self._owner.import_external_files(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)
