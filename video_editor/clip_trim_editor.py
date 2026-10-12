from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from . import ffmpeg_ops
from .crop_box import ASPECT_RATIO_LABELS, ASPECT_RATIO_VALUES
from .logging_config import get_logger
from .native_dialog import ask_save_filename
from .player import SharedPlayer
from .project import TimelineClip
from .ui_scale import get_scale
from .widgets import RangeSlider, VideoCropOverlay, fmt_time

log = get_logger()

BG = "#1e1e1e"


class ClipTrimEditor(tk.Frame):
    """Clip-Exporter-like panel for adjusting a single timeline clip's in/out
    points directly (no need to pre-cut it in the Clip Exporter tab first),
    plus the ability to export that trimmed range or a single frame from it.
    Embedded as a tab in the Timeline tab's right-side Notebook rather than a
    popup window; on_close is invoked after Apply or Close to hand control
    back to whoever opened this tab (see TimelineTab._open_edit_tab)."""

    def __init__(self, parent, clip: TimelineClip, on_change: Callable[[], None], on_close: Callable[[], None]):
        super().__init__(parent, bg=BG)

        self.clip = clip
        self.on_change = on_change
        self.on_close = on_close
        self.player = SharedPlayer(self)
        self._crop_overlay: VideoCropOverlay | None = None

        # Probed once up front: needed both to letterbox-fit the crop overlay
        # over the actual video content (not any black bars mpv adds) and to
        # convert its fractional box into source-pixel crop_top/bottom/left/right.
        probe = ffmpeg_ops.probe_video(clip.path)
        self._src_w = probe.width or 1
        self._src_h = probe.height or 1
        self._crop_top = clip.crop_top
        self._crop_bottom = clip.crop_bottom
        self._crop_left = clip.crop_left
        self._crop_right = clip.crop_right

        self._build_ui()

        # The crop box is a transparent overlay window tracking _preview_frame
        # geometrically -- see VideoCropOverlay's docstring for why (mpv embeds
        # by rendering straight into that frame's native window, so an ordinary
        # sibling Tk Canvas can't show through it).
        self._crop_overlay = VideoCropOverlay(
            self.winfo_toplevel(), self._preview_frame,
            on_change=self._on_crop_overlay_change, content_rect_fn=self._video_content_rect,
        )
        self._crop_overlay.set_crop_fractions(
            self._crop_top / self._src_h, self._crop_bottom / self._src_h,
            self._crop_left / self._src_w, self._crop_right / self._src_w,
        )
        self._update_crop_label()

        self.player.set_embed_widget(self._preview_frame)
        self.player.set_tick_callback(self._on_tick)
        self.player.load(clip.path, on_ready=self._on_ready)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        scale = get_scale()
        tk.Label(self, text="Preview:", bg=BG, fg="white", font=("Courier", scale.font_lg)).pack(
            anchor="w", padx=scale.space_lg, pady=(scale.space_lg, scale.space_xs))

        crop_row = tk.Frame(self, bg=BG)
        crop_row.pack(fill="x", padx=scale.space_lg, pady=(0, scale.space_xs))
        tk.Label(crop_row, text="Crop aspect ratio:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).pack(side="left")
        self._aspect_var = tk.StringVar(value="Free")
        aspect_box = ttk.Combobox(crop_row, textvariable=self._aspect_var, state="readonly",
                                   width=8, font=("Courier", scale.font_sm), values=ASPECT_RATIO_LABELS)
        aspect_box.pack(side="left", padx=(scale.space_xs, scale.space_lg))
        aspect_box.bind("<<ComboboxSelected>>", self._on_aspect_change)
        self._crop_lbl = tk.Label(crop_row, text="", bg=BG, fg="#ccc", font=("Courier", scale.font_sm))
        self._crop_lbl.pack(side="left")

        self._preview_frame = tk.Frame(self, bg="black", height=scale.trim_preview_h)
        self._preview_frame.pack(fill="both", expand=True, padx=scale.space_lg)
        self._preview_frame.pack_propagate(False)

        ctrl = tk.Frame(self, bg=BG)
        ctrl.pack(fill="x", padx=scale.space_lg, pady=scale.space_xs)
        self._play_btn = tk.Button(ctrl, text="▶  Play", command=self._toggle_play,
                                    bg="#333", fg="white", relief="flat",
                                    font=("Courier", scale.font_lg), padx=scale.space_xl + 2)
        self._play_btn.pack(side="left")
        self._time_lbl = tk.Label(ctrl, text="--:--:--.--- / --:--:--.---",
                                   bg=BG, fg="#aaa", font=("Courier", scale.font_md))
        self._time_lbl.pack(side="left", padx=scale.space_xl)

        self._slider = RangeSlider(self)
        self._slider.pack(fill="x", padx=scale.space_lg, pady=scale.space_xxs)
        self._slider.add_callback(self._on_range_changed)
        self._slider.add_seek_callback(self._on_seek)

        bot = tk.Frame(self, bg=BG)
        bot.pack(fill="x", padx=scale.space_lg, pady=scale.space_sm)
        self._in_lbl = tk.Label(bot, text="In:  --:--:--.---", bg=BG, fg="#44cc44",
                                 font=("Courier", scale.font_md))
        self._in_lbl.pack(side="left")
        self._out_lbl = tk.Label(bot, text="    Out:  --:--:--.---", bg=BG, fg="#cc4444",
                                  font=("Courier", scale.font_md))
        self._out_lbl.pack(side="left")

        # 0-200%, default 0 (muted) -- see TimelineClip.audio_volume.
        vol_row = tk.Frame(self, bg=BG)
        vol_row.pack(fill="x", padx=scale.space_lg, pady=(0, scale.space_sm))
        tk.Label(vol_row, text="Audio volume %:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).pack(side="left")
        self._volume_var = tk.StringVar(value=str(round(self.clip.audio_volume * 100)))
        tk.Entry(vol_row, textvariable=self._volume_var, width=6,
                 font=("Courier", scale.font_sm)).pack(side="left", padx=scale.space_xs)

        actions = tk.Frame(self, bg=BG)
        actions.pack(fill="x", padx=scale.space_lg, pady=scale.space_sm)
        tk.Button(actions, text="Apply to Timeline", command=self._apply,
                  bg="#4a9eff", fg="white", relief="flat", font=("Courier", scale.font_lg, "bold")
                  ).pack(side="left")
        tk.Button(actions, text="Export Clip...", command=self._export_clip,
                  bg="#7a5fff", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="right", padx=scale.space_xs)
        tk.Button(actions, text="Export Frame...", command=self._export_frame,
                  bg="#7a5fff", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="right", padx=scale.space_xs)
        tk.Button(actions, text="Close", command=self._on_close_clicked,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="right", padx=scale.space_xs)

        prog = tk.Frame(self, bg=BG)
        prog.pack(fill="x", padx=scale.space_lg, pady=(0, scale.space_lg))
        self._progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self._progress.pack(fill="x", side="left", expand=True)
        self._status_lbl = tk.Label(prog, text="", bg=BG, fg="#aaa", font=("Courier", scale.font_sm))
        self._status_lbl.pack(side="right", padx=scale.space_md)

    # -------------------------------------------------------------- crop --

    def _video_content_rect(self, aw: int, ah: int) -> tuple[int, int, int, int]:
        """Where the actual video pixels land within _preview_frame (mpv
        letterboxes by default, keeping aspect and centering) -- the crop
        overlay tracks this sub-rect, not the whole frame, so its box lines
        up with real footage instead of any black bars."""
        scale = min(aw / self._src_w, ah / self._src_h)
        rw, rh = max(1, round(self._src_w * scale)), max(1, round(self._src_h * scale))
        return ((aw - rw) // 2, (ah - rh) // 2, rw, rh)

    def _on_aspect_change(self, _event=None):
        self._crop_overlay.set_aspect_ratio(ASPECT_RATIO_VALUES.get(self._aspect_var.get()))

    def _on_crop_overlay_change(self, top_f: float, bottom_f: float, left_f: float, right_f: float):
        self._crop_top = round(top_f * self._src_h)
        self._crop_bottom = round(bottom_f * self._src_h)
        self._crop_left = round(left_f * self._src_w)
        self._crop_right = round(right_f * self._src_w)
        self._update_crop_label()

    def _update_crop_label(self):
        out_w = self._src_w - self._crop_left - self._crop_right
        out_h = self._src_h - self._crop_top - self._crop_bottom
        self._crop_lbl.config(text=f"top={self._crop_top}px  bottom={self._crop_bottom}px  "
                                    f"left={self._crop_left}px  right={self._crop_right}px  ->  {out_w}x{out_h}")

    # ------------------------------------------------------------- loading --

    def _on_ready(self):
        d = self.player.duration_s
        if d <= 0:
            return
        self._slider.start = max(0.0, min(1.0, self.clip.in_s / d))
        self._slider.end = max(self._slider.start + 0.001, min(1.0, self.clip.out_s / d))
        self._slider.set_playhead(self._slider.start)
        self._update_in_out_labels()

    def _on_tick(self, pos_frac, duration_s, paused):
        self._slider.set_playhead(pos_frac)
        pos_ms = pos_frac * duration_s * 1000
        self._time_lbl.config(text=f"{fmt_time(pos_ms)} / {fmt_time(duration_s * 1000)}")
        self._play_btn.config(text="⏸  Pause" if not paused else "▶  Play")

    # ---------------------------------------------------------------- logic --

    def _toggle_play(self):
        self.player.play_pause()

    def _on_range_changed(self, _start, _end):
        self._update_in_out_labels()

    def _on_seek(self, frac: float):
        self.player.seek(frac)

    def _update_in_out_labels(self):
        d = self.player.duration_s
        if d <= 0:
            return
        self._in_lbl.config(text=f"In:  {fmt_time(self._slider.start * d * 1000)}")
        self._out_lbl.config(text=f"    Out:  {fmt_time(self._slider.end * d * 1000)}")

    def _apply(self):
        d = self.player.duration_s
        if d <= 0:
            return
        self.clip.in_s = self._slider.start * d
        self.clip.out_s = self._slider.end * d
        self.clip.crop_top = self._crop_top
        self.clip.crop_bottom = self._crop_bottom
        self.clip.crop_left = self._crop_left
        self.clip.crop_right = self._crop_right
        try:
            self.clip.audio_volume = max(0.0, min(2.0, float(self._volume_var.get() or 0) / 100.0))
        except ValueError:
            pass
        self.on_change()
        log.info("Clip trim editor: applied %.2fs-%.2fs to %s",
                  self.clip.in_s, self.clip.out_s, self.clip.path.name)
        self.teardown()
        self.on_close()

    def _export_clip(self):
        d = self.player.duration_s
        if d <= 0:
            return
        in_s = self._slider.start * d
        out_s = self._slider.end * d
        default_name = f"{self.clip.path.stem}_{fmt_time(in_s * 1000).replace(':', '-').replace('.', '_')}.mp4"
        self.player.pause()
        self.player.suspend_polling()  # see SharedPlayer.suspend_polling

        def on_path(out_path: Path | None):
            self.player.resume_polling()
            if not out_path:
                return
            self._run_export_clip(in_s, out_s, out_path)

        ask_save_filename(self, on_path, title="Export clip as...", defaultextension=".mp4",
                           initialfile=default_name, filetypes=[("MP4 video", "*.mp4")])

    def _run_export_clip(self, in_s: float, out_s: float, out_path: Path):
        self._progress["value"] = 0
        self._status_lbl.config(text="Exporting...")

        def on_progress(frac, _label):
            self.after(0, lambda: self._progress.config(value=frac * 100))

        def run():
            ok = ffmpeg_ops.export_clip(self.clip.path, in_s, out_s, out_path, on_progress)
            self.after(0, lambda: self._status_lbl.config(
                text=f"Saved: {out_path.name}" if ok else "Export failed (see console)"))

        threading.Thread(target=run, daemon=True).start()

    def _export_frame(self):
        d = self.player.duration_s
        if d <= 0:
            return
        pos_s = self._slider.playhead * d
        default_name = f"{self.clip.path.stem}_{fmt_time(pos_s * 1000).replace(':', '-').replace('.', '_')}.png"
        self.player.pause()
        self.player.suspend_polling()  # see SharedPlayer.suspend_polling

        def on_path(out_path: Path | None):
            self.player.resume_polling()
            if not out_path:
                return
            self._run_export_frame(pos_s, out_path)

        ask_save_filename(self, on_path, title="Export frame as...", defaultextension=".png",
                           initialfile=default_name, filetypes=[("PNG image", "*.png")])

    def _run_export_frame(self, pos_s: float, out_path: Path):
        self._status_lbl.config(text="Exporting frame...")

        def run():
            ok = ffmpeg_ops.export_frame(self.clip.path, pos_s, out_path)
            self.after(0, lambda: self._status_lbl.config(
                text=f"Saved: {out_path.name}" if ok else "Export failed (see console)"))

        threading.Thread(target=run, daemon=True).start()

    # --------------------------------------------------------------- close --

    def _on_close_clicked(self):
        self.teardown()
        self.on_close()

    def teardown(self):
        """Idempotent cleanup of non-Tk resources (the mpv subprocess, the
        floating crop-overlay window) -- called whether this tab closes via
        its own Apply/Close buttons or gets force-replaced/rebuilt by the
        owning TimelineTab."""
        self.player.terminate()
        if self._crop_overlay is not None:
            self._crop_overlay.destroy()
            self._crop_overlay = None
