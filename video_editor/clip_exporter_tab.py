from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from . import ffmpeg_ops
from .logging_config import get_logger
from .native_dialog import ask_directory
from .player import SharedPlayer
from .ui_scale import get_scale
from .widgets import RangeSlider, fmt_time

log = get_logger()

BG = "#1e1e1e"


class ClipExporterTab(tk.Frame):
    def __init__(self, parent, state, player: SharedPlayer):
        super().__init__(parent, bg=BG)
        self.state = state
        self.player = player

        self._input_dir: Path | None = None
        self._output_dir: Path | None = None
        self._video_paths: dict[str, Path] = {}

        self._build_ui()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        scale = get_scale()
        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=60)
        container.grid_columnconfigure(1, weight=0, minsize=2)
        container.grid_columnconfigure(2, weight=40)

        # grid_propagate(False): a weighted grid column only gets *extra* space
        # beyond its content's natural minimum width -- without this, a long
        # folder-path label would force this column wider than its 60% share.
        menu = tk.Frame(container, bg=BG)
        menu.grid(row=0, column=0, sticky="nsew")
        menu.grid_propagate(False)
        ttk.Separator(container, orient="vertical").grid(row=0, column=1, sticky="ns")
        preview_pane = tk.Frame(container, bg=BG)
        preview_pane.grid(row=0, column=2, sticky="nsew")
        preview_pane.grid_propagate(False)

        folders = tk.Frame(menu, bg=BG)
        folders.pack(fill="x", padx=scale.space_lg, pady=scale.space_sm)
        self._input_lbl = tk.Label(folders, text="Input: (none)", bg=BG, fg="#aaa",
                                    font=("Courier", scale.font_md), anchor="w", justify="left",
                                    wraplength=scale.wraplength_folder_label)
        self._input_lbl.pack(side="left", fill="x", expand=True)
        tk.Button(folders, text="Browse input...", command=self._pick_input,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="left", padx=scale.space_xs)

        folders2 = tk.Frame(menu, bg=BG)
        folders2.pack(fill="x", padx=scale.space_lg, pady=scale.space_xxs)
        self._output_lbl = tk.Label(folders2, text="Output: (none)", bg=BG, fg="#aaa",
                                     font=("Courier", scale.font_md), anchor="w", justify="left",
                                     wraplength=scale.wraplength_folder_label)
        self._output_lbl.pack(side="left", fill="x", expand=True)
        tk.Button(folders2, text="Browse output...", command=self._pick_output,
                  bg="#333", fg="white", relief="flat", font=("Courier", scale.font_sm)
                  ).pack(side="left", padx=scale.space_xs)

        top = tk.Frame(menu, bg=BG)
        top.pack(fill="x", padx=scale.space_lg, pady=scale.space_sm)
        tk.Label(top, text="Video:", bg=BG, fg="white", font=("Courier", scale.font_lg)).pack(side="left")
        self._video_var = tk.StringVar()
        self._video_menu = ttk.Combobox(top, textvariable=self._video_var, state="readonly", width=55,
                                         font=("Courier", scale.font_sm))
        self._video_menu.pack(side="left", padx=scale.space_md)
        self._video_menu.bind("<<ComboboxSelected>>", lambda _e: self._load_current_video())

        ctrl = tk.Frame(menu, bg=BG)
        ctrl.pack(fill="x", padx=scale.space_lg, pady=scale.space_xs)
        self._play_btn = tk.Button(ctrl, text="▶  Play", command=self._toggle_play,
                                    bg="#333", fg="white", relief="flat",
                                    font=("Courier", scale.font_lg), padx=scale.space_xl + 2)
        self._play_btn.pack(side="left")
        self._time_lbl = tk.Label(ctrl, text="--:--:--.--- / --:--:--.---",
                                   bg=BG, fg="#aaa", font=("Courier", scale.font_md))
        self._time_lbl.pack(side="left", padx=scale.space_xl)

        self._slider = RangeSlider(menu)
        self._slider.pack(fill="x", padx=scale.space_lg, pady=scale.space_xxs)
        self._slider.add_callback(self._on_range_changed)
        self._slider.add_seek_callback(self._on_seek)

        bot = tk.Frame(menu, bg=BG)
        bot.pack(fill="x", padx=scale.space_lg, pady=scale.space_sm)
        self._in_lbl = tk.Label(bot, text="In:  --:--:--.---", bg=BG, fg="#44cc44",
                                 font=("Courier", scale.font_md))
        self._in_lbl.pack(side="left")
        self._out_lbl = tk.Label(bot, text="    Out:  --:--:--.---", bg=BG, fg="#cc4444",
                                  font=("Courier", scale.font_md))
        self._out_lbl.pack(side="left")

        self._export_btn = tk.Button(bot, text="Export Clip", command=self._export,
                                      bg="#4a9eff", fg="white", relief="flat",
                                      font=("Courier", scale.font_lg, "bold"), padx=scale.space_xxl)
        self._export_btn.pack(side="right")
        self._frame_btn = tk.Button(bot, text="Export Frame", command=self._export_frame,
                                     bg="#7a5fff", fg="white", relief="flat",
                                     font=("Courier", scale.font_lg, "bold"), padx=scale.space_xxl)
        self._frame_btn.pack(side="right", padx=scale.space_md)

        prog = tk.Frame(menu, bg=BG)
        prog.pack(fill="x", padx=scale.space_lg, pady=(0, scale.space_sm))
        self._progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self._progress.pack(fill="x", side="left", expand=True)
        self._status_lbl = tk.Label(prog, text="", bg=BG, fg="#aaa", font=("Courier", scale.font_md))
        self._status_lbl.pack(side="right", padx=scale.space_md)

        tk.Label(preview_pane, text="Preview:", bg=BG, fg="white", font=("Courier", scale.font_lg)).pack(
            anchor="w", padx=scale.space_lg, pady=(scale.space_lg, scale.space_xs))
        self._preview_frame = tk.Frame(preview_pane, bg="black")
        self._preview_frame.pack(fill="both", expand=True, padx=scale.space_lg, pady=(0, scale.space_lg))

    def rebuild(self):
        """Tears down and rebuilds this tab's widgets from the current UI
        scale profile, restoring folder state and re-embedding the player if
        this tab happens to be the active one (caller re-invokes on_show())."""
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        if self._input_dir is not None:
            self._input_lbl.config(text=f"Input: {self._input_dir}")
            self._load_video_list()
        if self._output_dir is not None:
            self._output_lbl.config(text=f"Output: {self._output_dir}")

    # -------------------------------------------------------------- folders --

    def _pick_input(self):
        self.player.pause()  # avoid a load racing the dialog subprocess while it's open
        self.player.suspend_polling()
        default = str(self.state.crop_output_dir) if self.state.crop_output_dir else None

        def done(d: Path | None):
            self.player.resume_polling()
            if not d:
                return
            self._input_dir = d
            self._input_lbl.config(text=f"Input: {d}")
            self.state.clip_input_dir = self._input_dir
            self._load_video_list()

        ask_directory(self, done, title="Select input folder", initialdir=default or "")

    def _pick_output(self):
        self.player.pause()
        self.player.suspend_polling()

        def done(d: Path | None):
            self.player.resume_polling()
            if not d:
                return
            self._output_dir = d
            self._output_lbl.config(text=f"Output: {d}")
            self.state.clip_output_dir = self._output_dir

        ask_directory(self, done, title="Select output folder")

    def apply_chained_defaults(self):
        """Called when this tab becomes visible; pick up the Crop tab's output
        folder as our input folder if we don't have one set yet."""
        if self._input_dir is None and self.state.crop_output_dir is not None:
            self._input_dir = self.state.crop_output_dir
            self._input_lbl.config(text=f"Input: {self._input_dir}")
            self._load_video_list()

    # --------------------------------------------------------- video loading --

    def _load_video_list(self):
        if not self._input_dir:
            return
        files = sorted(self._input_dir.glob("*.mp4"))
        self._video_paths = {f.name: f for f in files}
        self._video_menu["values"] = list(self._video_paths)
        if files:
            self._video_menu.current(0)
            self._load_current_video()
        log.info("Clip Exporter: found %d videos in %s", len(files), self._input_dir)

    def _load_current_video(self):
        name = self._video_var.get()
        path = self._video_paths.get(name)
        if not path:
            return
        self._slider.start = 0.0
        self._slider.end = 1.0
        self._slider.set_playhead(0.0)
        self._status_lbl.config(text="")

        def on_ready():
            self._update_in_out_labels()

        self.player.load(path, on_ready=on_ready)
        log.info("Clip Exporter: loaded %s", name)

    # -------------------------------------------------------------- tab focus --

    def on_show(self):
        # Must set the embed target before apply_chained_defaults(), since that
        # can trigger an immediate video auto-load -- otherwise that first load
        # falls back to a floating window instead of embedding.
        self.player.set_embed_widget(self._preview_frame)
        self.player.set_tick_callback(self._on_tick)
        self.apply_chained_defaults()

    def on_hide(self):
        self.player.set_tick_callback(None)

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

    def _export(self):
        name = self._video_var.get()
        path = self._video_paths.get(name)
        d = self.player.duration_s
        if not path or d <= 0 or not self._output_dir:
            self._status_lbl.config(text="Pick output folder and a video first.")
            return

        in_s = self._slider.start * d
        out_s = self._slider.end * d
        in_tag = fmt_time(in_s * 1000).replace(":", "-").replace(".", "_")
        out_name = f"{path.stem}_{in_tag}.mp4"
        out_path = self._output_dir / out_name

        self._export_btn.config(state="disabled")
        self._progress["value"] = 0
        self._status_lbl.config(text="Exporting...")

        def on_progress(frac, _label):
            self.after(0, lambda: self._progress.config(value=frac * 100))

        def run():
            ok = ffmpeg_ops.export_clip(path, in_s, out_s, out_path, on_progress)
            self.after(0, lambda: (
                self._status_lbl.config(text=f"Saved: {out_name}" if ok else "Export failed (see console)"),
                self._export_btn.config(state="normal"),
            ))

        threading.Thread(target=run, daemon=True).start()

    def _export_frame(self):
        name = self._video_var.get()
        path = self._video_paths.get(name)
        d = self.player.duration_s
        if not path or d <= 0 or not self._output_dir:
            self._status_lbl.config(text="Pick output folder and a video first.")
            return

        pos_s = self._slider.playhead * d
        time_tag = fmt_time(pos_s * 1000).replace(":", "-").replace(".", "_")
        out_name = f"{path.stem}_{time_tag}.png"
        out_path = self._output_dir / out_name

        self._frame_btn.config(state="disabled")
        self._status_lbl.config(text="Exporting frame...")

        def run():
            ok = ffmpeg_ops.export_frame(path, pos_s, out_path)
            self.after(0, lambda: (
                self._status_lbl.config(text=f"Saved: {out_name}" if ok else "Export failed (see console)"),
                self._frame_btn.config(state="normal"),
            ))

        threading.Thread(target=run, daemon=True).start()
