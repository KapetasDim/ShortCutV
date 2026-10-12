from __future__ import annotations

import tempfile
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import ttk
from typing import Callable

from PIL import Image, ImageTk

from . import ffmpeg_ops, image_render
from .curves import CURVE_PRESETS, eval_curve
from .logging_config import get_logger
from .player import SharedPlayer
from .project import TimelineClip
from .ui_scale import get_scale
from .widgets import Scrubber

log = get_logger()

BG = "#1e1e1e"


class ImageClipEditor(tk.Frame):
    """Settings panel for an image-as-clip timeline entry: how long it stays
    on screen and whether/how it Ken-Burns zooms, reusing the same curve
    presets built for text overlays. The default preview is a static PIL
    frame recomputed live on every scrub tick -- fine for a quick look, but
    visibly laggy for anything past a trivial zoom. "Bake Preview" renders
    the animation to a short silent .mp4 (ffmpeg_ops.render_image_clip_preview)
    and switches to playing that back through the same mpv-embedded
    transport (Play button, real seeking) the video clip editor uses, so
    scrubbing/playback is smooth. Embedded as a tab in the Timeline tab's
    right-side Notebook rather than a popup window; on_close is invoked
    after Apply or Close to hand control back to whoever opened this tab
    (see TimelineTab._open_edit_tab)."""

    def __init__(self, parent, clip: TimelineClip, canvas_w: int, canvas_h: int,
                 on_change: Callable[[], None], on_close: Callable[[], None]):
        super().__init__(parent, bg=BG)

        self.clip = clip
        self.canvas_w = max(1, canvas_w)
        self.canvas_h = max(1, canvas_h)
        self.on_change = on_change
        self.on_close = on_close
        self._fitted: Image.Image | None = None
        self._preview_imgtk = None
        self._preview_t_s = 0.0
        self._preview_max_w = get_scale().preview_max_w_image
        self._baked = False
        self.player: SharedPlayer | None = None  # created lazily on first bake
        self._last_bake_path: Path | None = None  # deleted once superseded/torn down
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_image_preview"
        self._tmp_dir.mkdir(exist_ok=True)

        self._build_ui()
        self._load_source()
        self._update_preview()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        scale = get_scale()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=scale.space_lg, pady=scale.space_lg)

        left = tk.Frame(body, bg=BG)
        left.pack(side="top", fill="x")
        right = tk.Frame(body, bg=BG)
        right.pack(side="top", fill="both", expand=True, pady=(scale.space_md, 0))

        def row(label, widget_factory):
            r = tk.Frame(left, bg=BG)
            r.pack(fill="x", pady=scale.space_xxs)
            tk.Label(r, text=label, bg=BG, fg="white", font=("Courier", scale.font_sm),
                     width=14, anchor="w").pack(side="left")
            w = widget_factory(r)
            w.pack(side="left", fill="x", expand=True)
            return w

        self._duration_var = tk.StringVar(value=f"{self.clip.duration:.2f}")
        row("Duration (s):", lambda r: tk.Entry(r, textvariable=self._duration_var, width=8,
                                                  font=("Courier", scale.font_sm)))

        self._zoom_var = tk.BooleanVar(value=self.clip.image_zoom_enabled)
        tk.Checkbutton(left, text="Zoom in over duration (Ken Burns)", variable=self._zoom_var,
                        command=self._on_field_change, bg=BG, fg="white", selectcolor="#333",
                        activebackground=BG, activeforeground="white", font=("Courier", scale.font_sm)
                        ).pack(anchor="w", pady=scale.space_xs)

        self._zoom_factor_var = tk.StringVar(value=str(self.clip.image_zoom_factor))
        row("Zoom factor:", lambda r: tk.Entry(r, textvariable=self._zoom_factor_var, width=8,
                                                 font=("Courier", scale.font_sm)))

        curve_row = tk.Frame(left, bg=BG)
        curve_row.pack(fill="x", pady=(scale.space_md, scale.space_xxs))
        tk.Label(curve_row, text="Zoom curve:", bg=BG, fg="white", font=("Courier", scale.font_sm),
                 width=14, anchor="w").pack(side="left")
        curve_labels = {key: label for key, (label, _params) in CURVE_PRESETS.items()}
        self._curve_label_to_key = {v: k for k, v in curve_labels.items()}
        self._curve_combo = ttk.Combobox(curve_row, values=list(curve_labels.values()),
                                          state="readonly", width=16, font=("Courier", scale.font_sm))
        self._curve_combo.set(curve_labels.get(self.clip.image_zoom_curve, curve_labels["none"]))
        self._curve_combo.pack(side="left")
        self._curve_combo.bind("<<ComboboxSelected>>", self._on_curve_changed)

        self._param_frame = tk.Frame(left, bg=BG)
        self._param_frame.pack(fill="x", pady=scale.space_xxs)
        self._param_vars: dict[str, tk.StringVar] = {}
        self._rebuild_param_fields()

        for var in (self._duration_var, self._zoom_factor_var):
            var.trace_add("write", lambda *_: self._on_field_change())

        actions = tk.Frame(left, bg=BG)
        actions.pack(fill="x", pady=scale.space_md)
        tk.Button(actions, text="Apply to Timeline", command=self._apply,
                  bg="#4a9eff", fg="white", relief="flat", font=("Courier", scale.font_md, "bold")
                  ).pack(side="left")
        tk.Button(actions, text="Close", command=self._on_close_clicked,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="right")

        bake_row = tk.Frame(left, bg=BG)
        bake_row.pack(fill="x", pady=(0, scale.space_xxs))
        self._bake_btn = tk.Button(bake_row, text="Bake Preview", command=self._bake_preview,
                                    bg="#7a5fff", fg="white", relief="flat", font=("Courier", scale.font_sm))
        self._bake_btn.pack(side="left")
        tk.Label(bake_row, text="Quality:", bg=BG, fg="white",
                 font=("Courier", scale.font_sm)).pack(side="left", padx=(scale.space_md, 2))
        self._bake_quality_var = tk.StringVar(value="480")
        ttk.Combobox(bake_row, textvariable=self._bake_quality_var,
                     values=["Full", "720", "480", "360", "270"], state="readonly",
                     width=5, font=("Courier", scale.font_sm)).pack(side="left")

        bake_status_row = tk.Frame(left, bg=BG)
        bake_status_row.pack(fill="x", pady=(0, scale.space_md))
        self._bake_progress = ttk.Progressbar(bake_status_row, mode="determinate", maximum=100)
        self._bake_progress.pack(side="left", fill="x", expand=True)
        self._bake_status_lbl = tk.Label(bake_status_row, text="Not baked -- preview may be laggy while scrubbing.",
                                          bg=BG, fg="#777", font=("Courier", scale.font_xs))
        self._bake_status_lbl.pack(side="left", padx=scale.space_sm)

        # Display size shared by both preview modes (based on the canvas's
        # aspect ratio) so switching between them doesn't jump the layout.
        disp_w = min(self._preview_max_w, self.canvas_w)
        disp_h = round(self.canvas_h * disp_w / self.canvas_w) if self.canvas_w else disp_w

        self._live_section = tk.Frame(right, bg=BG)
        self._live_section.pack(fill="both", expand=True)
        tk.Label(self._live_section, text="Preview (contain-fit to output canvas):",
                 bg=BG, fg="#aaa", font=("Courier", scale.font_sm)).pack(anchor="w")
        self._preview_lbl = tk.Label(self._live_section, bg="#000")
        self._preview_lbl.pack()
        tk.Label(self._live_section, text="Zoom preview (scrub through the clip's duration):",
                 bg=BG, fg="#aaa", font=("Courier", scale.font_sm)).pack(anchor="w", pady=(scale.space_sm, 0))
        self._scrubber = Scrubber(self._live_section)
        self._scrubber.pack(fill="x")
        self._scrubber.add_seek_callback(self._on_scrub)
        self._time_lbl = tk.Label(self._live_section, text="0.00s / 1.50s", bg=BG, fg="#777",
                                   font=("Courier", scale.font_xs))
        self._time_lbl.pack(anchor="w")

        self._baked_section = tk.Frame(right, bg=BG)
        tk.Label(self._baked_section, text="Baked preview:",
                 bg=BG, fg="#aaa", font=("Courier", scale.font_sm)).pack(anchor="w")
        self._video_frame = tk.Frame(self._baked_section, bg="#000", width=disp_w, height=disp_h)
        self._video_frame.pack()
        self._video_frame.pack_propagate(False)
        baked_transport = tk.Frame(self._baked_section, bg=BG)
        baked_transport.pack(fill="x", pady=(scale.space_sm, 0))
        self._baked_play_btn = tk.Button(baked_transport, text="▶  Play", command=self._toggle_baked_play,
                                          bg="#333", fg="white", relief="flat", font=("Courier", scale.font_md))
        self._baked_play_btn.pack(side="left")
        self._baked_scrubber = Scrubber(self._baked_section)
        self._baked_scrubber.pack(fill="x")
        self._baked_scrubber.add_seek_callback(self._on_baked_seek)
        self._baked_scrubber.set_locked(True)
        self._baked_time_lbl = tk.Label(self._baked_section, text="0.00s / 0.00s", bg=BG, fg="#777",
                                         font=("Courier", scale.font_xs))
        self._baked_time_lbl.pack(anchor="w")

    def _rebuild_param_fields(self):
        scale = get_scale()
        for w in self._param_frame.winfo_children():
            w.destroy()
        self._param_vars = {}
        key = self._curve_label_to_key.get(self._curve_combo.get(), "none")
        _label, params = CURVE_PRESETS[key]
        existing_params = self.clip.image_zoom_curve_params if self.clip.image_zoom_curve == key else {}
        for name, default in params.items():
            r = tk.Frame(self._param_frame, bg=BG)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=f"{name}:", bg=BG, fg="white", font=("Courier", scale.font_sm),
                     width=14, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(existing_params.get(name, default)))
            var.trace_add("write", lambda *_: self._on_field_change())
            tk.Entry(r, textvariable=var, width=8, font=("Courier", scale.font_sm)).pack(side="left")
            self._param_vars[name] = var

    def _on_curve_changed(self, _e=None):
        self._rebuild_param_fields()
        self._on_field_change()

    def _on_field_change(self, _e=None):
        if self._baked:
            self._show_live_section()
            self._bake_status_lbl.config(text="Settings changed -- re-bake to update.")
        self._update_preview()

    def _on_scrub(self, frac: float):
        try:
            duration = max(0.05, float(self._duration_var.get() or 1.5))
        except ValueError:
            duration = 1.5
        self._preview_t_s = frac * duration
        self._update_preview()

    # -------------------------------------------------------------- preview --

    def _load_source(self):
        try:
            source = Image.open(self.clip.path)
            self._fitted = image_render.fit_canvas(source, self.canvas_w, self.canvas_h)
        except Exception:
            log.exception("Image clip editor: failed to load %s", self.clip.path)
            self._fitted = None

    def _current_settings(self):
        try:
            duration = max(0.05, float(self._duration_var.get() or 1.5))
            zoom_factor = max(1.0, float(self._zoom_factor_var.get() or 1.2))
            params = {name: float(var.get()) for name, var in self._param_vars.items()}
        except ValueError:
            return None
        key = self._curve_label_to_key.get(self._curve_combo.get(), "none")
        return {
            "duration": duration,
            "zoom_enabled": self._zoom_var.get(),
            "zoom_factor": zoom_factor,
            "curve": key,
            "params": params,
        }

    def _update_preview(self):
        if self._fitted is None:
            return
        settings = self._current_settings()
        if settings is None:
            return

        t_s = max(0.0, min(self._preview_t_s, settings["duration"]))
        t_norm = t_s / settings["duration"] if settings["duration"] > 0 else 0.0
        self._scrubber.set_playhead(t_norm)
        self._time_lbl.config(text=f"{t_s:.2f}s / {settings['duration']:.2f}s")

        if settings["zoom_enabled"]:
            progress = eval_curve(settings["curve"], settings["params"], t_norm)
            scale = 1.0 + (settings["zoom_factor"] - 1.0) * progress
            frame = image_render.compute_zoom_frame(self._fitted, self.canvas_w, self.canvas_h, scale)
        else:
            frame = self._fitted

        display = frame
        if display.width > self._preview_max_w:
            ratio = self._preview_max_w / display.width
            display = display.resize((self._preview_max_w, max(1, round(display.height * ratio))), Image.LANCZOS)

        self._preview_imgtk = ImageTk.PhotoImage(display)
        self._preview_lbl.config(image=self._preview_imgtk)

    # ---------------------------------------------------------------- bake --

    def _bake_preview(self):
        settings = self._current_settings()
        if settings is None or self._fitted is None:
            return
        tmp_clip = TimelineClip(
            source=str(self.clip.path), in_s=0.0, out_s=settings["duration"], is_image=True,
            image_zoom_enabled=settings["zoom_enabled"], image_zoom_factor=settings["zoom_factor"],
            image_zoom_curve=settings["curve"], image_zoom_curve_params=settings["params"],
        )
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
                ok = ffmpeg_ops.render_image_clip_preview(
                    tmp_clip, self.canvas_w, self.canvas_h, out_path,
                    preview_height=preview_height, progress_cb=on_progress)
            except Exception:
                log.exception("Image clip editor: bake failed for %s", self.clip.path)
                ok = False
            self.after(0, lambda: self._on_bake_done(ok, out_path))

        threading.Thread(target=run, daemon=True).start()

    def _on_bake_done(self, ok: bool, out_path: Path):
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
        log.info("Image clip editor: baked preview for %s", self.clip.path.name)

    @staticmethod
    def _delete_bake_file(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.exception("Image clip editor: failed to delete stale bake file %s", path)

    def _show_baked_section(self):
        self._baked = True
        self._live_section.pack_forget()
        self._baked_section.pack(fill="both", expand=True)

    def _show_live_section(self):
        self._baked = False
        self._baked_section.pack_forget()
        self._live_section.pack(fill="both", expand=True)

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

    # ---------------------------------------------------------------- apply --

    def _apply(self):
        settings = self._current_settings()
        if settings is None:
            return
        self.clip.out_s = self.clip.in_s + settings["duration"]
        self.clip.image_zoom_enabled = settings["zoom_enabled"]
        self.clip.image_zoom_factor = settings["zoom_factor"]
        self.clip.image_zoom_curve = settings["curve"]
        self.clip.image_zoom_curve_params = settings["params"]
        self.on_change()
        log.info("Image clip editor: applied duration=%.2fs zoom=%s to %s",
                  settings["duration"], settings["zoom_enabled"], self.clip.path.name)
        self.teardown()
        self.on_close()

    # --------------------------------------------------------------- close --

    def _on_close_clicked(self):
        self.teardown()
        self.on_close()

    def teardown(self):
        """Terminates the baked-preview mpv player, if one was ever created,
        and deletes its leftover temp file."""
        if self.player is not None:
            self.player.terminate()
        if self._last_bake_path is not None:
            self._delete_bake_file(self._last_bake_path)
            self._last_bake_path = None
