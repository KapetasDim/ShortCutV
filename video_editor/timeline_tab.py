from __future__ import annotations

import copy
import io
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageTk

from . import ffmpeg_ops
from .clip_trim_editor import ClipTrimEditor
from .crop_resize_tab import IMAGE_EXTS
from .image_clip_editor import ImageClipEditor
from .logging_config import attach_text_widget, get_logger
from .native_dialog import ask_directory, ask_open_filename, ask_save_filename
from .player import SharedPlayer
from .project import (DEFAULT_TRANSITION_DURATION, SoundClip, TextOverlay, TimelineClip, TimelineProject,
                       load_project, save_project)
from .sound_clip_editor import SoundClipEditor
from .text_overlay_editor import TextOverlayEditor
from .ui_scale import get_scale
from .widgets import ScrollableFrame, Scrubber, bytes_to_image, fmt_time

log = get_logger()

BG = "#1e1e1e"
DEFAULT_IMAGE_DURATION = 1.5
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}

TRANSITION_LABELS = {
    "crossfade": "Crossfade",
    "fade_black": "Fade to Black",
    "wipe": "Wipe",
    "slide": "Slide",
    "cut": "Hard Cut",
}
TRANSITION_KEYS_BY_LABEL = {v: k for k, v in TRANSITION_LABELS.items()}


class TimelineCanvas(tk.Canvas):
    """Horizontal strip of clip blocks. Drag a block and release to reorder;
    click the x in a block's corner to remove it; each gap between blocks
    holds an embedded transition-type + duration control."""

    EDIT_HOVER_MULT = 4  # edit-button hover growth ratio; kept constant across profiles

    def __init__(self, parent, on_reorder, on_remove, on_transition_change, on_edit, **kwargs):
        scale = get_scale()
        self.BLOCK_W = scale.timeline_block_w
        self.BLOCK_H = scale.timeline_block_h
        self.GAP_W = scale.timeline_gap_w
        self.PAD = scale.timeline_pad
        self.EDIT_R = scale.timeline_edit_r
        self._font_xs = scale.font_xs
        self._font_sm = scale.font_sm
        super().__init__(parent, bg=BG, height=self.BLOCK_H + 2 * self.PAD,
                          highlightthickness=0, **kwargs)
        self.clips: list[TimelineClip] = []
        self._thumbs: dict[int, ImageTk.PhotoImage] = {}
        self._on_reorder = on_reorder
        self._on_remove = on_remove
        self._on_transition_change = on_transition_change
        self._on_edit = on_edit
        self._embedded = []  # canvas window ids for gap controls, cleared each redraw
        self._drag_index = None
        self._drag_start_x = 0
        self._hover_index = None

        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_hover_motion)
        self.bind("<Leave>", self._on_hover_leave)

    def set_clips(self, clips: list[TimelineClip]):
        """_thumbs is keyed by list position, not clip identity -- a delete or
        insert shifts every later clip's true index without changing what's
        cached there, which would otherwise silently show a stale/wrong
        thumbnail at the shifted positions. Diff against the previous list by
        object identity (edits like trim/crop mutate a clip in place, so an
        untouched clip keeps the same object across calls) and drop cached
        thumbnails from the first position that no longer matches onward, so
        shifted slots fall back to the placeholder until re-fetched instead of
        showing another clip's frame."""
        old = self.clips
        divergence = None
        for i in range(min(len(old), len(clips))):
            if old[i] is not clips[i]:
                divergence = i
                break
        else:
            if len(old) != len(clips):
                divergence = min(len(old), len(clips))
        if divergence is not None:
            for i in list(self._thumbs.keys()):
                if i >= divergence:
                    del self._thumbs[i]
        self.clips = clips
        self.redraw()

    def set_thumbnail(self, index: int, png_bytes: bytes):
        if not png_bytes or index >= len(self.clips):
            return
        img = bytes_to_image(png_bytes).resize((self.BLOCK_W, self.BLOCK_H))
        self._thumbs[index] = ImageTk.PhotoImage(img)
        self.redraw()

    def _block_x(self, index: int) -> float:
        x = self.PAD
        for i in range(index):
            x += self.BLOCK_W + self.GAP_W
        return x

    def redraw(self):
        self.delete("all")
        for w in self._embedded:
            try:
                w.destroy()
            except tk.TclError:
                pass  # widget already destroyed -- expected during teardown
        self._embedded = []

        y0 = self.PAD
        y1 = y0 + self.BLOCK_H

        for i, clip in enumerate(self.clips):
            x0 = self._block_x(i)
            x1 = x0 + self.BLOCK_W

            if i > 0:
                gap_x = x0 - self.GAP_W
                self._add_transition_control(gap_x, y0, clip)

            # The border-only outline rect below has no fill, so it's only
            # hit-testable near its edges -- tag the actual filled background
            # (thumbnail or placeholder) too, or double-click on the block's
            # interior would never register.
            thumb = self._thumbs.get(i)
            if thumb is not None:
                self.create_image(x0, y0, anchor="nw", image=thumb, tags=(f"block{i}",))
            else:
                self.create_rectangle(x0, y0, x1, y1, fill="#333", outline="", tags=(f"block{i}",))

            self.create_rectangle(x0, y0, x1, y1, outline="#4a9eff", width=2, tags=(f"block{i}",))
            name = clip.path.name
            label = name if len(name) <= 20 else name[:17] + "..."
            label_bar_h = self.EDIT_R * 2
            self.create_rectangle(x0, y1 - label_bar_h, x1, y1, fill="#000", outline="", stipple="gray50")
            self.create_text(x0 + 4, y1 - label_bar_h // 2, anchor="w", text=label, fill="white",
                              font=("Courier", self._font_xs), tags=(f"block{i}",))

            badge_r = self.EDIT_R
            self.create_oval(x1 - badge_r * 2, y0, x1, y0 + badge_r * 2, fill="#cc4444", outline="white",
                              tags=(f"remove{i}",))
            self.create_text(x1 - badge_r, y0 + badge_r, text="x", fill="white",
                              font=("Courier", self._font_sm, "bold"), tags=(f"remove{i}",))
            self.tag_bind(f"remove{i}", "<ButtonPress-1>", lambda e, i=i: self._remove(i))
            self.tag_bind(f"block{i}", "<Double-Button-1>", lambda e, i=i: self._on_edit(i))

            cx, cy = x0 + self.EDIT_R, y0 + self.EDIT_R
            hovered = i == self._hover_index
            r = self.EDIT_R * self.EDIT_HOVER_MULT if hovered else self.EDIT_R
            font_size = self._font_sm * self.EDIT_HOVER_MULT if hovered else self._font_sm
            self.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#4a9eff", outline="white",
                              tags=(f"edit{i}",))
            self.create_text(cx, cy, text="E", fill="white", font=("Courier", font_size, "bold"),
                              tags=(f"edit{i}",))
            self.tag_bind(f"edit{i}", "<ButtonPress-1>", lambda e, i=i: self._on_edit(i))

        if not self.clips:
            scale = get_scale()
            self.create_text(self.PAD, self.PAD + self.BLOCK_H / 2, anchor="w",
                              text="Timeline is empty -- add clips from the Library above",
                              fill="#666", font=("Courier", scale.font_md))

        total_w = self._block_x(len(self.clips)) + self.BLOCK_W + self.PAD
        self.configure(scrollregion=(0, 0, max(total_w, 1), self.BLOCK_H + 2 * self.PAD))

    def _add_transition_control(self, x, y, clip: TimelineClip):
        frame = tk.Frame(self, bg=BG)
        var = tk.StringVar(value=TRANSITION_LABELS.get(clip.transition_type, "Hard Cut"))
        combo = ttk.Combobox(frame, textvariable=var, values=list(TRANSITION_LABELS.values()),
                              state="readonly", width=11, font=("Courier", self._font_xs))
        combo.pack()

        # Duration/direction only apply to non-cut transitions (and direction
        # only to wipe/slide), so this sub-frame is rebuilt whenever the
        # transition type changes instead of always showing every field.
        dynamic = tk.Frame(frame, bg=BG)
        dynamic.pack()
        dur_var = tk.StringVar(value=str(clip.transition_duration))
        dir_var = tk.StringVar(value=clip.transition_direction)

        def apply(_e=None):
            key = TRANSITION_KEYS_BY_LABEL.get(var.get(), "cut")
            try:
                dur = float(dur_var.get())
            except ValueError:
                dur = DEFAULT_TRANSITION_DURATION
            self._on_transition_change(clip, key, dur, dir_var.get())

        def rebuild_dynamic():
            for w in dynamic.winfo_children():
                w.destroy()
            key = TRANSITION_KEYS_BY_LABEL.get(var.get(), "cut")
            if key != "cut":
                dur_entry = tk.Entry(dynamic, textvariable=dur_var, width=6, font=("Courier", self._font_xs))
                dur_entry.pack()
                dur_entry.bind("<FocusOut>", apply)
                dur_entry.bind("<Return>", apply)
            if key in ("wipe", "slide"):
                dir_combo = ttk.Combobox(dynamic, textvariable=dir_var,
                                          values=["left", "right", "up", "down"],
                                          state="readonly", width=6, font=("Courier", self._font_xs))
                dir_combo.pack()
                dir_combo.bind("<<ComboboxSelected>>", apply)

        def on_combo_change(_e=None):
            rebuild_dynamic()
            apply()

        combo.bind("<<ComboboxSelected>>", on_combo_change)
        rebuild_dynamic()

        self.create_window(x + self.GAP_W / 2, y + self.BLOCK_H / 2, window=frame)
        self._embedded.append(frame)

    def _index_at_x(self, x) -> int:
        for i in range(len(self.clips)):
            bx0 = self._block_x(i)
            if bx0 <= x <= bx0 + self.BLOCK_W:
                return i
        return len(self.clips) - 1

    def _block_index_at(self, x, y):
        """Returns the clip index if (x, y) is actually within a block's
        rectangle (not just its column, which _index_at_x alone doesn't rule
        out for points sitting in a gap), else None."""
        y0 = self.PAD
        y1 = y0 + self.BLOCK_H
        if not (y0 <= y <= y1):
            return None
        for i in range(len(self.clips)):
            bx0 = self._block_x(i)
            if bx0 <= x <= bx0 + self.BLOCK_W:
                return i
        return None

    def _remove(self, index: int):
        self._on_remove(index)

    def _on_press(self, event):
        idx = self._index_at_x(self.canvasx(event.x))
        if 0 <= idx < len(self.clips):
            bx0 = self._block_x(idx)
            if bx0 <= self.canvasx(event.x) <= bx0 + self.BLOCK_W:
                self._drag_index = idx
                self._drag_start_x = event.x

    def _on_motion(self, _event):
        pass  # visual reorder feedback intentionally omitted; reorder resolves on release

    def _on_hover_motion(self, event):
        idx = self._block_index_at(self.canvasx(event.x), event.y)
        if idx != self._hover_index:
            self._hover_index = idx
            self.redraw()

    def _on_hover_leave(self, _event):
        if self._hover_index is not None:
            self._hover_index = None
            self.redraw()

    def _on_release(self, event):
        if self._drag_index is None:
            return
        target = self._index_at_x(self.canvasx(event.x))
        target = max(0, min(target, len(self.clips) - 1))
        if target != self._drag_index:
            self._on_reorder(self._drag_index, target)
        self._drag_index = None


