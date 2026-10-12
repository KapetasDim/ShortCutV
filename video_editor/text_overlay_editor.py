from __future__ import annotations

import io
import tempfile
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import ttk
from typing import Callable

from PIL import Image, ImageTk

from . import ffmpeg_ops, fonts_catalog, text_render
from .crop_box import CropBoxController, CURSOR_BY_HANDLE
from .curves import CURVE_PRESETS, eval_curve, fade_opacity
from .logging_config import get_logger
from .player import SharedPlayer
from .project import TextOverlay, TimelineProject
from .ui_scale import get_scale
from .widgets import InlineColorPicker, Scrubber, _handle_points

log = get_logger()

BG = "#1e1e1e"


class TextOverlayEditor(tk.Frame):
    """The default preview composites the animated text over a single grabbed
    frame -- fine for checking position/style, but it doesn't show the actual
    footage moving behind the text. "Bake Preview" renders the real timeline
    video chunk the overlay plays over (trimmed from the composed preview at
    overlay.start_s/duration_s) with the text baked on
    (ffmpeg_ops.render_text_overlay_preview) and switches to playing that back
    through the same mpv-embedded transport (Play button, real seeking) used
    elsewhere in the app. Embedded as a tab in the Timeline tab's right-side
    Notebook rather than a popup window; on_close is invoked after Add/Update
    or Close to hand control back to whoever opened this tab (see
    TimelineTab._open_edit_tab)."""

    def __init__(self, parent, project: TimelineProject, preview_player: SharedPlayer,
                 on_save: Callable[[TextOverlay], None], on_close: Callable[[], None],
                 existing: TextOverlay | None = None):
        super().__init__(parent, bg=BG)

        self.project = project
        self.preview_player = preview_player
        self.on_save = on_save
        self.on_close = on_close
        self._existing = existing
        self._base_frame: Image.Image | None = None
        self._preview_imgtk = None
        self._update_job = None
        self._preview_t_s = 0.0  # scrub position within the overlay's own duration
        self._preview_max_w = get_scale().preview_max_w_text
        self._baked = False
        self.player: SharedPlayer | None = None  # created lazily on first bake
        self._last_bake_path: Path | None = None  # deleted once superseded/torn down
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_text_preview"
        self._tmp_dir.mkdir(exist_ok=True)

        # The placement box (position + size, in project-canvas pixel space)
        # is edited by dragging/resizing directly on the preview -- rotation
        # has no box handle, it stays input-field-only (see _rot_var below).
        e = existing
        default_x = e.x if e else self.project.canvas_w / 2
        default_y = e.y if e else self.project.canvas_h * 0.33
        box_w = e.box_w if e else TextOverlay.__dataclass_fields__["box_w"].default
        box_h = e.box_h if e else TextOverlay.__dataclass_fields__["box_h"].default
        self._box_ctrl = CropBoxController(self.project.canvas_w, self.project.canvas_h)
        self._box_ctrl.box.left = default_x - box_w / 2
        self._box_ctrl.box.top = default_y - box_h / 2
        self._box_ctrl.box.right = default_x + box_w / 2
        self._box_ctrl.box.bottom = default_y + box_h / 2
        self._box_ctrl.clamp()
        self._preview_canvas_scale = 1.0
        self._suspend_xy_sync = False

        self._build_ui()
        # Editing an existing overlay should preview the frame where it
        # actually starts, not whatever the main preview happens to be
        # scrubbed to; adding a new one still grabs the current preview frame.
        self._grab_frame(existing.start_s if existing else None)
        self._update_preview()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        ui = get_scale()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=ui.space_lg, pady=ui.space_lg)

        left = tk.Frame(body, bg=BG)
        left.pack(side="top", fill="x")
        right = tk.Frame(body, bg=BG)
        right.pack(side="top", fill="both", expand=True, pady=(ui.space_md, 0))

        def row(label, widget_factory, default=""):
            r = tk.Frame(left, bg=BG)
            r.pack(fill="x", pady=ui.space_xxs)
            tk.Label(r, text=label, bg=BG, fg="white", font=("Courier", ui.font_sm),
                     width=14, anchor="w").pack(side="left")
            w = widget_factory(r)
            w.pack(side="left", fill="x", expand=True)
            return w

        e = self._existing

        tk.Label(left, text="Text:", bg=BG, fg="white", font=("Courier", ui.font_sm), anchor="w").pack(fill="x")
        self._text_widget = tk.Text(left, height=3, width=30, font=("Courier", ui.font_md))
        self._text_widget.pack(fill="x", pady=(0, ui.space_sm))
        if e:
            self._text_widget.insert("1.0", e.text)
        self._text_widget.bind("<KeyRelease>", self._on_field_change)

        time_row = tk.Frame(left, bg=BG)
        time_row.pack(fill="x", pady=ui.space_xxs)
        tk.Label(time_row, text="Start (s):", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=14, anchor="w").pack(side="left")
        start_default = f"{e.start_s:.2f}" if e else f"{self.preview_player.current_pos_s():.2f}"
        self._start_var = tk.StringVar(value=start_default)
        tk.Entry(time_row, textvariable=self._start_var, width=8,
                 font=("Courier", ui.font_sm)).pack(side="left")
        tk.Button(time_row, text="Use current preview time", command=self._use_current_time,
                  bg="#333", fg="white", relief="flat", font=("Courier", ui.font_xs)).pack(side="left",
                                                                                             padx=ui.space_xs)

        self._duration_var = tk.StringVar(value=str(e.duration_s) if e else "2.0")
        row("Duration (s):", lambda r: tk.Entry(r, textvariable=self._duration_var, width=8,
                                                  font=("Courier", ui.font_sm)))

        box_cx, box_cy = self._box_ctrl.box.center
        self._x_var = tk.StringVar(value=str(round(box_cx)))
        self._y_var = tk.StringVar(value=str(round(box_cy)))
        self._rot_var = tk.StringVar(value=str(e.rotation_deg) if e else "0")
        row("X:", lambda r: tk.Entry(r, textvariable=self._x_var, width=8, font=("Courier", ui.font_sm)))
        row("Y:", lambda r: tk.Entry(r, textvariable=self._y_var, width=8, font=("Courier", ui.font_sm)))
        self._wrap_var = tk.BooleanVar(value=e.wrap_enabled if e else True)
        wrap_row = tk.Frame(left, bg=BG)
        wrap_row.pack(fill="x", pady=ui.space_xxs)
        tk.Checkbutton(wrap_row, text="Wrap text to box", variable=self._wrap_var, command=self._on_field_change,
                        bg=BG, fg="white", selectcolor="#333", activebackground=BG,
                        activeforeground="white", font=("Courier", ui.font_sm)).pack(side="left")
        row("Rotation (deg):", lambda r: tk.Entry(r, textvariable=self._rot_var, width=8,
                                                     font=("Courier", ui.font_sm)))

        self._font_var = tk.StringVar(value=e.font_family if e else "Arial")
        fonts = fonts_catalog.list_available_fonts()
        font_combo = row("Font:", lambda r: ttk.Combobox(r, textvariable=self._font_var, values=fonts,
                                                           state="readonly", width=16,
                                                           font=("Courier", ui.font_sm)))
        font_combo.bind("<<ComboboxSelected>>", self._on_field_change)

        self._size_var = tk.StringVar(value=str(e.font_size) if e else "64")
        row("Size:", lambda r: tk.Entry(r, textvariable=self._size_var, width=8, font=("Courier", ui.font_sm)))

        # Text color / Style / Background / Outline / Size curve / Fade rows don't
        # stretch their controls full-width (unlike the row()-built ones above),
        # leaving a wide blank strip to their right -- mid_right fills that with
        # the color picker instead of it sitting disconnected at the bottom.
        mid_section = tk.Frame(left, bg=BG)
        mid_section.pack(fill="x")
        mid_left = tk.Frame(mid_section, bg=BG)
        mid_left.pack(side="left")
        mid_right = tk.Frame(mid_section, bg=BG)
        mid_right.pack(side="left", fill="both", expand=True, padx=(ui.space_lg, 0))

        self._color = e.color if e else "#ffffff"
        self._bg_color = e.bg_color if e else "#000000"

        self._active_color_target = "text"
        self._color_picker = InlineColorPicker(mid_right, self._on_picker_change)
        self._color_picker.pack(anchor="ne")
        self._color_picker.set_color(self._color)

        color_row = tk.Frame(mid_left, bg=BG)
        color_row.pack(fill="x", pady=ui.space_xxs)
        tk.Label(color_row, text="Text color:", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=14, anchor="w").pack(side="left")
        self._color_btn = tk.Button(color_row, text="  ", bg=self._color, width=4,
                                     command=lambda: self._activate_color_target("text"), relief="flat",
                                     font=("Courier", ui.font_sm))
        self._color_btn.pack(side="left")

        style_row = tk.Frame(mid_left, bg=BG)
        style_row.pack(fill="x", pady=ui.space_xs)
        self._bold_var = tk.BooleanVar(value=e.bold if e else False)
        self._italic_var = tk.BooleanVar(value=e.italic if e else False)
        self._underline_var = tk.BooleanVar(value=e.underline if e else False)
        for text, var in (("Bold", self._bold_var), ("Italic", self._italic_var), ("Underline", self._underline_var)):
            tk.Checkbutton(style_row, text=text, variable=var, command=self._on_field_change,
                            bg=BG, fg="white", selectcolor="#333", activebackground=BG,
                            activeforeground="white", font=("Courier", ui.font_sm)).pack(side="left")

        bg_row = tk.Frame(mid_left, bg=BG)
        bg_row.pack(fill="x", pady=ui.space_xxs)
        tk.Label(bg_row, text="Background:", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=14, anchor="w").pack(side="left")
        self._bg_color_btn = tk.Button(bg_row, text="  ", bg=self._bg_color, width=4,
                                        command=lambda: self._activate_color_target("bg"), relief="flat",
                                        font=("Courier", ui.font_sm))
        self._bg_color_btn.pack(side="left")
        self._bg_opacity_var = tk.StringVar(value=str(round(e.bg_opacity * 100)) if e else "0")
        tk.Label(bg_row, text="opacity %:", bg=BG, fg="white",
                 font=("Courier", ui.font_sm)).pack(side="left", padx=(ui.space_md, 2))
        tk.Entry(bg_row, textvariable=self._bg_opacity_var, width=5,
                 font=("Courier", ui.font_sm)).pack(side="left")

        outline_row = tk.Frame(mid_left, bg=BG)
        outline_row.pack(fill="x", pady=ui.space_xxs)
        self._outline_var = tk.BooleanVar(value=e.outline_enabled if e else False)
        tk.Checkbutton(outline_row, text="Outline", variable=self._outline_var, command=self._on_field_change,
                        bg=BG, fg="white", selectcolor="#333", activebackground=BG,
                        activeforeground="white", font=("Courier", ui.font_sm), width=13, anchor="w"
                        ).pack(side="left")
        self._outline_color = e.outline_color if e else "#000000"
        self._outline_color_btn = tk.Button(outline_row, text="  ", bg=self._outline_color, width=4,
                                             command=lambda: self._activate_color_target("outline"), relief="flat",
                                             font=("Courier", ui.font_sm))
        self._outline_color_btn.pack(side="left")
        self._outline_width_var = tk.StringVar(value=str(e.outline_width) if e else "2")
        tk.Label(outline_row, text="width:", bg=BG, fg="white",
                 font=("Courier", ui.font_sm)).pack(side="left", padx=(ui.space_md, 2))
        tk.Entry(outline_row, textvariable=self._outline_width_var, width=5,
                 font=("Courier", ui.font_sm)).pack(side="left")

        curve_row = tk.Frame(mid_left, bg=BG)
        curve_row.pack(fill="x", pady=(ui.space_md, ui.space_xxs))
        tk.Label(curve_row, text="Size curve:", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=14, anchor="w").pack(side="left")
        self._curve_var = tk.StringVar(value="none")
        curve_labels = {key: label for key, (label, _params) in CURVE_PRESETS.items()}
        self._curve_label_to_key = {v: k for k, v in curve_labels.items()}
        self._curve_combo = ttk.Combobox(curve_row, values=list(curve_labels.values()),
                                          state="readonly", width=16, font=("Courier", ui.font_sm))
        self._curve_combo.set(curve_labels[e.curve_preset] if e and e.curve_preset in curve_labels
                               else curve_labels["none"])
        self._curve_combo.pack(side="left")
        self._curve_combo.bind("<<ComboboxSelected>>", self._on_curve_changed)

        self._param_frame = tk.Frame(mid_left, bg=BG)
        self._param_frame.pack(fill="x", pady=ui.space_xxs)
        self._param_vars: dict[str, tk.StringVar] = {}
        self._rebuild_param_fields()

        fade_row = tk.Frame(mid_left, bg=BG)
        fade_row.pack(fill="x", pady=ui.space_xxs)
        tk.Label(fade_row, text="Fade in/out (s):", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=14, anchor="w").pack(side="left")
        self._fade_in_var = tk.StringVar(value=str(e.fade_in_s) if e else "0.25")
        tk.Entry(fade_row, textvariable=self._fade_in_var, width=6,
                 font=("Courier", ui.font_sm)).pack(side="left")
        self._fade_out_var = tk.StringVar(value=str(e.fade_out_s) if e else "0.25")
        tk.Entry(fade_row, textvariable=self._fade_out_var, width=6,
                 font=("Courier", ui.font_sm)).pack(side="left", padx=(ui.space_xs, 0))

        for var in (self._start_var, self._duration_var, self._x_var, self._y_var, self._rot_var,
                    self._size_var, self._bg_opacity_var, self._outline_width_var,
                    self._fade_in_var, self._fade_out_var):
            var.trace_add("write", lambda *_: self._on_field_change())

        actions = tk.Frame(left, bg=BG)
        actions.pack(fill="x", pady=ui.space_md)
        tk.Button(actions, text="Refresh Frame", command=self._grab_frame,
                  bg="#333", fg="white", relief="flat", font=("Courier", ui.font_sm)).pack(side="left")
        tk.Button(actions, text="Update Overlay" if e else "Add to Timeline", command=self._add,
                  bg="#4a9eff", fg="white", relief="flat", font=("Courier", ui.font_md, "bold")
                  ).pack(side="right")
        tk.Button(actions, text="Close", command=self._on_close_clicked,
                  bg="#333", fg="white", relief="flat", font=("Courier", ui.font_sm)
                  ).pack(side="right", padx=ui.space_xs)

        bake_row = tk.Frame(left, bg=BG)
        bake_row.pack(fill="x", pady=(0, ui.space_xxs))
        self._bake_btn = tk.Button(bake_row, text="Bake Preview", command=self._bake_preview,
                                    bg="#7a5fff", fg="white", relief="flat", font=("Courier", ui.font_sm))
        self._bake_btn.pack(side="left")
        self._clear_bake_btn = tk.Button(bake_row, text="Clear Bake", command=self._clear_bake,
                                          bg="#333", fg="white", relief="flat", font=("Courier", ui.font_sm),
                                          state="disabled")
        self._clear_bake_btn.pack(side="left", padx=(ui.space_xs, 0))
        tk.Label(bake_row, text="Quality:", bg=BG, fg="white",
                 font=("Courier", ui.font_sm)).pack(side="left", padx=(ui.space_md, 2))
        self._bake_quality_var = tk.StringVar(value="480")
        ttk.Combobox(bake_row, textvariable=self._bake_quality_var,
                     values=["Full", "720", "480", "360", "270"], state="readonly",
                     width=5, font=("Courier", ui.font_sm)).pack(side="left")

        bake_status_row = tk.Frame(left, bg=BG)
        bake_status_row.pack(fill="x", pady=(0, ui.space_md))
        self._bake_progress = ttk.Progressbar(bake_status_row, mode="determinate", maximum=100)
        self._bake_progress.pack(side="left", fill="x", expand=True)
        self._bake_status_lbl = tk.Label(bake_status_row, text="Not baked -- preview may be laggy while scrubbing.",
                                          bg=BG, fg="#777", font=("Courier", ui.font_xs))
        self._bake_status_lbl.pack(side="left", padx=ui.space_sm)

        # Display size shared by both preview modes (based on the project canvas's
        # aspect ratio) so switching between them doesn't jump the layout.
        canvas_w, canvas_h = self.project.canvas_w, self.project.canvas_h
        disp_w = min(self._preview_max_w, canvas_w)
        disp_h = round(canvas_h * disp_w / canvas_w) if canvas_w else disp_w

        self._live_section = tk.Frame(right, bg=BG)
        self._live_section.pack(fill="both", expand=True)
        tk.Label(self._live_section, text="Live preview over grabbed frame (drag the box to place text):",
                 bg=BG, fg="#aaa", font=("Courier", ui.font_sm)).pack(anchor="w")
        self._preview_canvas = tk.Canvas(self._live_section, bg="#000", highlightthickness=0)
        self._preview_canvas.pack()
        self._preview_canvas.bind("<ButtonPress-1>", self._on_box_press)
        self._preview_canvas.bind("<B1-Motion>", self._on_box_motion)
        self._preview_canvas.bind("<ButtonRelease-1>", self._on_box_release)
        self._preview_canvas.bind("<Motion>", self._on_box_hover)

        tk.Label(self._live_section, text="Animation preview (scrub through the overlay's duration):",
                 bg=BG, fg="#aaa", font=("Courier", ui.font_sm)).pack(anchor="w", pady=(ui.space_sm, 0))
        self._anim_scrubber = Scrubber(self._live_section)
        self._anim_scrubber.pack(fill="x")
        self._anim_scrubber.add_seek_callback(self._on_anim_scrub)
        self._anim_time_lbl = tk.Label(self._live_section, text="0.00s / 2.00s", bg=BG, fg="#777",
                                        font=("Courier", ui.font_xs))
        self._anim_time_lbl.pack(anchor="w")

        self._baked_section = tk.Frame(right, bg=BG)
        tk.Label(self._baked_section, text="Baked preview (actual footage behind the text):",
                 bg=BG, fg="#aaa", font=("Courier", ui.font_sm)).pack(anchor="w")
        self._video_frame = tk.Frame(self._baked_section, bg="#000", width=disp_w, height=disp_h)
        self._video_frame.pack()
        self._video_frame.pack_propagate(False)
        baked_transport = tk.Frame(self._baked_section, bg=BG)
        baked_transport.pack(fill="x", pady=(ui.space_sm, 0))
        self._baked_play_btn = tk.Button(baked_transport, text="▶  Play", command=self._toggle_baked_play,
                                          bg="#333", fg="white", relief="flat", font=("Courier", ui.font_md))
        self._baked_play_btn.pack(side="left")
        self._baked_scrubber = Scrubber(self._baked_section)
        self._baked_scrubber.pack(fill="x")
        self._baked_scrubber.add_seek_callback(self._on_baked_seek)
        self._baked_scrubber.set_locked(True)
        self._baked_time_lbl = tk.Label(self._baked_section, text="0.00s / 0.00s", bg=BG, fg="#777",
                                         font=("Courier", ui.font_xs))
        self._baked_time_lbl.pack(anchor="w")

        # Grows/shrinks the preview to fill whatever space is left below the
        # controls (bounded by both available width and height, so a portrait
        # canvas doesn't overflow the window vertically on a wide resize).
        self._right_frame = right
        right.bind("<Configure>", self._on_right_resized)

    def _rebuild_param_fields(self):
        ui = get_scale()
        for w in self._param_frame.winfo_children():
            w.destroy()
        self._param_vars = {}
        key = self._curve_label_to_key.get(self._curve_combo.get(), "none")
        _label, params = CURVE_PRESETS[key]
        e = self._existing
        existing_params = e.curve_params if (e and e.curve_preset == key) else {}
        for name, default in params.items():
            r = tk.Frame(self._param_frame, bg=BG)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=f"{name}:", bg=BG, fg="white", font=("Courier", ui.font_sm),
                     width=14, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(existing_params.get(name, default)))
            var.trace_add("write", lambda *_: self._on_field_change())
            tk.Entry(r, textvariable=var, width=8, font=("Courier", ui.font_sm)).pack(side="left")
            self._param_vars[name] = var

    def _on_curve_changed(self, _e=None):
        self._rebuild_param_fields()
        self._on_field_change()

    def _activate_color_target(self, target: str):
        """Points the inline picker at one of the three color swatches (text/bg/outline)."""
        self._active_color_target = target
        current = {"text": self._color, "bg": self._bg_color, "outline": self._outline_color}[target]
        self._color_picker.set_color(current)

    def _on_picker_change(self, hex_color: str):
        if self._active_color_target == "text":
            self._color = hex_color
            self._color_btn.config(bg=hex_color)
        elif self._active_color_target == "bg":
            self._bg_color = hex_color
            self._bg_color_btn.config(bg=hex_color)
        else:
            self._outline_color = hex_color
            self._outline_color_btn.config(bg=hex_color)
        self._on_field_change()

    def _use_current_time(self):
        self._start_var.set(f"{self.preview_player.current_pos_s():.2f}")
        self._grab_frame()  # re-grab so the preview reflects the new position, not the stale frame

    def _on_anim_scrub(self, frac: float):
        try:
            duration = max(0.05, float(self._duration_var.get() or 2.0))
        except ValueError:
            duration = 2.0
        self._preview_t_s = frac * duration
        self._update_preview()

    def _on_right_resized(self, event):
        """Keeps the preview (live or baked) sized to fill whatever space is
        left below the controls -- bounded by both available width and height
        so a portrait project canvas doesn't overflow the window vertically
        just because the window was made wide."""
        canvas_w, canvas_h = self.project.canvas_w, self.project.canvas_h
        if not canvas_w or not canvas_h:
            return
        ui = get_scale()
        overhead_h = ui.scrubber_height + 150  # labels + play-button row + scrubber + time label, roughly
        avail_w = max(100, event.width)
        avail_h = max(100, event.height - overhead_h)
        max_w_from_height = round(avail_h * canvas_w / canvas_h)
        new_w = max(100, min(avail_w, max_w_from_height))
        if abs(new_w - self._preview_max_w) < 4:
            return
        self._preview_max_w = new_w
        new_h = round(canvas_h * new_w / canvas_w)
        self._video_frame.config(width=new_w, height=new_h)
        if self._base_frame is not None:
            self._update_preview()

    # -------------------------------------------------------------- preview --

    def _grab_frame(self, pos_s: float | None = None):
        path = self.preview_player.current_path
        if path is None:
            log.info("Text overlay editor: no composed preview loaded yet, nothing to grab")
            return
        if pos_s is None:
            pos_s = self.preview_player.current_pos_s()
        data = ffmpeg_ops.extract_frame(path, pos_s)
        if data:
            self._base_frame = Image.open(io.BytesIO(data)).convert("RGBA")
            log.info("Text overlay editor: grabbed preview frame at %.2fs", pos_s)
        self._update_preview()

    def invalidate_composed_preview(self):
        """Called by the owning TimelineTab when Build Preview / Render Final
        Video is clicked while this tab is open -- preview_player now points
        at a freshly rendered file, so any existing bake (trimmed from the
        old one) and the live preview's grabbed frame are both stale."""
        if self._baked:
            self._show_live_section()
        self._bake_status_lbl.config(text="Timeline preview changed -- re-bake to update.")
        try:
            start_s = float(self._start_var.get())
        except ValueError:
            start_s = None
        self._grab_frame(start_s)

    def _on_field_change(self, _e=None):
        if not self._suspend_xy_sync:
            self._sync_box_from_xy_fields()
        if self._baked:
            self._show_live_section()
            self._bake_status_lbl.config(text="Settings changed -- re-bake to update.")
        if self._update_job is not None:
            self.after_cancel(self._update_job)
        self._update_job = self.after(200, self._update_preview)

    def _sync_box_from_xy_fields(self):
        """Typing directly into the X/Y fields recenters the box on the typed
        point without changing its size -- the reverse of _sync_xy_from_box,
        which recenters the fields when the box itself is dragged."""
        try:
            x, y = float(self._x_var.get() or 0), float(self._y_var.get() or 0)
        except ValueError:
            return
        self._box_ctrl.set_box_center(x, y)

    def _sync_xy_from_box(self):
        self._suspend_xy_sync = True
        try:
            cx, cy = self._box_ctrl.box.center
            self._x_var.set(str(round(cx)))
            self._y_var.set(str(round(cy)))
        finally:
            self._suspend_xy_sync = False

    def _build_overlay(self) -> TextOverlay | None:
        try:
            key = self._curve_label_to_key.get(self._curve_combo.get(), "none")
            params = {name: float(var.get()) for name, var in self._param_vars.items()}
            return TextOverlay(
                text=self._text_widget.get("1.0", "end-1c"),
                start_s=float(self._start_var.get() or 0),
                duration_s=max(0.05, float(self._duration_var.get() or 2.0)),
                x=float(self._x_var.get() or 0),
                y=float(self._y_var.get() or 0),
                box_w=self._box_ctrl.box.w,
                box_h=self._box_ctrl.box.h,
                wrap_enabled=self._wrap_var.get(),
                rotation_deg=float(self._rot_var.get() or 0),
                font_family=self._font_var.get(),
                font_size=max(1, int(float(self._size_var.get() or 64))),
                color=self._color,
                bold=self._bold_var.get(),
                italic=self._italic_var.get(),
                underline=self._underline_var.get(),
                bg_color=self._bg_color,
                bg_opacity=max(0.0, min(1.0, float(self._bg_opacity_var.get() or 0) / 100.0)),
                outline_enabled=self._outline_var.get(),
                outline_color=self._outline_color,
                outline_width=max(0, int(float(self._outline_width_var.get() or 2))),
                curve_preset=key,
                curve_params=params,
                fade_in_s=max(0.0, float(self._fade_in_var.get() or 0)),
                fade_out_s=max(0.0, float(self._fade_out_var.get() or 0)),
            )
        except ValueError:
            return None

    def _update_preview(self):
        self._update_job = None
        overlay = self._build_overlay()
        if overlay is None or self._base_frame is None:
            return

        t_s = max(0.0, min(self._preview_t_s, overlay.duration_s))
        t_norm = t_s / overlay.duration_s if overlay.duration_s > 0 else 0.0
        self._anim_scrubber.set_playhead(t_norm)
        self._anim_time_lbl.config(text=f"{t_s:.2f}s / {overlay.duration_s:.2f}s")

        canvas_scale = self._base_frame.height / self.project.canvas_h if self.project.canvas_h else 1.0
        scale = eval_curve(overlay.curve_preset, overlay.curve_params, t_norm)
        opacity = fade_opacity(t_s, overlay.duration_s, overlay.fade_in_s, overlay.fade_out_s)
        composited = text_render.paste_overlay_on_frame(self._base_frame, overlay, scale, canvas_scale, opacity)

        display = composited
        if display.width != self._preview_max_w:
            ratio = self._preview_max_w / display.width
            display = display.resize((self._preview_max_w, max(1, round(display.height * ratio))), Image.LANCZOS)

        self._preview_imgtk = ImageTk.PhotoImage(display)
        self._preview_canvas.config(width=display.width, height=display.height)
        self._preview_canvas.delete("all")
        self._preview_canvas.create_image(0, 0, anchor="nw", image=self._preview_imgtk)
        self._preview_canvas_scale = display.width / self.project.canvas_w if self.project.canvas_w else 1.0
        self._draw_box_overlay()

    def _draw_box_overlay(self):
        s = self._preview_canvas_scale
        b = self._box_ctrl.box
        x0, y0, x1, y1 = b.left * s, b.top * s, b.right * s, b.bottom * s
        c = self._preview_canvas
        c.create_rectangle(x0, y0, x1, y1, outline="#4a9eff", width=2)
        for hx, hy in _handle_points(x0, y0, x1, y1):
            r = 4
            c.create_rectangle(hx - r, hy - r, hx + r, hy + r, fill="#4a9eff", outline="white")

    # ---------------------------------------------------------------- box --
    # A plain Tk Canvas over a static composited image (unlike the timeline
    # clip crop editor's live-video overlay) -- no transparency tricks needed,
    # clicks anywhere on the canvas just work.

    def _on_box_press(self, event):
        s = self._preview_canvas_scale
        if s == 0:
            return
        self._box_ctrl.begin_drag(event.x / s, event.y / s, 8 / s)

    def _on_box_release(self, _event):
        self._box_ctrl.end_drag()

    def _on_box_motion(self, event):
        s = self._preview_canvas_scale
        if self._box_ctrl.drag_handle is None or s == 0:
            return
        self._box_ctrl.drag_to(event.x / s, event.y / s, min_size=10 / s)
        self._sync_xy_from_box()
        self._update_preview()

    def _on_box_hover(self, event):
        s = self._preview_canvas_scale
        if s == 0:
            return
        handle = self._box_ctrl.hit_test(event.x / s, event.y / s, 8 / s)
        self._preview_canvas.config(cursor=CURSOR_BY_HANDLE.get(handle, "crosshair"))

    # ---------------------------------------------------------------- bake --

    def _bake_preview(self):
        overlay = self._build_overlay()
        source_path = self.preview_player.current_path
        if overlay is None or source_path is None:
            self._bake_status_lbl.config(text="Compose the timeline preview first, then bake.")
            return

        # A fresh unique filename per bake -- reusing one path across bakes let
        # ffmpeg's overwrite race the previous bake's mpv instance, which could
        # still have that exact file open (Windows in particular tears the read
        # mid-write, producing a corrupted frame mixing old and new content).
        out_path = self._tmp_dir / f"preview_{id(self)}_{uuid.uuid4().hex}.mp4"
        quality = self._bake_quality_var.get()
        preview_height = None if quality == "Full" else int(quality)

        self._bake_btn.config(state="disabled", text="Baking...")
        self._bake_progress["value"] = 0
        self._bake_status_lbl.config(text="Rendering...")

        def on_progress(frac, _label):
            self.after(0, lambda: self._bake_progress.config(value=frac * 100))

        def run():
            try:
                ok = ffmpeg_ops.render_text_overlay_preview(
                    source_path, overlay, self.project.canvas_w, self.project.canvas_h, out_path,
                    preview_height=preview_height, progress_cb=on_progress)
            except Exception:
                log.exception("Text overlay editor: bake failed for overlay '%s'", overlay.text)
                ok = False
            self.after(0, lambda: self._on_bake_done(ok, out_path, overlay.text))

        threading.Thread(target=run, daemon=True).start()

    def _on_bake_done(self, ok: bool, out_path, overlay_text: str):
        self._bake_btn.config(state="normal", text="Bake Preview")
        if not ok:
            self._bake_status_lbl.config(text="Bake failed (see console).")
            self._delete_bake_file(out_path)
            return
        self._bake_status_lbl.config(text="Baked.")
        if self.player is None:
            self.player = SharedPlayer(self)
            self.player.set_embed_widget(self._video_frame)
            self.player.set_tick_callback(self._on_baked_tick)
        self._show_baked_section()
        # .load() terminates the previous mpv instance before playing the new
        # file, so it's safe to delete the old bake only now that it's released.
        self.player.load(out_path, on_ready=lambda: self._baked_scrubber.set_locked(False))
        if self._last_bake_path is not None and self._last_bake_path != out_path:
            self._delete_bake_file(self._last_bake_path)
        self._last_bake_path = out_path
        log.info("Text overlay editor: baked preview for '%s'", overlay_text[:30])

    @staticmethod
    def _delete_bake_file(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.exception("Text overlay editor: failed to delete stale bake file %s", path)

    def _show_baked_section(self):
        self._baked = True
        self._clear_bake_btn.config(state="normal")
        self._live_section.pack_forget()
        self._baked_section.pack(fill="both", expand=True)

    def _show_live_section(self):
        self._baked = False
        self._clear_bake_btn.config(state="disabled")
        self._baked_section.pack_forget()
        self._live_section.pack(fill="both", expand=True)

    def _clear_bake(self):
        if self.player is not None:
            self.player.pause()
        self._show_live_section()
        self._bake_status_lbl.config(text="Not baked -- preview may be laggy while scrubbing.")

    def _on_baked_tick(self, pos_frac, duration_s, paused):
        self._baked_scrubber.set_playhead(pos_frac)
        self._baked_play_btn.config(text="⏸  Pause" if not paused else "▶  Play")
        self._baked_time_lbl.config(text=f"{pos_frac * duration_s:.2f}s / {duration_s:.2f}s")

    def _toggle_baked_play(self):
        if self.player is not None:
            self.player.play_pause()

    def _on_baked_seek(self, frac: float):
        if self.player is not None:
            self.player.seek(frac)

    # ---------------------------------------------------------------- add --

    def _add(self):
        overlay = self._build_overlay()
        if overlay is None:
            return
        self.on_save(overlay)
        verb = "updated" if self._existing else "added"
        log.info("Text overlay %s: '%s' @ %.2fs-%.2fs", verb, overlay.text[:30], overlay.start_s, overlay.end_s)
        if self._existing is None:
            self._text_widget.delete("1.0", "end")
        self.teardown()
        self.on_close()

    # --------------------------------------------------------------- close --

    def _on_close_clicked(self):
        self.teardown()
        self.on_close()

    def teardown(self):
        """Idempotent cleanup of the debounced-preview after() job, the
        baked-preview mpv player (if one was ever created), and its leftover
        temp file -- called whether this tab closes via its own buttons or
        gets force-replaced/rebuilt by the owning TimelineTab."""
        if self._update_job is not None:
            self.after_cancel(self._update_job)
            self._update_job = None
        if self.player is not None:
            self.player.terminate()
            self.player = None
        if self._last_bake_path is not None:
            self._delete_bake_file(self._last_bake_path)
            self._last_bake_path = None
