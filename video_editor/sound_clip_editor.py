from __future__ import annotations

import tempfile
import threading
import uuid
from pathlib import Path
from tkinter import ttk
import tkinter as tk
from typing import Callable

from . import ffmpeg_ops
from .logging_config import get_logger
from .player import SharedPlayer
from .project import SoundClip, TimelineProject
from .ui_scale import get_scale
from .widgets import Scrubber

log = get_logger()

BG = "#1e1e1e"


class SoundClipEditor(tk.Frame):
    """Sound clips have no cheap live-recompute equivalent to the PIL-based
    live preview text/image overlays use (any real volume/speed/loop/fade
    processing needs ffmpeg), so unlike those editors this only has one
    preview mode: bake. "Bake Preview" renders the actual composed-timeline
    video chunk the sound plays over with the processed sound mixed in
    (ffmpeg_ops.render_sound_clip_preview) and plays it back through the same
    mpv-embedded transport used everywhere else in the app. Embedded as a tab
    in the Timeline tab's right-side Notebook rather than a popup window;
    on_close is invoked after Add/Update or Close to hand control back to
    whoever opened this tab (see TimelineTab._open_edit_tab)."""

    def __init__(self, parent, project: TimelineProject, preview_player: SharedPlayer, source: str,
                 on_save: Callable[[SoundClip], None], on_close: Callable[[], None],
                 existing: SoundClip | None = None):
        super().__init__(parent, bg=BG)

        self.project = project
        self.preview_player = preview_player
        self.source = source
        self.on_save = on_save
        self.on_close = on_close
        self._existing = existing
        self._baked = False
        self.player: SharedPlayer | None = None  # created lazily on first bake
        self._last_bake_path: Path | None = None
        self._tmp_dir = Path(tempfile.gettempdir()) / "video_editor_sound_preview"
        self._tmp_dir.mkdir(exist_ok=True)

        self._build_ui()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        ui = get_scale()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=ui.space_lg, pady=ui.space_lg)

        left = tk.Frame(body, bg=BG)
        left.pack(side="top", fill="x")
        right = tk.Frame(body, bg=BG)
        right.pack(side="top", fill="both", expand=True, pady=(ui.space_md, 0))

        def row(label, widget_factory):
            r = tk.Frame(left, bg=BG)
            r.pack(fill="x", pady=ui.space_xxs)
            tk.Label(r, text=label, bg=BG, fg="white", font=("Courier", ui.font_sm),
                     width=18, anchor="w").pack(side="left")
            w = widget_factory(r)
            w.pack(side="left")
            return w

        e = self._existing
        tk.Label(left, text=f"Source: {Path(self.source).name}", bg=BG, fg="#aaa",
                 font=("Courier", ui.font_sm)).pack(anchor="w", pady=(0, ui.space_sm))

        time_row = tk.Frame(left, bg=BG)
        time_row.pack(fill="x", pady=ui.space_xxs)
        tk.Label(time_row, text="Start on timeline (s):", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=18, anchor="w").pack(side="left")
        start_default = f"{e.start_s:.2f}" if e else f"{self.preview_player.current_pos_s():.2f}"
        self._start_var = tk.StringVar(value=start_default)
        tk.Entry(time_row, textvariable=self._start_var, width=8,
                 font=("Courier", ui.font_sm)).pack(side="left")
        tk.Button(time_row, text="Use current preview time", command=self._use_current_time,
                  bg="#333", fg="white", relief="flat", font=("Courier", ui.font_xs)).pack(side="left",
                                                                                             padx=ui.space_xs)

        self._clip_start_var = tk.StringVar(value=str(e.clip_start_s) if e else "0.0")
        row("Source offset (s):", lambda r: tk.Entry(r, textvariable=self._clip_start_var, width=8,
                                                        font=("Courier", ui.font_sm)))

        self._clip_duration_var = tk.StringVar(
            value=str(e.clip_duration_s) if e else f"{self._default_duration():.2f}")
        row("Segment length (s):", lambda r: tk.Entry(r, textvariable=self._clip_duration_var, width=8,
                                                         font=("Courier", ui.font_sm)))

        self._volume_var = tk.StringVar(value=str(round((e.volume if e else 1.0) * 100)))
        row("Volume %:", lambda r: tk.Entry(r, textvariable=self._volume_var, width=8,
                                              font=("Courier", ui.font_sm)))

        self._speed_var = tk.StringVar(value=str(round((e.speed if e else 1.0) * 100)))
        row("Speed/pitch %:", lambda r: tk.Entry(r, textvariable=self._speed_var, width=8,
                                                    font=("Courier", ui.font_sm)))

        repeat_row = tk.Frame(left, bg=BG)
        repeat_row.pack(fill="x", pady=ui.space_xxs)
        tk.Label(repeat_row, text="Repeat count:", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=18, anchor="w").pack(side="left")
        self._repeat_count_var = tk.StringVar(value=str(e.repeat_count if e else 1))
        self._repeat_entry = tk.Entry(repeat_row, textvariable=self._repeat_count_var, width=8,
                                       font=("Courier", ui.font_sm))
        self._repeat_entry.pack(side="left")
        self._repeat_infinite_var = tk.BooleanVar(value=e.repeat_infinite if e else False)
        tk.Checkbutton(repeat_row, text="Loop until timeline ends", variable=self._repeat_infinite_var,
                        command=self._on_infinite_toggled, bg=BG, fg="white", selectcolor="#333",
                        activebackground=BG, activeforeground="white", font=("Courier", ui.font_sm)
                        ).pack(side="left", padx=ui.space_md)
        self._on_infinite_toggled()

        fade_row = tk.Frame(left, bg=BG)
        fade_row.pack(fill="x", pady=ui.space_xxs)
        tk.Label(fade_row, text="Fade in/out (s):", bg=BG, fg="white", font=("Courier", ui.font_sm),
                 width=18, anchor="w").pack(side="left")
        self._fade_in_var = tk.StringVar(value=str(e.fade_in_s if e else 0.0))
        tk.Entry(fade_row, textvariable=self._fade_in_var, width=6,
                 font=("Courier", ui.font_sm)).pack(side="left")
        self._fade_out_var = tk.StringVar(value=str(e.fade_out_s if e else 0.0))
        tk.Entry(fade_row, textvariable=self._fade_out_var, width=6,
                 font=("Courier", ui.font_sm)).pack(side="left", padx=(ui.space_xs, 0))

        for var in (self._clip_start_var, self._clip_duration_var, self._volume_var, self._speed_var,
                    self._repeat_count_var, self._fade_in_var, self._fade_out_var, self._start_var):
            var.trace_add("write", lambda *_: self._on_field_change())

        actions = tk.Frame(left, bg=BG)
        actions.pack(fill="x", pady=ui.space_md)
        tk.Button(actions, text="Update Sound" if e else "Add to Timeline", command=self._add,
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
        self._bake_status_lbl = tk.Label(bake_status_row, text="Not baked -- click Bake Preview to hear it.",
                                          bg=BG, fg="#777", font=("Courier", ui.font_xs))
        self._bake_status_lbl.pack(side="left", padx=ui.space_sm)

        self._empty_section = tk.Frame(right, bg=BG)
        self._empty_section.pack(fill="both", expand=True)
        tk.Label(self._empty_section, text="(nothing baked yet)", bg=BG, fg="#555",
                 font=("Courier", ui.font_sm)).pack(pady=ui.space_xl)

        self._baked_section = tk.Frame(right, bg=BG)
        tk.Label(self._baked_section, text="Baked preview:",
                 bg=BG, fg="#aaa", font=("Courier", ui.font_sm)).pack(anchor="w")
        self._video_frame = tk.Frame(self._baked_section, bg="#000", height=ui.trim_preview_h)
        self._video_frame.pack(fill="x")
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

    def _default_duration(self) -> float:
        try:
            path = Path(self.source)
            if path.exists():
                return max(0.05, ffmpeg_ops.probe_video(path).duration_s)
        except Exception:
            log.exception("Sound clip editor: failed to probe default duration for %s", self.source)
        return 5.0

    def _on_infinite_toggled(self):
        self._repeat_entry.config(state="disabled" if self._repeat_infinite_var.get() else "normal")
        self._on_field_change()

    def _use_current_time(self):
        self._start_var.set(f"{self.preview_player.current_pos_s():.2f}")

    # -------------------------------------------------------------- build --

    def _build_sound(self) -> SoundClip | None:
        try:
            return SoundClip(
                source=self.source,
                start_s=max(0.0, float(self._start_var.get() or 0)),
                clip_start_s=max(0.0, float(self._clip_start_var.get() or 0)),
                clip_duration_s=max(0.05, float(self._clip_duration_var.get() or 0.05)),
                volume=max(0.0, min(2.0, float(self._volume_var.get() or 0) / 100.0)),
                speed=max(0.1, min(4.0, float(self._speed_var.get() or 100) / 100.0)),
                repeat_count=max(1, int(float(self._repeat_count_var.get() or 1))),
                repeat_infinite=self._repeat_infinite_var.get(),
                fade_in_s=max(0.0, float(self._fade_in_var.get() or 0)),
                fade_out_s=max(0.0, float(self._fade_out_var.get() or 0)),
            )
        except ValueError:
            return None

    def invalidate_composed_preview(self):
        """Called by the owning TimelineTab when Build Preview / Render Final
        Video is clicked while this tab is open -- preview_player now points
        at a freshly rendered file, so any existing bake (trimmed from the
        old one) is stale."""
        if self._baked:
            self._show_empty_section()
        self._bake_status_lbl.config(text="Timeline preview changed -- re-bake to update.")

    def _on_field_change(self, _e=None):
        if self._baked:
            self._show_empty_section()
            self._bake_status_lbl.config(text="Settings changed -- re-bake to update.")

    # ---------------------------------------------------------------- bake --

    def _bake_preview(self):
        sound = self._build_sound()
        source_path = self.preview_player.current_path
        if sound is None or source_path is None:
            self._bake_status_lbl.config(text="Compose the timeline preview first, then bake.")
            return

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
                ok = ffmpeg_ops.render_sound_clip_preview(
                    source_path, sound, out_path, preview_height=preview_height, progress_cb=on_progress)
            except Exception:
                log.exception("Sound clip editor: bake failed for %s", self.source)
                ok = False
            self.after(0, lambda: self._on_bake_done(ok, out_path))

        threading.Thread(target=run, daemon=True).start()

    def _on_bake_done(self, ok: bool, out_path: Path):
        self._bake_btn.config(state="normal", text="Bake Preview")
        if not ok:
            self._bake_status_lbl.config(text="Bake failed (nothing to play at this position, or see console).")
            self._delete_bake_file(out_path)
            return
        self._bake_status_lbl.config(text="Baked.")
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
        self._clear_bake_btn.config(state="normal")
        self._empty_section.pack_forget()
        self._baked_section.pack(fill="both", expand=True)

    def _show_empty_section(self):
        self._baked = False
        self._clear_bake_btn.config(state="disabled")
        self._baked_section.pack_forget()
        self._empty_section.pack(fill="both", expand=True)

    def _clear_bake(self):
        if self.player is not None:
            self.player.pause()
        self._show_empty_section()
        self._bake_status_lbl.config(text="Not baked -- click Bake Preview to hear it.")

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
        """Terminates the baked-preview mpv player, if one was ever created,
        and deletes its leftover temp file."""
        if self.player is not None:
            self.player.terminate()
            self.player = None
        if self._last_bake_path is not None:
            self._delete_bake_file(self._last_bake_path)
            self._last_bake_path = None
