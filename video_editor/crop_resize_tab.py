from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from PIL import Image

from . import ffmpeg_ops
from .crop_box import ASPECT_RATIO_LABELS, ASPECT_RATIO_VALUES
from .logging_config import get_logger
from .native_dialog import ask_directory
from .ui_scale import get_scale
from .widgets import CropCanvas, ScrollableFrame

log = get_logger()

BG = "#1e1e1e"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp"}


class CropResizeTab(tk.Frame):
    def __init__(self, parent, state):
        super().__init__(parent, bg=BG)
        self.state = state  # AppState, for chained folder defaults

        self._input_dir: Path | None = None
        self._output_dir: Path | None = None
        self._checks: dict[str, tk.BooleanVar] = {}
        self._files: list[Path] = []

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

        body = tk.Frame(menu, bg=BG)
        body.pack(fill="both", expand=True, padx=scale.space_lg, pady=scale.space_sm)

        tk.Label(body, text="Clips to apply to:", bg=BG, fg="white",
                 font=("Courier", scale.font_lg)).pack(anchor="w")
        self._checklist = ScrollableFrame(body, height=scale.crop_checklist_h, bg=BG,
                                           width=scale.crop_checklist_w)
        self._checklist.pack(fill="x")

        size_row = tk.Frame(body, bg=BG)
        size_row.pack(fill="x", pady=scale.space_md)
        tk.Label(size_row, text="Target W:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).grid(row=0, column=0, sticky="w")
        self._target_w = tk.Entry(size_row, width=8, font=("Courier", scale.font_sm))
        self._target_w.grid(row=0, column=1, padx=scale.space_xs)
        tk.Label(size_row, text="Target H:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).grid(row=1, column=0, sticky="w")
        self._target_h = tk.Entry(size_row, width=8, font=("Courier", scale.font_sm))
        self._target_h.grid(row=1, column=1, padx=scale.space_xs)
        tk.Label(size_row, text="(blank = keep cropped size)", bg=BG, fg="#777",
                 font=("Courier", scale.font_xs)).grid(row=2, column=0, columnspan=2, sticky="w")

        self._run_btn = tk.Button(body, text="Run Crop/Resize", command=self._run,
                                   bg="#4a9eff", fg="white", relief="flat",
                                   font=("Courier", scale.font_lg, "bold"))
        self._run_btn.pack(fill="x", pady=scale.space_md)

        self._progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self._progress.pack(fill="x")
        self._status_lbl = tk.Label(body, text="", bg=BG, fg="#aaa", font=("Courier", scale.font_sm))
        self._status_lbl.pack(anchor="w")

        preview_header = tk.Frame(preview_pane, bg=BG)
        preview_header.pack(fill="x", padx=scale.space_lg, pady=(scale.space_lg, 0))
        tk.Label(preview_header, text="Crop preview (drag box):", bg=BG, fg="white",
                 font=("Courier", scale.font_lg)).pack(side="left")
        tk.Label(preview_header, text="Aspect ratio:", bg=BG, fg="white",
                 font=("Courier", scale.font_md)).pack(side="left", padx=(scale.space_lg, scale.space_xs))
        self._aspect_var = tk.StringVar(value="Free")
        aspect_box = ttk.Combobox(preview_header, textvariable=self._aspect_var, state="readonly",
                                   width=8, font=("Courier", scale.font_sm),
                                   values=ASPECT_RATIO_LABELS)
        aspect_box.pack(side="left")
        aspect_box.bind("<<ComboboxSelected>>", self._on_aspect_change)

        self._crop_canvas = CropCanvas(preview_pane, bg=BG)
        self._crop_canvas.pack(fill="both", expand=True, anchor="nw",
                                padx=scale.space_lg, pady=scale.space_xs)
        self._preview_header = preview_header
        preview_pane.bind("<Configure>", self._on_preview_resize)

    def rebuild(self):
        """Tears down and rebuilds this tab's widgets from the current UI
        scale profile, restoring whatever folder/file state was already set."""
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        if self._input_dir is not None:
            self._input_lbl.config(text=f"Input: {self._input_dir}")
            self._refresh_files()
        if self._output_dir is not None:
            self._output_lbl.config(text=f"Output: {self._output_dir}")

    # -------------------------------------------------------------- folders --

    def _pick_input(self):
        def done(d: Path | None):
            if not d:
                return
            self._input_dir = d
            self._input_lbl.config(text=f"Input: {d}")
            self.state.crop_input_dir = self._input_dir
            self._refresh_files()

        ask_directory(self, done, title="Select input folder")

    def _pick_output(self):
        def done(d: Path | None):
            if not d:
                return
            self._output_dir = d
            self._output_lbl.config(text=f"Output: {d}")
            self.state.crop_output_dir = self._output_dir

        ask_directory(self, done, title="Select output folder")

    def _refresh_files(self):
        if not self._input_dir:
            return
        self._files = sorted(
            p for p in self._input_dir.iterdir()
            if p.suffix.lower() == ".mp4" or p.suffix.lower() in IMAGE_EXTS
        )
        self._checklist.clear()
        self._checks = {}
        for f in self._files:
            var = tk.BooleanVar(value=True)
            self._checks[f.name] = var
            tk.Checkbutton(self._checklist.body, text=f.name, variable=var,
                            bg=BG, fg="white", selectcolor="#333",
                            activebackground=BG, activeforeground="white",
                            font=("Courier", get_scale().font_sm), anchor="w").pack(fill="x", anchor="w")
        if self._files:
            self._load_preview_frame(self._files[0])
        log.info("Crop/Resize: found %d files in %s", len(self._files), self._input_dir)

    def _on_aspect_change(self, _event=None):
        ratio = ASPECT_RATIO_VALUES.get(self._aspect_var.get())
        self._crop_canvas.set_aspect_ratio(ratio)

    def _on_preview_resize(self, event):
        scale = get_scale()
        avail_w = event.width - 2 * scale.space_lg
        avail_h = (event.height - self._preview_header.winfo_reqheight()
                   - scale.space_lg - 2 * scale.space_xs)
        self._crop_canvas.set_max_size(avail_w, avail_h)

    def _load_preview_frame(self, path: Path):
        try:
            if path.suffix.lower() == ".mp4":
                probe_frame = ffmpeg_ops.extract_thumbnail(path, 0.0, out_w=1200)
                if probe_frame:
                    from .widgets import bytes_to_image
                    self._crop_canvas.set_image(bytes_to_image(probe_frame))
            else:
                self._crop_canvas.set_image(Image.open(path))
        except Exception:
            log.exception("Crop/Resize: failed to load preview frame for %s", path)
            self._status_lbl.config(text=f"Could not preview {path.name} (see log).")

    # ---------------------------------------------------------------- run --

    def _run(self):
        if not self._input_dir or not self._output_dir:
            self._status_lbl.config(text="Pick input and output folders first.")
            return
        selected = [f for f in self._files if self._checks[f.name].get()]
        if not selected:
            self._status_lbl.config(text="No clips selected.")
            return

        crop_fractions = self._crop_canvas.get_crop_fractions()
        try:
            tw = int(self._target_w.get()) if self._target_w.get().strip() else None
            th = int(self._target_h.get()) if self._target_h.get().strip() else None
        except ValueError:
            self._status_lbl.config(text="Target W/H must be integers.")
            return

        videos = [f for f in selected if f.suffix.lower() == ".mp4"]
        images = [f for f in selected if f.suffix.lower() in IMAGE_EXTS]

        self._run_btn.config(state="disabled")
        self._progress["value"] = 0
        self._status_lbl.config(text="Running...")

        def on_progress(frac, msg):
            self.after(0, lambda: (self._progress.config(value=frac * 100),
                                    self._status_lbl.config(text=msg)))

        def run():
            log.info("Crop/Resize batch start: %d videos, %d images", len(videos), len(images))
            failed = []
            if videos:
                try:
                    ffmpeg_ops.run_crop_resize(videos, self._output_dir, crop_fractions, (tw, th), on_progress)
                except Exception:
                    log.exception("Crop/Resize: video batch failed")
                    failed.extend(f.name for f in videos)
            for img_path in images:
                try:
                    self._crop_resize_image(img_path, crop_fractions, tw, th)
                except Exception:
                    log.exception("Crop/Resize: failed to process image %s", img_path)
                    failed.append(img_path.name)
            done_msg = f"Done. {len(selected) - len(failed)}/{len(selected)} file(s) processed."
            if failed:
                done_msg += f" Failed: {', '.join(failed[:5])}{'...' if len(failed) > 5 else ''} (see log)."
            self.after(0, lambda: (
                self._status_lbl.config(text=done_msg),
                self._run_btn.config(state="normal"),
            ))
            log.info("Crop/Resize batch done (%d failed)", len(failed))

        threading.Thread(target=run, daemon=True).start()

    def _crop_resize_image(self, path: Path, crop_fractions, tw, th):
        top_f, bottom_f, left_f, right_f = crop_fractions
        img = Image.open(path)
        w, h = img.size
        top, bottom = round(top_f * h), round(bottom_f * h)
        left, right = round(left_f * w), round(right_f * w)
        cw, ch = w - left - right, h - top - bottom
        img = img.crop((left, top, left + cw, top + ch))
        out_w = tw if tw else cw
        out_h = th if th else ch
        img = img.resize((out_w, out_h), Image.LANCZOS)
        img.save(self._output_dir / path.name)
        log.info("Cropped image %s -> %dx%d", path.name, out_w, out_h)
