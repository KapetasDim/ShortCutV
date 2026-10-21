"""Unified multi-lane timeline: a video track (sequential clip blocks, drag to
reorder), a transitions lane (fixed-width, click-only boxes centered on each
cut), a text-overlay lane (time-positioned, click to select), and an audio
track (time-positioned sound clips with rendered waveforms, drag to move).
Supports zoom (Ctrl+scroll), snap-while-dragging, and drop-to-add from the Bin
panel.

Item-based scene: rather than clearing and rebuilding the whole
QGraphicsScene on every update (which used to fire on every playback tick and
every drag-move -- the two hot paths), the scene keeps persistent items:

  * the playhead is one line + one arrow that are *moved*, never recreated, so
    playback just calls setLine()/setPos() ~12x/sec instead of rebuilding
    hundreds of items;
  * each lane (ruler/track backgrounds, video clips, text blocks, sound
    blocks) is rebuilt only when its own data actually changes;
  * text/sound blocks are QGraphicsItemGroups, so dragging one just setPos()s
    that single group instead of touching the rest of the scene.

Clip blocks sit at their position in the *rendered* output (timing.clip_layout,
the same function the renderer uses): a transition overlaps the next clip
onto the previous one's tail, exactly as xfade does. Text and sound placed
against a clip here land on that clip in the render.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QMouseEvent, QPainterPath,
                            QPen, QWheelEvent)
from PySide6.QtWidgets import (QComboBox, QGraphicsItemGroup, QGraphicsScene, QGraphicsView, QLineEdit,
                               QVBoxLayout, QWidget)

from ..logging_config import get_logger
from ..project import DEFAULT_TRANSITION_DURATION, SoundClip, TextOverlay, TimelineClip
from ..timing import clip_layout
from . import waveform
from .job_queue import cancel_group, submit
from .theme import COLORS
from .thumbnail_worker import GROUP as THUMB_GROUP
from .thumbnail_worker import request_thumbnail
from .widgets import bytes_to_image, pil_to_qpixmap

log = get_logger()

TRANSITION_LABELS = {
    "crossfade": "Crossfade", "fade_black": "Fade to Black", "wipe": "Wipe", "slide": "Slide", "cut": "Hard Cut",
}
TRANSITION_KEYS_BY_LABEL = {v: k for k, v in TRANSITION_LABELS.items()}

TOP_PAD = 4          # small gap above the video track (the time ruler was removed)
VIDEO_TRACK_H = 72
TRANSITION_TRACK_H = 22
TEXT_TRACK_H = 22
AUDIO_TRACK_H = 48
TRACK_GAP = 4
BOTTOM_PAD = 16     # breathing room below the last lane, so it isn't clipped flush against the container edge
LABEL_COL_W = 74
TRANSITION_BOX_W = 60  # fixed on-screen width, independent of zoom/duration -- see _rebuild_transitions
SNAP_PX = 8          # snap threshold, in view pixels

THUMB_CACHE_MAX = 500
WAVEFORM_GROUP = "waveforms"

BIN_ITEM_MIME = "application/x-video-editor-bin-item"

_Z_STATIC = 0
_Z_CONTENT = 1


class TimelineWidget(QWidget):
    selection_changed = Signal(object)  # ("clip"|"text"|"sound", index) or None

    def __init__(self, on_reorder: Callable, on_remove_clip: Callable, on_transition_change: Callable,
                 on_edit_clip: Callable, on_move_sound: Callable, on_move_text: Callable,
                 on_drop_bin_item: Callable, parent=None):
        super().__init__(parent)
        self.clips: list[TimelineClip] = []
        self.text_overlays: list[TextOverlay] = []
        self.sound_clips: list[SoundClip] = []
        self.pixels_per_second = 40.0
        self.selected = None  # ("clip"|"text"|"sound", index) or None

        self._on_reorder = on_reorder
        self._on_remove_clip = on_remove_clip
        self._on_transition_change = on_transition_change
        self._on_edit_clip = on_edit_clip
        self._on_move_sound = on_move_sound
        self._on_move_text = on_move_text
        self._on_drop_bin_item = on_drop_bin_item

        # Thumbnails keyed by clip *content* (source identity + trim), not by
        # timeline index: reorder/remove/undo never invalidate them, and a
        # clip that comes back via undo finds its thumbnail still cached.
        self._thumb_cache: "OrderedDict[tuple, object]" = OrderedDict()
        self._thumb_pending: set[tuple] = set()
        self._clips_rebuild_scheduled = False
        # Keyed by waveform.file_key. _done records files that finished
        # (success OR no audio/failure) so a silent file isn't re-requested on
        # every redraw.
        self._waveform_pending: set[tuple] = set()
        self._waveform_done: set[tuple] = set()
        self._starts: list[float] = []
        self._joins: list = []
        self._total_s = 0.0
        self._drag = None  # dict describing an in-progress drag

        # Persistent scene items / per-lane containers.
        self._static_group: Optional[QGraphicsItemGroup] = None
        self._clip_group: Optional[QGraphicsItemGroup] = None
        self._transition_group: Optional[QGraphicsItemGroup] = None
        self._text_groups: dict[int, QGraphicsItemGroup] = {}
        self._sound_groups: dict[int, QGraphicsItemGroup] = {}
        self._total_w = 0.0
        self._transition_popup: Optional["_TransitionPopup"] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._scene = QGraphicsScene()
        self._view = _TimelineGraphicsView(self._scene, self)
        total_h = self._lanes_total_height()
        self._view.setMinimumHeight(total_h)
        self._view.setMaximumHeight(total_h)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setFrameShape(self._view.Shape.NoFrame)
        self._view.setBackgroundBrush(QBrush(QColor(COLORS["bg"])))
        layout.addWidget(self._view)

        self.redraw()

    # -------------------------------------------------------------- state --

    def set_zoom(self, pps: float):
        self.pixels_per_second = max(4.0, min(400.0, pps))
        self.redraw()

    def set_project_data(self, clips, text_overlays, sound_clips):
        self.clips = clips
        self.text_overlays = text_overlays
        self.sound_clips = sound_clips
        self.redraw()

    def reset_waveform_state(self):
        """Drops pending/done waveform bookkeeping plus the decode cache, and
        cancels outstanding thumbnail/waveform jobs. Call on New/Load Project
        -- otherwise a file already marked done ("no audio there") from a
        previous project is never retried."""
        cancel_group(WAVEFORM_GROUP)
        cancel_group(THUMB_GROUP)
        self._waveform_pending.clear()
        self._waveform_done.clear()
        self._thumb_pending.clear()
        waveform.clear_cache()

    def total_video_duration(self) -> float:
        return self._total_s

    def _video_starts(self) -> list[float]:
        return self._starts

    # -------------------------------------------------------------- layout --

    def _x_of(self, seconds: float) -> float:
        return LABEL_COL_W + seconds * self.pixels_per_second

    def _s_of(self, x: float) -> float:
        return max(0.0, (x - LABEL_COL_W) / self.pixels_per_second)

    def _transition_track_y(self) -> float:
        return TOP_PAD

    def _video_track_y(self) -> float:
        return TOP_PAD + TRANSITION_TRACK_H + TRACK_GAP

    def _text_track_y(self) -> float:
        return TOP_PAD + TRANSITION_TRACK_H + TRACK_GAP + VIDEO_TRACK_H + TRACK_GAP

    def _audio_track_y(self) -> float:
        return (TOP_PAD + TRANSITION_TRACK_H + TRACK_GAP + VIDEO_TRACK_H + TRACK_GAP
                + TEXT_TRACK_H + TRACK_GAP)

    def _lanes_total_height(self) -> float:
        """Single source of truth for the view's/scene's total height --
        __init__ (fixed view height) and redraw() (scene rect) each need this
        and previously computed it separately by hand, which is exactly how
        the view fell out of sync with an added lane and started clipping
        the bottom row. BOTTOM_PAD adds a little breathing room below the
        audio row rather than cutting it exactly at its bottom edge."""
        return self._audio_track_y() + AUDIO_TRACK_H + BOTTOM_PAD

    # ------------------------------------------------------------------ draw --

    def redraw(self):
        """Full relayout: rebuilds every lane. Called on structural/zoom/
        selection changes -- NOT on the per-drag hot path."""
        self._starts, self._joins, self._total_s = clip_layout(self.clips)
        total_s = max(self._total_s,
                       max([s.start_s + s.clip_duration_s for s in self.sound_clips], default=0.0) + 5.0)
        self._total_w = self._x_of(total_s) + 200

        self._rebuild_static()
        self._rebuild_clips()
        self._rebuild_transitions()
        self._rebuild_text()
        self._rebuild_sound()

        total_h = self._lanes_total_height()
        self._scene.setSceneRect(0, 0, max(self._total_w, self._view.viewport().width()), total_h)

    def _remove_group(self, group: Optional[QGraphicsItemGroup]):
        if group is not None and group.scene() is not None:
            self._scene.removeItem(group)

    def _new_group(self, z: int) -> QGraphicsItemGroup:
        group = QGraphicsItemGroup()
        group.setZValue(z)
        self._scene.addItem(group)
        return group

    # -- static lane (track backgrounds/labels, empty hint) --

    def _rebuild_static(self):
        self._remove_group(self._static_group)
        g = self._new_group(_Z_STATIC)
        self._static_group = g

        for y, name, h in ((self._transition_track_y(), "FX", TRANSITION_TRACK_H),
                            (self._video_track_y(), "VIDEO", VIDEO_TRACK_H),
                            (self._text_track_y(), "TEXT", TEXT_TRACK_H),
                            (self._audio_track_y(), "AUDIO", AUDIO_TRACK_H)):
            g.addToGroup(self._scene.addRect(QRectF(0, y, LABEL_COL_W, h), QPen(Qt.PenStyle.NoPen),
                                              QBrush(QColor(COLORS["surface"]))))
            lbl = self._scene.addSimpleText(name, QFont("Segoe UI", 7, QFont.Weight.Bold))
            lbl.setBrush(QBrush(QColor(COLORS["text_dim"])))
            lbl.setPos(6, y + h / 2 - 6)
            g.addToGroup(lbl)

        if not self.clips:
            y = self._video_track_y()
            empty = self._scene.addSimpleText("Drag clips here from the Bin, or select one and press Add.",
                                               QFont("Segoe UI", 9))
            empty.setBrush(QBrush(QColor(COLORS["text_dim"])))
            empty.setPos(LABEL_COL_W + 12, y + VIDEO_TRACK_H / 2 - 8)
            g.addToGroup(empty)

    # -------------------------------------------------------- video clips lane --

    @staticmethod
    def _thumb_key(clip: TimelineClip) -> Optional[tuple]:
        ident = waveform.file_key(clip.path)
        if ident is None:
            return None
        return ident if clip.is_image else (*ident, round(clip.in_s, 3), round(clip.out_s, 3))

    def _thumbnail_for(self, clip: TimelineClip):
        key = self._thumb_key(clip)
        if key is None:
            return None
        pix = self._thumb_cache.get(key)
        if pix is not None:
            self._thumb_cache.move_to_end(key)
            return pix
        if key not in self._thumb_pending:
            self._thumb_pending.add(key)
            request_thumbnail(clip, lambda data, key=key: self._on_thumbnail(key, data))
        return None

    def _on_thumbnail(self, key: tuple, png_bytes: bytes):
        self._thumb_pending.discard(key)
        self._thumb_cache[key] = pil_to_qpixmap(bytes_to_image(png_bytes))
        while len(self._thumb_cache) > THUMB_CACHE_MAX:
            self._thumb_cache.popitem(last=False)
        # Coalesce: a project load lands dozens of thumbnails back-to-back;
        # rebuild the lane once per event-loop pass, not once per arrival.
        if not self._clips_rebuild_scheduled:
            self._clips_rebuild_scheduled = True
            QTimer.singleShot(0, self._deferred_rebuild_clips)

    def _deferred_rebuild_clips(self):
        self._clips_rebuild_scheduled = False
        self._rebuild_clips()

    def _rebuild_clips(self):
        self._remove_group(self._clip_group)
        g = self._new_group(_Z_CONTENT)
        self._clip_group = g
        y = self._video_track_y()
        thumb_h = VIDEO_TRACK_H - 16
        name_font = QFont("Segoe UI", 7)
        name_fm = QFontMetrics(name_font)

        starts = self._video_starts()
        for i, clip in enumerate(self.clips):
            x0 = self._x_of(starts[i])
            w = max(6.0, clip.duration * self.pixels_per_second)
            selected = self.selected == ("clip", i)

            thumb = self._thumbnail_for(clip)
            if thumb is not None:
                item = self._scene.addPixmap(thumb.scaled(int(w), thumb_h,
                                                           Qt.AspectRatioMode.IgnoreAspectRatio,
                                                           Qt.TransformationMode.SmoothTransformation))
                item.setPos(x0, y + 4)
                g.addToGroup(item)
            else:
                g.addToGroup(self._scene.addRect(QRectF(x0, y + 4, w, thumb_h), QPen(Qt.PenStyle.NoPen),
                                                  QBrush(QColor(COLORS["surface_alt"]))))

            border = QColor(COLORS["accent"]) if selected else QColor(COLORS["border_strong"])
            border_w = 2.5 if selected else 1.5
            g.addToGroup(self._scene.addRect(QRectF(x0, y + 4, w, thumb_h), QPen(border, border_w)))

            # The transparent full-body hit rect goes on FIRST, so the smaller
            # controls added after it (label, delete button) sit on top and win
            # the hit-test.
            hit = self._scene.addRect(QRectF(x0, y + 4, w, thumb_h), QPen(Qt.PenStyle.NoPen),
                                       QBrush(Qt.BrushStyle.NoBrush))
            hit.setData(0, ("clip", i))
            g.addToGroup(hit)

            label = name_fm.elidedText(clip.path.name, Qt.TextElideMode.ElideRight, int(max(0, w - 8)))
            text_item = self._scene.addSimpleText(label, name_font)
            text_item.setBrush(QBrush(QColor("white")))
            text_item.setPos(x0 + 3, y + VIDEO_TRACK_H - 12)
            text_item.setData(0, ("clip", i))
            g.addToGroup(text_item)

            # Delete button on the LEFT edge: the next clip's transition
            # overlap covers this block's right-hand tail.
            remove_item = self._scene.addRect(QRectF(x0, y + 4, 14, 14), QPen(QColor("white")),
                                               QBrush(QColor(COLORS["danger"])))
            remove_item.setData(0, ("remove_clip", i))
            g.addToGroup(remove_item)
            close_lbl = self._scene.addSimpleText("x", QFont("Segoe UI", 8, QFont.Weight.Bold))
            close_lbl.setBrush(QBrush(QColor("white")))
            close_lbl.setPos(x0 + 3, y + 3)
            close_lbl.setData(0, ("remove_clip", i))
            g.addToGroup(close_lbl)

    # ---------------------------------------------------- transitions lane --

    def _rebuild_transitions(self):
        """Its own dedicated lane rather than living inside the video track
        (see the Save-freeze/transition-box investigation): keeps every clip
        block's on-screen position pure time->pixel like the text/sound
        lanes, so nothing drifts out of visual alignment as transitions are
        added. Each box is fixed-width regardless of zoom -- it's a control,
        not a time-scaled span -- centered on the cut it edits and NOT
        draggable (click-only, opens _open_transition_popup)."""
        self._remove_group(self._transition_group)
        g = self._new_group(_Z_CONTENT)
        self._transition_group = g
        y = self._transition_track_y()

        starts = self._video_starts()
        for i, clip in enumerate(self.clips):
            if i == 0:
                continue
            # Centered on the overlap (or on the cut, for a hard cut).
            cx = self._x_of(starts[i] + self._joins[i].duration / 2)
            box_path = QPainterPath()
            box_path.addRoundedRect(QRectF(cx - TRANSITION_BOX_W / 2, y, TRANSITION_BOX_W, TRANSITION_TRACK_H), 4, 4)
            rect = self._scene.addPath(box_path, QPen(QColor(COLORS["border_strong"]), 1.5),
                                        QBrush(QColor(COLORS["surface_alt"])))
            rect.setData(0, ("transition", i))
            g.addToGroup(rect)

            # The effective join: a transition too long for its clips is
            # clamped, or demoted to a cut if there's no room at all.
            label = TRANSITION_LABELS.get(self._joins[i].transition, "Hard Cut")
            text = self._scene.addSimpleText(label, QFont("Segoe UI", 7))
            text.setBrush(QBrush(QColor(COLORS["text"])))
            tb = text.boundingRect()
            text.setPos(cx - tb.width() / 2, y + TRANSITION_TRACK_H / 2 - tb.height() / 2)
            text.setData(0, ("transition", i))
            g.addToGroup(text)

    def _open_transition_popup(self, clip_index: int, global_pos):
        """Opens a small Qt.Popup with the transition controls, instead of
        permanently embedding a QComboBox/QLineEdit into the QGraphicsScene.
        Keeps a reference on self -- a popup with no parent and no Python
        reference held anywhere past this method returning is fair game for
        garbage collection before it ever gets a chance to show, which is
        why clicking a transition box used to silently do nothing."""
        if not (0 <= clip_index < len(self.clips)):
            return
        clip = self.clips[clip_index]

        def apply(key, dur, direction):
            self._on_transition_change(clip, key, dur, direction)
            self.redraw()

        self._transition_popup = _TransitionPopup(clip, apply)
        self._transition_popup.move(global_pos)
        self._transition_popup.show()

    # -------------------------------------------------------------- text lane --

    def _rebuild_text(self):
        for group in self._text_groups.values():
            self._remove_group(group)
        self._text_groups = {}
        y = self._text_track_y()

        for i, overlay in enumerate(self.text_overlays):
            w = max(4.0, overlay.duration_s * self.pixels_per_second)
            selected = self.selected == ("text", i)
            color = QColor(COLORS["violet"])
            g = self._new_group(_Z_CONTENT)
            block = self._scene.addRect(QRectF(0, 2, w, TEXT_TRACK_H - 4),
                                        QPen(QColor("white") if selected else color, 1.5 if selected else 1),
                                        QBrush(color.lighter(115) if selected else color))
            block.setOpacity(0.85 if selected else 0.6)
            block.setData(0, ("text", i))
            g.addToGroup(block)
            if w > 30:
                snippet = (overlay.text or "").replace("\n", " ")[:20]
                t = self._scene.addSimpleText(snippet, QFont("Segoe UI", 6))
                t.setBrush(QBrush(QColor("white")))
                t.setPos(3, 3)
                t.setData(0, ("text", i))
                g.addToGroup(t)
            g.setPos(self._x_of(overlay.start_s), y)
            self._text_groups[i] = g

    # ------------------------------------------------------------- audio lane --

    def _rebuild_sound(self):
        for group in self._sound_groups.values():
            self._remove_group(group)
        self._sound_groups = {}
        y = self._audio_track_y()

        total_video_s = self.total_video_duration()
        for i, sound in enumerate(self.sound_clips):
            played = sound.played_duration_s(max(total_video_s, sound.start_s + sound.clip_duration_s))
            w = max(4.0, played * self.pixels_per_second)
            selected = self.selected == ("sound", i)
            color = QColor(COLORS["success"])
            g = self._new_group(_Z_CONTENT)

            block = self._scene.addRect(QRectF(0, 2, w, AUDIO_TRACK_H - 4),
                                        QPen(QColor("white") if selected else color, 1.5 if selected else 1),
                                        QBrush(color.darker(220)))
            block.setData(0, ("sound", i))
            g.addToGroup(block)

            key = waveform.file_key(Path(sound.source))
            file_peaks = waveform.cached(key) if key else None
            if file_peaks is None:
                if key and key not in self._waveform_done and key not in self._waveform_pending:
                    self._request_waveform(key)
            else:
                peaks = file_peaks.segment(sound.clip_start_s, sound.clip_duration_s)
                if peaks:
                    self._draw_waveform(g, peaks, w, color)

            if w > 24:
                name = self._scene.addSimpleText(sound.source.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:18],
                                                  QFont("Segoe UI", 6))
                name.setBrush(QBrush(QColor("white")))
                name.setPos(3, 3)
                name.setData(0, ("sound", i))
                g.addToGroup(name)

            g.setPos(self._x_of(sound.start_s), y)
            self._sound_groups[i] = g

    def _draw_waveform(self, group, peaks, w: float, color: QColor):
        """Draws `peaks` (high-resolution min/max pairs) as vertical bars,
        downsampled to about one column per 2 px of the block's current width
        -- so zooming in shows more detail up to the decoded resolution, while
        zoomed-out blocks stay sparse instead of a solid smear."""
        mid = AUDIO_TRACK_H / 2
        # ~68% of the block height, so amplitude + stroke stay inside the block.
        half_h = (AUDIO_TRACK_H - 4) / 2 * 0.68
        n = len(peaks)
        cols = max(1, min(n, int(w / 2)))
        pen_w = min(2.0, max(0.8, w / cols * 0.6))
        path = QPainterPath()
        for c in range(cols):
            a = c * n // cols
            b = max(a + 1, (c + 1) * n // cols)
            seg = peaks[a:b]
            lo = min(s[0] for s in seg)
            hi = max(s[1] for s in seg)
            px = (c / cols) * w
            path.moveTo(px, mid - max(-1.0, min(1.0, hi)) * half_h)
            path.lineTo(px, mid - max(-1.0, min(1.0, lo)) * half_h)
        group.addToGroup(self._scene.addPath(path, QPen(color.lighter(140), pen_w)))

    def _request_waveform(self, key: tuple):
        self._waveform_pending.add(key)

        def done(_exit_code: int, data: bytes):
            try:
                waveform.reduce_and_store(key, data)
            except Exception:
                log.exception("Waveform: failed to reduce PCM for %s", key[0])
            self._waveform_pending.discard(key)
            self._waveform_done.add(key)  # don't re-request even if it yielded nothing
            self._rebuild_sound()

        submit("ffmpeg", waveform.ffmpeg_args(Path(key[0])), done, group=WAVEFORM_GROUP)

    # ----------------------------------------------------------------- hits --

    def _item_action_at(self, x: float, y: float):
        for item in self._scene.items(QPointF(x, y)):
            data = item.data(0)
            if data is not None:
                return data
        return None

    def _nearest_snap(self, seconds: float) -> float:
        candidates = [0.0, *self._video_starts(), self.total_video_duration()]
        px = self._x_of(seconds)
        best = seconds
        best_d = SNAP_PX + 1
        for c in candidates:
            d = abs(self._x_of(c) - px)
            if d < best_d:
                best_d, best = d, c
        return best

    # ------------------------------------------------------------------ input --

    def on_view_press(self, event: QMouseEvent):
        p = self._view.mapToScene(event.position().toPoint())
        x, y = p.x(), p.y()

        action = self._item_action_at(x, y)
        if action is not None:
            kind, i = action
            if kind == "remove_clip":
                self._on_remove_clip(i)
                return
            if kind == "transition":
                self._open_transition_popup(i, event.globalPosition().toPoint())
                return
            if kind == "clip":
                self._select(("clip", i))
                self._drag = {"kind": "clip", "index": i}
                return
            if kind == "text":
                self._select(("text", i))
                overlay = self.text_overlays[i]
                self._drag = {"kind": "text", "index": i, "offset": x - self._x_of(overlay.start_s)}
                return
            if kind == "sound":
                self._select(("sound", i))
                sound = self.sound_clips[i]
                self._drag = {"kind": "sound", "index": i, "offset": x - self._x_of(sound.start_s)}
                return
        self._select(None)

    def _select(self, selection):
        """Re-clicking the already-selected item (e.g. to start dragging it)
        must not re-emit: that rebuilds the Inspector editor and throws away
        any unsaved edits in it."""
        if selection == self.selected:
            return
        self.selected = selection
        self.selection_changed.emit(selection)
        self.redraw()

    def on_view_double_click(self, event: QMouseEvent):
        p = self._view.mapToScene(event.position().toPoint())
        action = self._item_action_at(p.x(), p.y())
        if action is not None and action[0] == "clip":
            self._on_edit_clip(action[1])

    def on_view_move(self, event: QMouseEvent):
        if self._drag is None:
            return
        p = self._view.mapToScene(event.position().toPoint())
        x = p.x()
        if self._drag["kind"] == "text":
            new_start = self._nearest_snap(max(0.0, self._s_of(x - self._drag["offset"])))
            i = self._drag["index"]
            self.text_overlays[i].start_s = new_start
            group = self._text_groups.get(i)
            if group is not None:
                group.setPos(self._x_of(new_start), self._text_track_y())
        elif self._drag["kind"] == "sound":
            new_start = self._nearest_snap(max(0.0, self._s_of(x - self._drag["offset"])))
            i = self._drag["index"]
            self.sound_clips[i].start_s = new_start
            group = self._sound_groups.get(i)
            if group is not None:
                group.setPos(self._x_of(new_start), self._audio_track_y())

    def on_view_release(self, event: QMouseEvent):
        if self._drag is None:
            return
        kind = self._drag["kind"]
        index = self._drag["index"]
        self._drag = None
        if kind == "text":
            self._on_move_text(index)
        elif kind == "sound":
            self._on_move_sound(index)
        elif kind == "clip":
            p = self._view.mapToScene(event.position().toPoint())
            starts = self._video_starts()
            target = 0
            for i, s in enumerate(starts):
                if self._x_of(s) <= p.x():
                    target = i
            if target != index:
                self._on_reorder(index, target)

    def on_wheel(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else (1 / 1.15)
            self.set_zoom(self.pixels_per_second * factor)
            event.accept()
            return
        bar = self._view.horizontalScrollBar()
        bar.setValue(bar.value() - int(event.angleDelta().y() / 2))
        event.accept()

    # ------------------------------------------------------------ drag & drop --

    def accepts_drop(self, event) -> bool:
        return event.mimeData().hasFormat(BIN_ITEM_MIME)

    def handle_drop(self, event: QDropEvent):
        raw = bytes(event.mimeData().data(BIN_ITEM_MIME)).decode("utf-8")
        self._on_drop_bin_item(raw)
        event.acceptProposedAction()


class _TimelineGraphicsView(QGraphicsView):
    def __init__(self, scene, owner: TimelineWidget):
        super().__init__(scene)
        self._owner = owner
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

    def mousePressEvent(self, event: QMouseEvent):
        self._owner.on_view_press(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self._owner.on_view_double_click(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        self._owner.on_view_move(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._owner.on_view_release(event)

    def wheelEvent(self, event: QWheelEvent):
        self._owner.on_wheel(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._owner.accepts_drop(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._owner.accepts_drop(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if self._owner.accepts_drop(event):
            self._owner.handle_drop(event)


class _TransitionPopup(QWidget):
    """Short-lived popup for editing one clip's incoming transition -- see
    TimelineWidget._open_transition_popup for why this replaced a permanently
    scene-embedded QComboBox/QLineEdit."""

    def __init__(self, clip: TimelineClip, on_apply: Callable[[str, float, str], None]):
        super().__init__(None, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._on_apply = on_apply
        self.setStyleSheet(f"background-color: {COLORS['surface_raised']}; "
                            f"border: 1px solid {COLORS['border_strong']};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self._combo = QComboBox()
        self._combo.addItems(list(TRANSITION_LABELS.values()))
        self._combo.setCurrentText(TRANSITION_LABELS.get(clip.transition_type, "Hard Cut"))
        layout.addWidget(self._combo)

        self._dur_edit = QLineEdit(str(clip.transition_duration))
        layout.addWidget(self._dur_edit)

        self._dir_combo = QComboBox()
        self._dir_combo.addItems(["left", "right", "up", "down"])
        self._dir_combo.setCurrentText(clip.transition_direction)
        layout.addWidget(self._dir_combo)

        self._sync_visibility()
        self._combo.currentTextChanged.connect(self._on_change)
        self._dur_edit.editingFinished.connect(self._on_change)
        self._dir_combo.currentTextChanged.connect(self._on_change)

    def _sync_visibility(self):
        key = TRANSITION_KEYS_BY_LABEL.get(self._combo.currentText(), "cut")
        self._dur_edit.setVisible(key != "cut")
        self._dir_combo.setVisible(key in ("wipe", "slide"))
        self.adjustSize()

    def _on_change(self, *_args):
        self._sync_visibility()
        key = TRANSITION_KEYS_BY_LABEL.get(self._combo.currentText(), "cut")
        try:
            dur = float(self._dur_edit.text())
        except ValueError:
            dur = DEFAULT_TRANSITION_DURATION
        self._on_apply(key, dur, self._dir_combo.currentText())