class TimelineTab(tk.Frame):
    def __init__(self, parent, state, player: SharedPlayer, preview_player: SharedPlayer):
        super().__init__(parent, bg=BG)
        self.state = state
        self.player = player                    # library-clip browse preview
        self.preview_player = preview_player     # composed timeline (proxy/final) preview
        self.project = TimelineProject()
        self._lib_input_dir: Path | None = None
        self._lib_output_dir: Path | None = None
        self._library_items: list[tuple[str, Path]] = []  # (label, path)
        self._preview_source: Path | None = None
        self._rendering = False
        self._render_proc_holder: dict = {}
        self._has_composed_preview = False   # survives rebuild() so the scrubber lock state is restored
        self._composed_preview_label_text = "Not rendered yet"
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_proxy"
        self._tmp_dir.mkdir(exist_ok=True)
        self._edit_tab_frame = None    # non-None while a clip/text-overlay edit tab is open
        self._edit_tab_editor = None   # the embedded editor instance living in that tab

        self._build_ui()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True)

        # A weighted grid column's *minimum* width still comes from its
        # content's natural requested size, even with grid_propagate(False)
        # on the slave -- and an embedded mpv video's native window can be
        # resized directly by the OS/mpv when a very different-aspect-ratio
        # clip loads, independent of Tk's own geometry manager, which fed
        # back into the grid computation and made the 60/40 split visibly
        # drift (pre-existing, not specific to the edit-tab Notebook).
        # place() with explicit pixel geometry never asks children for their
        # size at all, so it can't be perturbed this way -- recomputed on
        # every container resize via <Configure>.
        menu = tk.Frame(container, bg=BG)
        sep = ttk.Separator(container, orient="vertical")
        right_wrap = tk.Frame(container, bg=BG)
        SEP_W = 2

        def relayout(_event=None):
            w, h = container.winfo_width(), container.winfo_height()
            if w <= 1 or h <= 1:
                return
            menu_w = round(w * 0.6)
            right_x = menu_w + SEP_W
            menu.place(x=0, y=0, width=menu_w, height=h)
            sep.place(x=menu_w, y=0, width=SEP_W, height=h)
            right_wrap.place(x=right_x, y=0, width=max(0, w - right_x), height=h)

        container.bind("<Configure>", relayout)

        # The right 40% is a Notebook: a permanent "Playback" tab (the
        # library-clip + composed-timeline previews, side by side as before)
        # plus a transient "Edit" tab that appears in place of what used to
        # be a separate popup window (see _open_edit_tab).
        self._right_notebook = ttk.Notebook(right_wrap)
        self._right_notebook.pack(fill="both", expand=True)

        self._build_menu(menu)

        self._current_view_frame = tk.Frame(self._right_notebook, bg=BG)
        self._right_notebook.add(self._current_view_frame, text="Playback")
        self._build_current_view(self._current_view_frame)

    def _build_current_view(self, pane):
        lib_pane = tk.Frame(pane, bg=BG)
        sep = ttk.Separator(pane, orient="vertical")
        composed_pane = tk.Frame(pane, bg=BG)
        SEP_W = 2

        def relayout(_event=None):
            w, h = pane.winfo_width(), pane.winfo_height()
            if w <= 1 or h <= 1:
                return
            half = (w - SEP_W) // 2
            lib_pane.place(x=0, y=0, width=half, height=h)
            sep.place(x=half, y=0, width=SEP_W, height=h)
            composed_pane.place(x=half + SEP_W, y=0, width=max(0, w - half - SEP_W), height=h)

        pane.bind("<Configure>", relayout)

        self._build_lib_pane(lib_pane)
        self._build_composed_pane(composed_pane)

    def _build_menu(self, menu):
        scale = get_scale()
        # Entry (readonly) instead of Label: a long path used to wrap onto
        # multiple lines via wraplength; readonly Entries don't wrap at all,
        # they scroll their content on click-drag/Home/End instead, which is
        # what lets a long path stay reachable without eating vertical space.
        folders = tk.Frame(menu, bg=BG)
        folders.pack(fill="x", padx=scale.space_lg, pady=scale.space_xs)
        self._lib_in_var = tk.StringVar(value="Clips source (input): (none)")
        self._lib_in_lbl = tk.Entry(folders, textvariable=self._lib_in_var, bg=BG, fg="#aaa",
                                     readonlybackground=BG, relief="flat", state="readonly",
                                     font=("Courier", scale.font_sm))
        self._lib_in_lbl.pack(side="left", fill="x", expand=True)
        tk.Button(folders, text="Browse...", command=self._pick_lib_input,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="left", padx=scale.space_xs)

        folders2 = tk.Frame(menu, bg=BG)
        folders2.pack(fill="x", padx=scale.space_lg, pady=scale.space_xxs)
        self._lib_out_var = tk.StringVar(value="Clips source (output): (none)")
        self._lib_out_lbl = tk.Entry(folders2, textvariable=self._lib_out_var, bg=BG, fg="#aaa",
                                      readonlybackground=BG, relief="flat", state="readonly",
                                      font=("Courier", scale.font_sm))
        self._lib_out_lbl.pack(side="left", fill="x", expand=True)
        tk.Button(folders2, text="Browse...", command=self._pick_lib_output,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="left", padx=scale.space_xs)

        lib_frame = tk.Frame(menu, bg=BG)
        lib_frame.pack(fill="x", padx=scale.space_lg, pady=(scale.space_sm, 0))
        lib_header = tk.Frame(lib_frame, bg=BG)
        lib_header.pack(fill="x")
        tk.Label(lib_header, text="Library:", bg=BG, fg="white", font=("Courier", scale.font_lg)).pack(side="left")
        tk.Button(lib_header, text="Refresh", command=self._refresh_library,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)).pack(side="right")
        self._library = ScrollableFrame(lib_frame, height=scale.library_list_h, bg=BG)
        self._library.pack(fill="x")
        tk.Button(lib_frame, text="Add selected -> Timeline", command=self._add_selected,
                  bg="#4a9eff", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(fill="x", pady=scale.space_xs)

        # Intro/outro/canvas-source controls get their own row below the
        # timeline label (rather than crammed onto the same line as it) --
        # side by side there overflowed the 60% left pane's actual width.
        timeline_header = tk.Frame(menu, bg=BG)
        timeline_header.pack(fill="x", padx=scale.space_lg, pady=(scale.space_md, 0))
        tk.Label(timeline_header, text="Timeline (drag block to reorder, x to remove)",
                 bg=BG, fg="white", font=("Courier", scale.font_lg)).pack(side="left")

        timeline_controls_row = tk.Frame(menu, bg=BG)
        timeline_controls_row.pack(fill="x", padx=scale.space_lg, pady=(scale.space_xxs, 0))

        intro_row = tk.Frame(timeline_controls_row, bg=BG)
        intro_row.pack(side="left")
        tk.Label(intro_row, text="Intro transition:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).pack(side="left")
        self._intro_type_var = tk.StringVar(
            value=TRANSITION_LABELS.get(self.project.intro_transition_type, "Hard Cut"))
        intro_combo = ttk.Combobox(intro_row, textvariable=self._intro_type_var,
                                    values=list(TRANSITION_LABELS.values()),
                                    state="readonly", width=12, font=("Courier", scale.font_sm))
        intro_combo.pack(side="left", padx=scale.space_xs)
        self._intro_dynamic = tk.Frame(intro_row, bg=BG)
        self._intro_dynamic.pack(side="left")
        self._intro_dur_var = tk.StringVar(value=str(self.project.intro_transition_duration))
        self._intro_dir_var = tk.StringVar(value=self.project.intro_transition_direction)

        def apply_intro(_e=None):
            key = TRANSITION_KEYS_BY_LABEL.get(self._intro_type_var.get(), "cut")
            try:
                dur = float(self._intro_dur_var.get())
            except ValueError:
                dur = DEFAULT_TRANSITION_DURATION
            self.project.intro_transition_type = key
            self.project.intro_transition_duration = dur
            self.project.intro_transition_direction = self._intro_dir_var.get()
            log.info("Timeline: intro transition set to %s (%.2fs, direction=%s)",
                      key, dur, self._intro_dir_var.get())

        def rebuild_intro_dynamic():
            for w in self._intro_dynamic.winfo_children():
                w.destroy()
            key = TRANSITION_KEYS_BY_LABEL.get(self._intro_type_var.get(), "cut")
            if key != "cut":
                dur_entry = tk.Entry(self._intro_dynamic, textvariable=self._intro_dur_var,
                                      width=6, font=("Courier", scale.font_sm))
                dur_entry.pack(side="left", padx=scale.space_xxs)
                dur_entry.bind("<FocusOut>", apply_intro)
                dur_entry.bind("<Return>", apply_intro)
            if key in ("wipe", "slide"):
                dir_combo = ttk.Combobox(self._intro_dynamic, textvariable=self._intro_dir_var,
                                          values=["left", "right", "up", "down"],
                                          state="readonly", width=6, font=("Courier", scale.font_sm))
                dir_combo.pack(side="left", padx=scale.space_xxs)
                dir_combo.bind("<<ComboboxSelected>>", apply_intro)

        def on_intro_combo_change(_e=None):
            rebuild_intro_dynamic()
            apply_intro()

        intro_combo.bind("<<ComboboxSelected>>", on_intro_combo_change)
        rebuild_intro_dynamic()
        self._rebuild_intro_dynamic = rebuild_intro_dynamic

        outro_row = tk.Frame(timeline_controls_row, bg=BG)
        outro_row.pack(side="left", padx=(scale.space_xl, 0))
        tk.Label(outro_row, text="Outro transition:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).pack(side="left")
        self._outro_type_var = tk.StringVar(
            value=TRANSITION_LABELS.get(self.project.outro_transition_type, "Hard Cut"))
        outro_combo = ttk.Combobox(outro_row, textvariable=self._outro_type_var,
                                    values=list(TRANSITION_LABELS.values()),
                                    state="readonly", width=12, font=("Courier", scale.font_sm))
        outro_combo.pack(side="left", padx=scale.space_xs)
        self._outro_dynamic = tk.Frame(outro_row, bg=BG)
        self._outro_dynamic.pack(side="left")
        self._outro_dur_var = tk.StringVar(value=str(self.project.outro_transition_duration))
        self._outro_dir_var = tk.StringVar(value=self.project.outro_transition_direction)

        def apply_outro(_e=None):
            key = TRANSITION_KEYS_BY_LABEL.get(self._outro_type_var.get(), "cut")
            try:
                dur = float(self._outro_dur_var.get())
            except ValueError:
                dur = DEFAULT_TRANSITION_DURATION
            self.project.outro_transition_type = key
            self.project.outro_transition_duration = dur
            self.project.outro_transition_direction = self._outro_dir_var.get()
            log.info("Timeline: outro transition set to %s (%.2fs, direction=%s)",
                      key, dur, self._outro_dir_var.get())

        def rebuild_outro_dynamic():
            for w in self._outro_dynamic.winfo_children():
                w.destroy()
            key = TRANSITION_KEYS_BY_LABEL.get(self._outro_type_var.get(), "cut")
            if key != "cut":
                dur_entry = tk.Entry(self._outro_dynamic, textvariable=self._outro_dur_var,
                                      width=6, font=("Courier", scale.font_sm))
                dur_entry.pack(side="left", padx=scale.space_xxs)
                dur_entry.bind("<FocusOut>", apply_outro)
                dur_entry.bind("<Return>", apply_outro)
            if key in ("wipe", "slide"):
                dir_combo = ttk.Combobox(self._outro_dynamic, textvariable=self._outro_dir_var,
                                          values=["left", "right", "up", "down"],
                                          state="readonly", width=6, font=("Courier", scale.font_sm))
                dir_combo.pack(side="left", padx=scale.space_xxs)
                dir_combo.bind("<<ComboboxSelected>>", apply_outro)

        def on_outro_combo_change(_e=None):
            rebuild_outro_dynamic()
            apply_outro()

        outro_combo.bind("<<ComboboxSelected>>", on_outro_combo_change)
        rebuild_outro_dynamic()
        self._rebuild_outro_dynamic = rebuild_outro_dynamic

        # Output canvas size is derived from a clip's native resolution rather
        # than typed in by hand -- pick which clip via this dropdown any time,
        # not just automatically from the first clip added (see _dims_for_clip
        # / _on_canvas_source_selected). Hidden/shown by _update_empty_state,
        # same as the row it replaced, since there's nothing to pick from
        # with an empty timeline.
        self._canvas_row = tk.Frame(timeline_controls_row, bg=BG)
        tk.Label(self._canvas_row, text="|", bg=BG, fg="#555",
                 font=("Courier", scale.font_lg)).pack(side="left", padx=scale.space_md)
        tk.Label(self._canvas_row, text="Canvas from clip:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).pack(side="left")
        self._canvas_source_var = tk.StringVar(value="")
        self._canvas_source_combo = ttk.Combobox(self._canvas_row, textvariable=self._canvas_source_var,
                                                   state="readonly", width=22, font=("Courier", scale.font_sm))
        self._canvas_source_combo.pack(side="left", padx=scale.space_xs)
        self._canvas_source_combo.bind("<<ComboboxSelected>>", self._on_canvas_source_selected)
        self._canvas_size_lbl = tk.Label(self._canvas_row, text="", bg=BG, fg="#aaa",
                                          font=("Courier", scale.font_sm))
        self._canvas_size_lbl.pack(side="left", padx=(scale.space_xs, 0))
        self._update_canvas_size_label()

        self._timeline = TimelineCanvas(menu, self._on_reorder, self._on_remove,
                                         self._on_transition_change, self._on_edit_clip)
        self._timeline.pack(fill="x", padx=scale.space_lg, pady=(scale.space_xs, 0))
        # TimelineCanvas already sets its scrollregion to the full block width
        # on every redraw() -- it just had nothing driving xview, so clips past
        # the visible width were simply unreachable once they overflowed it.
        timeline_scrollbar = ttk.Scrollbar(menu, orient="horizontal", command=self._timeline.xview)
        timeline_scrollbar.pack(fill="x", padx=scale.space_lg, pady=(0, scale.space_xs))
        self._timeline.configure(xscrollcommand=timeline_scrollbar.set)

        def _on_timeline_wheel(event):
            self._timeline.xview_scroll(int(-1 * (event.delta / 120)), "units")

        self._timeline.bind("<MouseWheel>", _on_timeline_wheel)

        # Below the timeline: a left column (text-overlay container, sized to
        # its content width -- exactly wide enough for the list's Remove
        # button to sit at its right edge -- with the sound section filling
        # whatever's left below it) and the log console filling the rest of
        # the width to its right, running the full height of this split.
        split_row = tk.Frame(menu, bg=BG)
        split_row.pack(fill="both", expand=True, padx=scale.space_lg, pady=(scale.space_sm, scale.space_sm))

        self._overlay_col = tk.Frame(split_row, bg=BG)
        self._overlay_col.pack(side="left", fill="y")

        # text_header / text_overlay_list / canvas_row are only shown once the
        # timeline has clips (see _update_empty_state) -- built here but not
        # packed yet; self._sound_section/self._save_project_btn are stable
        # anchors so re-showing them via pack(before=...) always restores the
        # correct stacking order even after being pack_forget()'d.
        self._text_header = tk.Frame(self._overlay_col, bg=BG)
        tk.Label(self._text_header, text="Text overlays:", bg=BG, fg="white",
                 font=("Courier", scale.font_lg)).pack(side="left")
        self._add_text_btn = tk.Button(self._text_header, text="Add Text Overlay...", command=self._open_text_editor,
                                        bg="#7a5fff", fg="white", relief="flat", state="disabled",
                                        font=("Courier", scale.font_sm))
        self._add_text_btn.pack(side="right")
        # x2: this list used to only show ~3 rows at a time before needing to scroll.
        # width fixes the column at exactly this content width (see ScrollableFrame) --
        # otherwise, unlike the rest of the app's fill="x" rows, there's no parent
        # width to stretch to here, so it'd fall back to Tk's tiny Canvas default.
        self._text_overlay_list = ScrollableFrame(self._overlay_col, height=scale.text_overlay_list_h * 2,
                                                    width=scale.text_overlay_col_w, bg=BG)

        # A plain white 1px Frame rather than ttk.Separator -- the latter's
        # color follows the active ttk theme (clam here) and doesn't reliably
        # render as pure white against this dark background.
        self._overlay_sep = tk.Frame(self._overlay_col, bg="white", height=1)

        # Sound clips: library sounds placed on the composed timeline (a
        # clip's own embedded audio is edited from the clip's own edit tab
        # instead -- see ClipTrimEditor's volume field). Fills the rest of
        # the column's height below the text-overlay container. Not packed
        # here -- like text_header/text_overlay_list, _update_empty_state
        # controls its visibility (nothing to place sounds against before the
        # timeline has clips and a composed preview).
        self._sound_section = tk.Frame(self._overlay_col, bg=BG)
        tk.Label(self._sound_section, text="Sounds:", bg=BG, fg="white",
                 font=("Courier", scale.font_lg)).pack(anchor="w")
        self._sound_list = ScrollableFrame(self._sound_section, height=scale.text_overlay_list_h * 2,
                                            width=scale.text_overlay_col_w, bg=BG)
        self._sound_list.pack(fill="both", expand=True, pady=scale.space_xs)

        self._log_frame = tk.Frame(split_row, bg=BG)
        self._log_frame.pack(side="left", fill="both", expand=True, padx=(scale.space_lg, 0))
        log_header = tk.Frame(self._log_frame, bg=BG)
        log_header.pack(fill="x")
        tk.Label(log_header, text="Log:", bg=BG, fg="white", font=("Courier", scale.font_sm)).pack(side="left")
        tk.Button(log_header, text="Clear", command=self._clear_log,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_xs)).pack(side="right")
        log_body = tk.Frame(self._log_frame, bg=BG)
        log_body.pack(fill="both", expand=True)
        log_scrollbar = ttk.Scrollbar(log_body, orient="vertical")
        log_scrollbar.pack(side="right", fill="y")
        self._log_text = tk.Text(log_body, bg="#0a0a0a", fg="#ccc", font=("Courier", scale.font_xs),
                                  state="disabled", wrap="none", yscrollcommand=log_scrollbar.set)
        self._log_text.pack(side="left", fill="both", expand=True)
        log_scrollbar.config(command=self._log_text.yview)
        attach_text_widget(self._log_text)

        self._actions = tk.Frame(menu, bg=BG)
        self._actions.pack(fill="x", padx=scale.space_lg, pady=scale.space_sm)

        self._save_project_btn = tk.Button(self._actions, text="Save Project", command=self._save_project,
                                            bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm))
        self._save_project_btn.pack(side="left", padx=(0, scale.space_xs))
        tk.Button(self._actions, text="Load Project", command=self._load_project,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="left", padx=scale.space_xs)

        tk.Label(self._actions, text="Proxy quality:", bg=BG, fg="white",
                 font=("Courier", scale.font_sm)).pack(side="left", padx=(scale.space_xl, 2))
        self._proxy_height_var = tk.StringVar(value="270")
        ttk.Combobox(self._actions, textvariable=self._proxy_height_var,
                     values=["720", "480", "360", "270"], state="readonly",
                     width=5, font=("Courier", scale.font_sm)).pack(side="left", padx=2)

        self._build_btn = tk.Button(self._actions, text="Build Preview", command=self._on_build_preview_clicked,
                                     bg="#7a5fff", fg="white", relief="flat", font=("Courier", scale.font_sm))
        self._build_btn.pack(side="left", padx=scale.space_xs)
        tk.Button(self._actions, text="Render Final Video", command=lambda: self._render(proxy=False),
                  bg="#4a9eff", fg="white", relief="flat", font=("Courier", scale.font_md, "bold")
                  ).pack(side="right")

        self._update_empty_state()

        prog = tk.Frame(menu, bg=BG)
        prog.pack(fill="x", padx=scale.space_lg, pady=(0, scale.space_sm))
        self._progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self._progress.pack(fill="x", side="left", expand=True)
        self._status_lbl = tk.Label(prog, text="", bg=BG, fg="#aaa", font=("Courier", scale.font_sm))
        self._status_lbl.pack(side="right", padx=scale.space_md)

    def _build_lib_pane(self, pane):
        scale = get_scale()
        tk.Label(pane, text="Library clip preview:", bg=BG, fg="white",
                 font=("Courier", scale.font_sm)).pack(anchor="w", padx=scale.space_md,
                                                        pady=(scale.space_md, 0))
        self._lib_preview_frame = tk.Frame(pane, bg="black")
        self._lib_preview_frame.pack(fill="both", expand=True, padx=scale.space_md, pady=scale.space_xs)

        transport = tk.Frame(pane, bg=BG)
        transport.pack(fill="x", padx=scale.space_md)
        self._play_btn = tk.Button(transport, text="▶  Play", command=self._toggle_play,
                                    bg="#333", fg="white", relief="flat", font=("Courier", scale.font_md))
        self._play_btn.pack(side="left")
        self._preview_lbl = tk.Label(pane, text="Previewing: (none)", bg=BG, fg="#aaa",
                                      font=("Courier", scale.font_xs), anchor="w")
        self._preview_lbl.pack(fill="x", padx=scale.space_md)
        self._lib_time_lbl = tk.Label(pane, text="--:--:--.--- / --:--:--.---",
                                       bg=BG, fg="#777", font=("Courier", scale.font_xs), anchor="w")
        self._lib_time_lbl.pack(fill="x", padx=scale.space_md)
        self._scrubber = Scrubber(pane)
        self._scrubber.pack(fill="x", padx=scale.space_md, pady=(scale.space_xxs, scale.space_md))
        self._scrubber.add_seek_callback(self._on_lib_seek)

    def _build_composed_pane(self, pane):
        scale = get_scale()
        tk.Label(pane, text="Timeline (composed) preview:", bg=BG, fg="white",
                 font=("Courier", scale.font_sm)).pack(anchor="w", padx=scale.space_md,
                                                        pady=(scale.space_md, 0))
        self._composed_preview_frame = tk.Frame(pane, bg="black")
        self._composed_preview_frame.pack(fill="both", expand=True, padx=scale.space_md, pady=scale.space_xs)

        preview_transport = tk.Frame(pane, bg=BG)
        preview_transport.pack(fill="x", padx=scale.space_md)
        self._preview_play_btn = tk.Button(preview_transport, text="▶  Play", command=self._toggle_preview_play,
                                            bg="#333", fg="white", relief="flat", font=("Courier", scale.font_md))
        self._preview_play_btn.pack(side="left")
        self._composed_lbl = tk.Label(pane, text=self._composed_preview_label_text, bg=BG, fg="#aaa",
                                       font=("Courier", scale.font_xs), anchor="w")
        self._composed_lbl.pack(fill="x", padx=scale.space_md)
        self._preview_time_lbl = tk.Label(pane, text="--:--:--.--- / --:--:--.---",
                                           bg=BG, fg="#777", font=("Courier", scale.font_xs), anchor="w")
        self._preview_time_lbl.pack(fill="x", padx=scale.space_md)
        self._preview_scrubber = Scrubber(pane)
        self._preview_scrubber.pack(fill="x", padx=scale.space_md, pady=(scale.space_xxs, scale.space_md))
        self._preview_scrubber.add_seek_callback(self._on_preview_seek)
        self._preview_scrubber.set_locked(not self._has_composed_preview)

    def _update_empty_state(self):
        """Text overlays, sounds, and the output-canvas-source row are
        meaningless before any clip exists (there's nothing to overlay
        text/sound on or size a canvas from), so they're hidden until the
        timeline has at least one clip. _overlay_col only ever holds the
        first four, in this order, so forgetting and re-packing them together
        keeps their relative order correct without needing a stable before=
        anchor among them. _canvas_row lives in timeline_controls_row instead
        (a different parent), so it's handled separately -- pack() with no
        options just re-appends it after whatever's already packed there
        (outro_row), which is fine since that's never re-packed itself."""
        scale = get_scale()
        empty = not self.project.clips
        self._text_header.pack_forget()
        self._text_overlay_list.pack_forget()
        self._overlay_sep.pack_forget()
        self._sound_section.pack_forget()
        self._canvas_row.pack_forget()
        if not empty:
            self._text_header.pack(fill="x", pady=(0, 0))
            self._text_overlay_list.pack(fill="x", pady=scale.space_xs)
            self._overlay_sep.pack(fill="x", pady=scale.space_sm)
            self._sound_section.pack(fill="both", expand=True)
            self._canvas_row.pack(side="left")

    def _open_edit_tab(self, title: str, factory):
        """Opens factory(container, on_close) as a tab in the right-side
        Notebook, in place of what used to be a separate popup window.
        Replaces any edit tab already open -- only one edit session at a
        time. factory must build its content into `container` and return
        the editor instance, so it can be torn down (mpv player terminated,
        pending debounce jobs cancelled) if force-replaced or the tab is
        rebuilt out from under it."""
        self._discard_edit_tab()
        frame = tk.Frame(self._right_notebook, bg=BG)
        editor = factory(frame, self._return_to_current_view)
        editor.pack(fill="both", expand=True)
        self._right_notebook.add(frame, text=title)
        self._edit_tab_frame = frame
        self._edit_tab_editor = editor
        self._right_notebook.select(frame)

    def _discard_edit_tab(self):
        """Tears down and removes the edit tab without navigating anywhere
        -- used both to make room for a freshly-opened edit and during a
        scale-profile rebuild, where the whole widget tree is about to be
        destroyed anyway and only the editor's non-Tk resources (an mpv
        subprocess, a pending after() job) need explicit cleanup first."""
        if self._edit_tab_frame is None:
            return
        try:
            self._edit_tab_editor.teardown()
        except Exception:
            log.exception("Timeline: error tearing down edit tab")
        self._right_notebook.forget(self._edit_tab_frame)
        self._edit_tab_frame.destroy()
        self._edit_tab_frame = None
        self._edit_tab_editor = None

    def _return_to_current_view(self):
        """Passed to editors as on_close -- called after the editor has
        already torn itself down (Apply or Close was clicked), so this just
        removes the tab and switches back to Playback."""
        if self._edit_tab_frame is not None:
            self._right_notebook.forget(self._edit_tab_frame)
            self._edit_tab_frame.destroy()
            self._edit_tab_frame = None
            self._edit_tab_editor = None
        self._right_notebook.select(self._current_view_frame)

    def rebuild(self):
        """Tears down and rebuilds this tab's widgets from the current UI
        scale profile, restoring folder/library/timeline/text-overlay state.
        The caller is responsible for re-invoking on_show() afterward if this
        tab is the active one, so the players re-embed into the fresh frames."""
        self._discard_edit_tab()
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        if self._lib_input_dir is not None:
            self._lib_in_var.set(f"Clips source (input): {self._lib_input_dir}")
        if self._lib_output_dir is not None:
            self._lib_out_var.set(f"Clips source (output): {self._lib_output_dir}")
        if self._lib_input_dir is not None or self._lib_output_dir is not None:
            self._refresh_library()
        self._sync_timeline_clips()
        for i in range(len(self.project.clips)):
            self._request_thumbnail(i)
        self._refresh_text_overlay_list()
        self._refresh_sound_list()
        self._add_text_btn.config(state="normal" if self._has_composed_preview else "disabled")

    # -------------------------------------------------------------- folders --

    def _pick_lib_input(self):
        self._stop_active_work()
        default = str(self.state.clip_input_dir) if self.state.clip_input_dir else None

        def done(d: Path | None):
            self._resume_polling()
            if not d:
                return
            self._lib_input_dir = d
            self._lib_in_var.set(f"Clips source (input): {d}")
            self._refresh_library()

        ask_directory(self, done, title="Select clips (input) folder", initialdir=default or "")

    def _pick_lib_output(self):
        self._stop_active_work()
        default = str(self.state.clip_output_dir) if self.state.clip_output_dir else None

        def done(d: Path | None):
            self._resume_polling()
            if not d:
                return
            self._lib_output_dir = d
            self._lib_out_var.set(f"Clips source (output): {d}")
            self._refresh_library()

        ask_directory(self, done, title="Select clips (output) folder", initialdir=default or "")

    def apply_chained_defaults(self):
        changed = False
        if self._lib_input_dir is None and self.state.clip_input_dir is not None:
            self._lib_input_dir = self.state.clip_input_dir
            self._lib_in_var.set(f"Clips source (input): {self._lib_input_dir}")
            changed = True
        if self._lib_output_dir is None and self.state.clip_output_dir is not None:
            self._lib_output_dir = self.state.clip_output_dir
            self._lib_out_var.set(f"Clips source (output): {self._lib_output_dir}")
            changed = True
        if changed:
            self._refresh_library()

    def _refresh_library(self):
        self._library_items = []
        for tag, d in (("in", self._lib_input_dir), ("out", self._lib_output_dir)):
            if d and d.exists():
                files = list(d.iterdir())
                videos = sorted(p for p in files if p.suffix.lower() == ".mp4")
                images = sorted(p for p in files if p.suffix.lower() in IMAGE_EXTS)
                sounds = sorted(p for p in files if p.suffix.lower() in AUDIO_EXTS)
                for f in videos:
                    self._library_items.append((f"[{tag}][vid] {f.name}", f))
                for f in images:
                    self._library_items.append((f"[{tag}][img] {f.name}", f))
                for f in sounds:
                    self._library_items.append((f"[{tag}][snd] {f.name}", f))

        self._library.clear()
        self._selected_lib_index = None
        for i, (label, path) in enumerate(self._library_items):
            row = tk.Frame(self._library.body, bg=BG)
            row.pack(fill="x")
            btn = tk.Radiobutton(
                row, text=label, value=i, variable=self._lib_selection_var(),
                command=lambda i=i: self._select_library_item(i),
                bg=BG, fg="white", selectcolor="#333", activebackground=BG,
                activeforeground="white", font=("Courier", get_scale().font_sm), anchor="w",
            )
            btn.pack(fill="x", anchor="w")
        log.info("Timeline library: %d clips found", len(self._library_items))

    def _lib_selection_var(self):
        if not hasattr(self, "_lib_var"):
            self._lib_var = tk.IntVar(value=-1)
        return self._lib_var

    def _select_library_item(self, index: int):
        self._selected_lib_index = index
        _, path = self._library_items[index]
        self._preview_source = path
        self._preview_lbl.config(text=f"Previewing: {path.name}")

        is_img = path.suffix.lower() in IMAGE_EXTS
        self.player.load(path, is_image=is_img)
        log.info("Timeline: previewing %s", path.name)

    def _add_selected(self):
        if self._selected_lib_index is None:
            self._status_lbl.config(text="Select a clip in the library first.")
            return
        _, path = self._library_items[self._selected_lib_index]

        if path.suffix.lower() in AUDIO_EXTS:
            self._add_sound_from_library(path)
            return

        is_first_clip = not self.project.clips
        is_img = path.suffix.lower() in IMAGE_EXTS

        try:
            if is_img:
                with Image.open(path) as img:
                    width, height = img.size
                duration_s = DEFAULT_IMAGE_DURATION
                clip = TimelineClip(source=str(path), in_s=0.0, out_s=duration_s, is_image=True)
            else:
                probe = ffmpeg_ops.probe_video(path)
                width, height = probe.width, probe.height
                duration_s = probe.duration_s
                clip = TimelineClip(source=str(path), in_s=0.0, out_s=duration_s)
        except Exception:
            log.exception("Timeline: failed to add %s to timeline (unreadable/corrupt source?)", path)
            self._status_lbl.config(text=f"Could not add {path.name} (see log).")
            return

        self.project.clips.append(clip)
        self._sync_timeline_clips()
        self._request_thumbnail(len(self.project.clips) - 1)
        self._update_empty_state()

        if is_first_clip and width and height:
            # h264/h265 require even dimensions -- round down rather than
            # reject, since off-by-one almost never matters visually.
            width, height = width // 2 * 2, height // 2 * 2
            self.project.canvas_w = width
            self.project.canvas_h = height
            self._canvas_source_var.set(self._canvas_source_labels()[0])
            self._update_canvas_size_label()
            log.info("Timeline: output canvas defaulted to %dx%d (first clip's resolution)", width, height)

        log.info("Timeline: added %s (%.2fs)", path.name, duration_s)

    def _add_sound_from_library(self, path: Path):
        """Places a new sound at the composed preview's current playhead
        position (0 if nothing's been composed yet), defaulting its segment
        length to the source file's own full duration."""
        try:
            duration_s = max(0.05, ffmpeg_ops.probe_video(path).duration_s)
        except Exception:
            log.exception("Timeline: failed to probe sound duration for %s", path.name)
            duration_s = 5.0
        start_s = self.preview_player.current_pos_s() if self._has_composed_preview else 0.0
        sound = SoundClip(source=str(path), start_s=start_s, clip_duration_s=duration_s)
        self.project.sound_clips.append(sound)
        self._refresh_sound_list()
        log.info("Timeline: added sound %s @ %.2fs", path.name, start_s)

    # -------------------------------------------------------------- tab focus --

    def on_show(self):
        self.player.set_embed_widget(self._lib_preview_frame)
        self.player.set_tick_callback(self._on_tick)
        self.preview_player.set_embed_widget(self._composed_preview_frame)
        self.preview_player.set_tick_callback(self._on_preview_tick)
        self.apply_chained_defaults()

    def on_hide(self):
        self.player.set_tick_callback(None)
        self.preview_player.set_tick_callback(None)

    def _on_tick(self, pos_frac, duration_s, paused):
        self._scrubber.set_playhead(pos_frac)
        self._play_btn.config(text="⏸  Pause" if not paused else "▶  Play")
        self._lib_time_lbl.config(text=f"{fmt_time(pos_frac * duration_s * 1000)} / {fmt_time(duration_s * 1000)}")

    def _toggle_play(self):
        self.player.play_pause()

    def _on_lib_seek(self, frac: float):
        self.player.seek(frac)
        d = self.player.duration_s
        if d > 0:
            self._lib_time_lbl.config(text=f"{fmt_time(frac * d * 1000)} / {fmt_time(d * 1000)}")

    def _on_preview_tick(self, pos_frac, duration_s, paused):
        self._preview_scrubber.set_playhead(pos_frac)
        self._preview_play_btn.config(text="⏸  Pause" if not paused else "▶  Play")
        self._preview_time_lbl.config(
            text=f"{fmt_time(pos_frac * duration_s * 1000)} / {fmt_time(duration_s * 1000)}")

    def _on_preview_seek(self, frac: float):
        self.preview_player.seek(frac)
        d = self.preview_player.duration_s
        if d > 0:
            self._preview_time_lbl.config(text=f"{fmt_time(frac * d * 1000)} / {fmt_time(d * 1000)}")

    def _toggle_preview_play(self):
        self.preview_player.play_pause()

    # ----------------------------------------------------------- text overlays --

    def _open_text_editor(self):
        self._open_edit_tab(
            "Add Text Overlay",
            lambda container, on_close: TextOverlayEditor(
                container, self.project, self.preview_player, self._on_text_overlay_added, on_close))

    def _on_text_overlay_added(self, overlay: TextOverlay):
        self.project.text_overlays.append(overlay)
        self._refresh_text_overlay_list()

    def _edit_text_overlay(self, index: int):
        if not (0 <= index < len(self.project.text_overlays)):
            return
        overlay = self.project.text_overlays[index]
        self._open_edit_tab(
            "Edit Text Overlay",
            lambda container, on_close: TextOverlayEditor(
                container, self.project, self.preview_player,
                on_save=lambda o: self._on_text_overlay_updated(index, o),
                on_close=on_close, existing=overlay))

    def _on_text_overlay_updated(self, index: int, overlay: TextOverlay):
        if 0 <= index < len(self.project.text_overlays):
            self.project.text_overlays[index] = overlay
            self._refresh_text_overlay_list()

    def _remove_text_overlay(self, index: int):
        if 0 <= index < len(self.project.text_overlays):
            removed = self.project.text_overlays.pop(index)
            self._refresh_text_overlay_list()
            log.info("Timeline: removed text overlay '%s'", (removed.text or "")[:30])

    def _duplicate_text_overlay(self, index: int):
        """Inserts an identical copy right after the original, so a family of
        similar overlays (same style, tweaked text/timing) can be built by
        duplicating and editing rather than re-entering every field."""
        if not (0 <= index < len(self.project.text_overlays)):
            return
        duplicate = copy.deepcopy(self.project.text_overlays[index])
        self.project.text_overlays.insert(index + 1, duplicate)
        self._refresh_text_overlay_list()
        log.info("Timeline: duplicated text overlay '%s'", (duplicate.text or "")[:30])

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _notify_edit_tab_preview_recomposed(self):
        """Build Preview / Render Final Video can be clicked while a Text
        Overlay or Sound edit tab is open (the composed-preview controls live
        outside the right-side Notebook) -- if so, preview_player.load() just
        swapped the video out from under it: any baked preview was trimmed
        from the now-replaced file, and (for text) the live preview's grabbed
        frame is stale too."""
        if isinstance(self._edit_tab_editor, (TextOverlayEditor, SoundClipEditor)):
            self._edit_tab_editor.invalidate_composed_preview()

    def _edit_sound_clip(self, index: int):
        if not (0 <= index < len(self.project.sound_clips)):
            return
        sound = self.project.sound_clips[index]
        self._open_edit_tab(
            "Edit Sound",
            lambda container, on_close: SoundClipEditor(
                container, self.project, self.preview_player, sound.source,
                on_save=lambda s: self._on_sound_clip_updated(index, s),
                on_close=on_close, existing=sound))

    def _on_sound_clip_updated(self, index: int, sound: SoundClip):
        if 0 <= index < len(self.project.sound_clips):
            self.project.sound_clips[index] = sound
            self._refresh_sound_list()

    def _remove_sound_clip(self, index: int):
        if 0 <= index < len(self.project.sound_clips):
            removed = self.project.sound_clips.pop(index)
            self._refresh_sound_list()
            log.info("Timeline: removed sound clip '%s'", Path(removed.source).name)

    def _refresh_sound_list(self):
        scale = get_scale()
        self._sound_list.clear()
        total_s = self.preview_player.duration_s if self._has_composed_preview else 0.0
        for i, sound in enumerate(self.project.sound_clips):
            row = tk.Frame(self._sound_list.body, bg=BG)
            row.pack(fill="x", pady=1)
            end_s = sound.end_s(total_s) if total_s > 0 else sound.start_s + sound.clip_duration_s
            tk.Label(row, text=f"{Path(sound.source).name}  {sound.start_s:.2f}s-{end_s:.2f}s",
                     bg=BG, fg="white", font=("Courier", scale.font_sm), anchor="w"
                     ).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="Remove", command=lambda i=i: self._remove_sound_clip(i),
                      bg="#cc4444", fg="white", relief="flat", font=("Courier", scale.font_xs)).pack(side="right")
            # Same gate as text overlays' Edit -- the bake preview needs a
            # composed timeline video to trim the sound's actual segment from.
            tk.Button(row, text="Edit", command=lambda i=i: self._edit_sound_clip(i),
                      bg="#4a9eff", fg="white", relief="flat", font=("Courier", scale.font_xs),
                      state="normal" if self._has_composed_preview else "disabled"
                      ).pack(side="right", padx=scale.space_xs)

    def _refresh_text_overlay_list(self):
        scale = get_scale()
        self._text_overlay_list.clear()
        for i, overlay in enumerate(self.project.text_overlays):
            row = tk.Frame(self._text_overlay_list.body, bg=BG)
            row.pack(fill="x", pady=1)
            snippet = (overlay.text or "").replace("\n", " ")[:24]
            tk.Label(row, text=f"\"{snippet}\"  {overlay.start_s:.2f}s-{overlay.end_s:.2f}s",
                     bg=BG, fg="white", font=("Courier", scale.font_sm), anchor="w"
                     ).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="Remove", command=lambda i=i: self._remove_text_overlay(i),
                      bg="#cc4444", fg="white", relief="flat", font=("Courier", scale.font_xs)).pack(side="right")
            # Editing needs a frame to grab from the composed preview (see
            # TextOverlayEditor._grab_frame) -- same gate as "Add Text Overlay...".
            tk.Button(row, text="Edit", command=lambda i=i: self._edit_text_overlay(i),
                      bg="#4a9eff", fg="white", relief="flat", font=("Courier", scale.font_xs),
                      state="normal" if self._has_composed_preview else "disabled"
                      ).pack(side="right", padx=scale.space_xs)
            tk.Button(row, text="Duplicate", command=lambda i=i: self._duplicate_text_overlay(i),
                      bg="#333", fg="white", relief="flat", font=("Courier", scale.font_xs)
                      ).pack(side="right", padx=(0, scale.space_xs))

    # --------------------------------------------------------- timeline edits --

    def _on_reorder(self, from_idx, to_idx):
        clip = self.project.clips.pop(from_idx)
        self.project.clips.insert(to_idx, clip)
        self._sync_timeline_clips()
        for i in range(len(self.project.clips)):
            self._request_thumbnail(i)
        log.info("Timeline: reordered clip %d -> %d", from_idx, to_idx)

    def _on_remove(self, index):
        if 0 <= index < len(self.project.clips):
            removed = self.project.clips.pop(index)
            self._sync_timeline_clips()
            for i in range(index, len(self.project.clips)):
                self._request_thumbnail(i)
            self._update_empty_state()
            log.info("Timeline: removed %s from timeline", Path(removed.source).name)

    def _on_transition_change(self, clip: TimelineClip, transition_type, duration, direction="left"):
        clip.transition_type = transition_type
        clip.transition_duration = duration
        clip.transition_direction = direction
        log.info("Timeline: transition set to %s (%.2fs, direction=%s)",
                  transition_type, duration, direction)

    def _on_edit_clip(self, index: int):
        if not (0 <= index < len(self.project.clips)):
            return
        clip = self.project.clips[index]
        if clip.is_image:
            self._open_edit_tab(
                f"Edit: {clip.path.name}",
                lambda container, on_close: ImageClipEditor(
                    container, clip, self.project.canvas_w, self.project.canvas_h,
                    on_change=lambda: self._on_clip_edited(index), on_close=on_close))
        else:
            self._open_edit_tab(
                f"Edit: {clip.path.name}",
                lambda container, on_close: ClipTrimEditor(
                    container, clip, on_change=lambda: self._on_clip_edited(index), on_close=on_close))

    def _on_clip_edited(self, index: int):
        self._sync_timeline_clips()
        self._request_thumbnail(index)
        log.info("Timeline: clip %d trim updated via edit window", index)

    def _request_thumbnail(self, index):
        if index >= len(self.project.clips):
            return
        clip = self.project.clips[index]
        mid = clip.in_s + clip.duration / 2

        def run():
            try:
                if clip.is_image:
                    with Image.open(clip.path) as img:
                        img.thumbnail((160, 160))
                        buf = io.BytesIO()
                        img.convert("RGB").save(buf, format="PNG")
                        data = buf.getvalue()
                else:
                    data = ffmpeg_ops.extract_thumbnail(clip.path, mid)
            except Exception:
                log.exception("Timeline: failed to build thumbnail for clip %d (%s)", index, clip.path)
                return
            self.after(0, lambda: self._timeline.set_thumbnail(index, data))

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _dims_for_clip(clip: TimelineClip) -> tuple[int, int]:
        """Native resolution of a clip's source file (PIL for images, ffprobe
        for video), rounded down to even -- h264/h265 require even dimensions,
        and an odd native size (not unusual for phone screenshots) would
        otherwise crash every later ffmpeg encode of a canvas sized from it."""
        if clip.is_image:
            with Image.open(clip.path) as img:
                w, h = img.size
        else:
            probe = ffmpeg_ops.probe_video(clip.path)
            w, h = probe.width, probe.height
        return w // 2 * 2, h // 2 * 2

    def _sync_timeline_clips(self):
        """Every mutation of self.project.clips should go through this (instead
        of calling self._timeline.set_clips directly) so the canvas-source
        dropdown's options never fall out of sync with the actual clip list."""
        self._timeline.set_clips(self.project.clips)
        self._refresh_canvas_source_dropdown()

    def _canvas_source_labels(self) -> list[str]:
        return [f"{i + 1}: {Path(c.source).name}" for i, c in enumerate(self.project.clips)]

    def _refresh_canvas_source_dropdown(self):
        labels = self._canvas_source_labels()
        self._canvas_source_combo.config(values=labels)
        if self._canvas_source_var.get() not in labels:
            self._canvas_source_var.set("")

    def _update_canvas_size_label(self):
        self._canvas_size_lbl.config(text=f"-> {self.project.canvas_w}x{self.project.canvas_h}")

    def _on_canvas_source_selected(self, _event=None):
        label = self._canvas_source_var.get()
        labels = self._canvas_source_labels()
        if label not in labels:
            return
        clip = self.project.clips[labels.index(label)]
        try:
            width, height = self._dims_for_clip(clip)
        except Exception:
            log.exception("Timeline: failed to read dimensions for canvas source %s", clip.path.name)
            self._status_lbl.config(text=f"Couldn't read dimensions of {clip.path.name} (see console).")
            return
        if not width or not height:
            return
        self.project.canvas_w, self.project.canvas_h = width, height
        self._update_canvas_size_label()
        log.info("Timeline: output canvas set to %dx%d (from clip %s)", width, height, clip.path.name)

    # -------------------------------------------------------------- rendering --

    def _stop_active_work(self):
        """Cancels any in-progress render and pauses both players. Called
        before opening any native file dialog (Save/Load/Browse) -- a render
        or player-load completing while such a dialog is open has been
        observed to hang the app (the dialog disables the owner window at the
        OS level while an embedded mpv player tries to (re)create itself)."""
        if self._rendering:
            self._cancel_render()
        self.player.pause()
        self.preview_player.pause()
        self.player.suspend_polling()
        self.preview_player.suspend_polling()

    def _resume_polling(self):
        """Pairs with _stop_active_work(); call once the native dialog it
        preceded has returned."""
        self.player.resume_polling()
        self.preview_player.resume_polling()

    def _on_build_preview_clicked(self):
        if self._rendering:
            self._cancel_render()
            return
        # Stop/cancel whatever is currently showing in the composed-preview
        # window before kicking off a fresh build.
        self.preview_player.pause()
        self._render(proxy=True)

    def _cancel_render(self):
        self._render_proc_holder["cancelled"] = True
        proc = self._render_proc_holder.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.exception("Timeline: failed to terminate render process (already exited?)")
        self._rendering = False
        self._build_btn.config(text="Build Preview", bg="#7a5fff")
        self._status_lbl.config(text="Cancelled.")
        log.info("Timeline: preview build cancelled by user")

    def _render(self, proxy: bool):
        if not self.project.clips:
            self._status_lbl.config(text="Timeline is empty.")
            return

        if proxy:
            self._start_render(proxy, self._tmp_dir / "proxy_preview.mp4")
            return

        self._stop_active_work()

        def done(out_path: Path | None):
            self._resume_polling()
            if not out_path:
                return
            self._start_render(proxy, out_path)

        ask_save_filename(self, done, title="Render final video as...",
                           defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])

    def _start_render(self, proxy: bool, out_path: Path):
        clips_snapshot = list(self.project.clips)
        canvas_w, canvas_h = self.project.canvas_w, self.project.canvas_h
        proxy_height = int(self._proxy_height_var.get())

        self._progress["value"] = 0
        self._status_lbl.config(text="Rendering proxy..." if proxy else "Rendering final video...")

        proc_holder: dict = {}
        if proxy:
            self._render_proc_holder = proc_holder
            self._rendering = True
            self._build_btn.config(text="Cancel", bg="#cc4444")

        def on_progress(frac, _label):
            self.after(0, lambda: self._progress.config(value=frac * 100))

        text_overlays_snapshot = list(self.project.text_overlays)
        sound_clips_snapshot = list(self.project.sound_clips)

        def run():
            # render_timeline can raise before ever starting an ffmpeg subprocess
            # (e.g. a clip/image/sound source that no longer exists on disk) --
            # without this try/except, that exception would just kill this thread
            # silently (Python logs it to the console but doesn't surface it),
            # leaving the UI stuck on "Rendering..." forever with no feedback.
            err: Exception | None = None
            try:
                ok = ffmpeg_ops.render_timeline(clips_snapshot, canvas_w, canvas_h, out_path,
                                                 proxy=proxy, proxy_height=proxy_height,
                                                 progress_cb=on_progress, proc_holder=proc_holder,
                                                 text_overlays=text_overlays_snapshot,
                                                 sound_clips=sound_clips_snapshot,
                                                 intro_type=self.project.intro_transition_type,
                                                 intro_duration=self.project.intro_transition_duration,
                                                 intro_direction=self.project.intro_transition_direction,
                                                 outro_type=self.project.outro_transition_type,
                                                 outro_duration=self.project.outro_transition_duration,
                                                 outro_direction=self.project.outro_transition_direction)
            except Exception as e:  # noqa: BLE001 -- must not let this thread die silently
                log.exception("Timeline render crashed before/during ffmpeg")
                ok, err = False, e

            def done():
                if proxy:
                    self._rendering = False
                    self._build_btn.config(text="Build Preview", bg="#7a5fff")
                if err is not None:
                    self._status_lbl.config(text=f"Render error: {err}")
                elif ok and proxy:
                    self._status_lbl.config(text="Preview updated.")
                    self._composed_preview_label_text = f"{out_path.name} (proxy)"
                    self._composed_lbl.config(text=self._composed_preview_label_text)
                    self._has_composed_preview = True
                    self._preview_scrubber.set_locked(False)
                    self._add_text_btn.config(state="normal")
                    self._refresh_text_overlay_list()
                    self._refresh_sound_list()
                    self.preview_player.load(out_path)
                    self._notify_edit_tab_preview_recomposed()
                elif ok:
                    self._status_lbl.config(text=f"Saved: {out_path.name}")
                    self._composed_preview_label_text = f"{out_path.name} (final)"
                    self._composed_lbl.config(text=self._composed_preview_label_text)
                    self._has_composed_preview = True
                    self._preview_scrubber.set_locked(False)
                    self._add_text_btn.config(state="normal")
                    self._refresh_text_overlay_list()
                    self._refresh_sound_list()
                    self.preview_player.load(out_path)
                    self._notify_edit_tab_preview_recomposed()
                elif not proc_holder.get("cancelled"):
                    self._status_lbl.config(text="Render failed (see console).")
            self.after(0, done)

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- persistence --

    def _save_project(self):
        """Every step here is wrapped defensively -- a Save action must never
        be able to crash or freeze the app, only ever report failure via the
        status label and leave the app in whatever state it was already in."""
        self._stop_active_work()

        def on_path(path: Path | None):
            self._resume_polling()
            if not path:
                return
            self._do_save(path)

        try:
            ask_save_filename(self, on_path, message="",
                               title="Save project as...", defaultextension=".json",
                               filetypes=[("Project JSON", "*.json")])
        except Exception:
            self._resume_polling()
            log.exception("Timeline: save dialog failed")
            self._safe_status("Save failed (see console).")

    def _do_save(self, path: Path):
        # Plain JSON write, fast enough not to need the background-thread +
        # blocking-status-window treatment run_blocked gives actual ffmpeg work.
        project = self.project
        project.lib_input_dir = str(self._lib_input_dir) if self._lib_input_dir else None
        project.lib_output_dir = str(self._lib_output_dir) if self._lib_output_dir else None
        try:
            save_project(project, path)
            self._safe_status(f"Project saved: {path.name}")
        except Exception:
            log.exception("Timeline: save failed")
            self._safe_status("Save failed (see console).")

    def _load_project(self):
        """Same defensive wrapping as _save_project -- a failed load reports
        via the status label and leaves the current in-memory project intact
        rather than crashing or freezing."""
        self._stop_active_work()

        def on_path(path: Path | None):
            self._resume_polling()
            if not path:
                return
            self._do_load(path)

        try:
            ask_open_filename(self, on_path, message="",
                               title="Load project...", filetypes=[("Project JSON", "*.json")])
        except Exception:
            self._resume_polling()
            log.exception("Timeline: load dialog failed")
            self._safe_status("Load failed (see console).")

    def _do_load(self, path: Path):
        # Plain JSON read, fast enough not to need the background-thread +
        # blocking-status-window treatment run_blocked gives actual ffmpeg work.
        try:
            loaded = load_project(path)
        except Exception:
            log.exception("Timeline: load failed")
            self._safe_status("Load failed (see console) -- current project unchanged.")
            return
        try:
            # An open edit tab (Text Overlay/Image/Trim) refers to an
            # object from the project being replaced -- discard it before
            # swapping self.project out from under it.
            self._discard_edit_tab()
            self.project = loaded
            if self.project.lib_input_dir:
                self._lib_input_dir = Path(self.project.lib_input_dir)
                self._lib_in_var.set(f"Clips source (input): {self._lib_input_dir}")
            if self.project.lib_output_dir:
                self._lib_output_dir = Path(self.project.lib_output_dir)
                self._lib_out_var.set(f"Clips source (output): {self._lib_output_dir}")
            if self.project.lib_input_dir or self.project.lib_output_dir:
                self._refresh_library()
            self._update_canvas_size_label()
            self._intro_type_var.set(TRANSITION_LABELS.get(self.project.intro_transition_type, "Hard Cut"))
            self._intro_dur_var.set(str(self.project.intro_transition_duration))
            self._intro_dir_var.set(self.project.intro_transition_direction)
            self._rebuild_intro_dynamic()
            self._outro_type_var.set(TRANSITION_LABELS.get(self.project.outro_transition_type, "Hard Cut"))
            self._outro_dur_var.set(str(self.project.outro_transition_duration))
            self._outro_dir_var.set(self.project.outro_transition_direction)
            self._rebuild_outro_dynamic()
            self._sync_timeline_clips()
            for i in range(len(self.project.clips)):
                self._request_thumbnail(i)
            self._update_empty_state()
            self._has_composed_preview = False
            self._composed_preview_label_text = "Not rendered yet"
            self._composed_lbl.config(text=self._composed_preview_label_text)
            self._preview_scrubber.set_locked(True)
            self._add_text_btn.config(state="disabled")
            # After _has_composed_preview so the rebuilt rows' Edit buttons
            # come up disabled, not carrying over the old project's state.
            self._refresh_text_overlay_list()
            self._refresh_sound_list()
        except Exception:
            # The project data itself already loaded fine at this point --
            # only refreshing the UI to reflect it failed, which shouldn't
            # be possible, but this is the "must never crash" persistence
            # path, so nothing here is trusted to be exception-free.
            log.exception("Timeline: loaded project but failed to refresh the UI for it")
            self._safe_status(f"Project loaded ({path.name}) but the view may be stale -- "
                               f"consider switching tabs or restarting.")
            return
        self._safe_status(f"Project loaded: {path.name}")

    def _safe_status(self, text: str):
        """Even updating the status label is wrapped -- if this tab is mid-
        rebuild (scale-profile switch) the label widget could momentarily
        not exist; that must never surface as a crash for a background
        status message."""
        try:
            self._status_lbl.config(text=text)
        except Exception:
            log.exception("Timeline: failed to update status label with %r", text)
