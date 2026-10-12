from __future__ import annotations

import json
import tempfile
import tkinter as tk
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import ttk
from typing import Optional

from .clip_exporter_tab import ClipExporterTab
from .crop_resize_tab import CropResizeTab
from .logging_config import get_logger
from .native_dialog import terminate_all_dialog_processes
from .player import SharedPlayer
from .timeline_tab import TimelineTab
from .ui_scale import available_profiles, get_scale, set_profile

log = get_logger()

STATE_FILE = Path(__file__).parent / ".state.json"

# Bake-preview temp dirs used by image_clip_editor/sound_clip_editor/text_overlay_editor.
# Each editor only deletes its own bake file on next-bake or on its own teardown()
# (Apply/Close) -- if the app is killed/crashes mid-session those files are
# orphaned there permanently. Swept once here, before any editor tab can open
# and start writing into these dirs, so this can never race a live bake.
_BAKE_TMP_DIR_NAMES = (
    "video_editor_image_preview",
    "video_editor_sound_preview",
    "video_editor_text_preview",
)


def _cleanup_stale_bake_temp_dirs() -> None:
    for name in _BAKE_TMP_DIR_NAMES:
        d = Path(tempfile.gettempdir()) / name
        if not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                log.exception("Failed to remove stale bake temp file %s", f)


@dataclass
class AppState:
    """Remembers per-tab folder choices across launches and lets later tabs
    default to an earlier tab's output folder (crop -> clip exporter -> timeline)."""
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # Tk 8.6.13+ auto-rescales widget/font pixel sizes on Windows when a
        # window is dragged to a monitor with different DPI (layered on top
        # of the Per-Monitor-V2 awareness declared in run_video_editor.py).
        # That fights the app's own manual 1080p/4k profile system, causing
        # visible reflow mid-drag -- pinning the scaling factor to whatever
        # it auto-detected at startup makes Tk stop re-adjusting it later.
        self.tk.call("tk", "scaling", self.tk.call("tk", "scaling"))
        self.title("Video Editor")
        self.configure(bg="#1e1e1e")
        self.geometry(get_scale().app_window_geometry)
        self.state("zoomed")

        _cleanup_stale_bake_temp_dirs()
        self.state_data = AppState.load()
        # `player` is shared by Clip Exporter and Timeline's library-browse preview
        # (they're never both visible at once). Timeline's composed-preview gets its
        # own instance so both windows can be open side by side.
        self.player = SharedPlayer(self, window_x_offset=0)
        self.timeline_preview_player = SharedPlayer(self, window_x_offset=480)

        self._apply_ttk_style()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.crop_tab = CropResizeTab(self.notebook, self.state_data)
        self.clip_tab = ClipExporterTab(self.notebook, self.state_data, self.player)
        self.timeline_tab = TimelineTab(self.notebook, self.state_data, self.player, self.timeline_preview_player)

        self.notebook.add(self.crop_tab, text="Crop & Resize")
        self.notebook.add(self.clip_tab, text="Clip Exporter")
        self.notebook.add(self.timeline_tab, text="Timeline")

        # Built after the Notebook (and lift()'d) so it stacks visually on top
        # of the tab strip's own background -- ttk.Notebook has no public API
        # for adding extra widgets into its tab-strip row, so this overlays the
        # otherwise-empty space to the right of the last tab instead.
        self._build_scale_bar()

        self._focusable_tabs = [self.clip_tab, self.timeline_tab]
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.notebook.select(self.timeline_tab)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        log.info("App started")

    def _build_scale_bar(self):
        """Top-right 1080p/4K toggle, overlaid via place() on the same row as
        the Notebook's tab strip (see the call site for why place()+lift()
        instead of pack()). Widgets are built once and reconfigured in place
        on scale switches (rather than destroyed/rebuilt)."""
        self._scale_bar = tk.Frame(self, bg="#2a2a2a")
        self._scale_bar.place(relx=1.0, y=1, anchor="ne")
        self._scale_label = tk.Label(self._scale_bar, text="UI scale:", bg="#2a2a2a", fg="#888")
        self._scale_label.pack(side="left", padx=(8, 4), pady=1)
        self._scale_buttons: dict[str, tk.Button] = {}
        for name in available_profiles():
            btn = tk.Button(self._scale_bar, text=name, command=lambda n=name: self.set_ui_scale(n),
                             relief="flat", pady=0)
            btn.pack(side="left", padx=2, pady=1)
            self._scale_buttons[name] = btn
        self._scale_bar.lift()
        self._refresh_scale_bar()

    def _apply_ttk_style(self):
        """ttk widgets (Notebook tabs, Combobox, Progressbar) are themed via
        ttk.Style, not per-widget .config(font=...) -- unlike the tk widgets
        elsewhere in the app, style changes apply live to existing widgets,
        so this doesn't need a rebuild() call, just a re-invoke on switch.
        The default Windows theme ('vista') ignores background/foreground
        style options on Notebook tabs, so 'clam' is required to actually
        get a dark-themed tab strip instead of the stock white/grey one."""
        scale = get_scale()
        style = ttk.Style()
        if style.theme_use() != "clam" and "clam" in style.theme_names():
            style.theme_use("clam")
        font = ("Courier", scale.font_md)
        style.configure("TNotebook", background="#1e1e1e", borderwidth=0)
        style.configure("TNotebook.Tab", background="#2a2a2a", foreground="#dddddd",
                         font=font, padding=(scale.space_lg, scale.space_sm), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", "#4a9eff"), ("active", "#3a3a3a")],
                  foreground=[("selected", "#ffffff"), ("!selected", "#dddddd")])

        # A Combobox's dropdown list is a plain Tk Listbox popped up by the
        # theme engine, not a themable ttk element -- the per-widget font=
        # kwarg only sets the closed entry field's font, not this listbox's.
        # It's controlled through the Tk option database instead, which is
        # global (no per-instance override), so every dropdown in the app
        # gets one shared size tied to the active profile.
        self.option_add("*TCombobox*Listbox.font", ("Courier", scale.font_sm))

    def _refresh_scale_bar(self):
        scale = get_scale()
        font = ("Courier", scale.font_sm)
        self._scale_label.config(font=font)
        active = scale.name
        for name, btn in self._scale_buttons.items():
            btn.config(font=font, padx=scale.space_md)
            if name == active:
                btn.config(bg="#4a9eff", fg="white")
            else:
                btn.config(bg="#333", fg="white")

    def set_ui_scale(self, name: str):
        """Switches the active size profile and rebuilds every tab so the new
        sizes actually take effect (Tk widgets don't live-reconfigure)."""
        if name == get_scale().name:
            return
        set_profile(name)
        self._refresh_scale_bar()
        self._apply_ttk_style()
        for tab in (self.crop_tab, self.clip_tab, self.timeline_tab):
            tab.rebuild()
        selected = self.notebook.nametowidget(self.notebook.select())
        if selected in self._focusable_tabs:
            selected.on_show()
        log.info("App: UI scale switched to %s", name)

    def _on_tab_changed(self, event):
        notebook = event.widget
        selected = notebook.nametowidget(notebook.select())
        for tab in self._focusable_tabs:
            if tab is selected:
                tab.on_show()
            else:
                tab.on_hide()

    def _on_close(self):
        log.info("App closing")
        terminate_all_dialog_processes()
        self.player.terminate()
        self.timeline_preview_player.terminate()
        self.state_data.save()
        self.destroy()
