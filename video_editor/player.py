"""Single shared mpv player used across all tabs. Normally embeds directly into
a Tk widget the owning tab designates via set_embed_widget() (mpv's `wid` option),
so playback renders inline in a fixed pane instead of a floating OS window. Falls
back to a floating window snapped beside the main app if no embed widget is set.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import mpv

from .logging_config import get_logger

log = get_logger()

TickCB = Optional[Callable[[float, float, bool], None]]  # (pos_frac, duration_s, paused)


class SharedPlayer:
    def __init__(self, root: tk.Tk, window_width: int = 480, window_x_offset: int = 0):
        self._root = root
        self._window_width = window_width
        self._window_x_offset = window_x_offset
        self._player: Optional[mpv.MPV] = None
        self._duration_s = 0.0
        self._poll_id = None
        self._tick_cb: TickCB = None
        self._current_path: Optional[Path] = None
        self._embed_widget: Optional[tk.Widget] = None
        # Bumped on every load()/terminate() so a _wait_for_duration poll
        # loop started by an earlier load() can tell it's been superseded and
        # stop instead of eventually firing its on_ready against whatever
        # player instance happens to occupy self._player by the time its
        # duration resolves (see _wait_for_duration).
        self._generation = 0

    def set_embed_widget(self, widget: Optional[tk.Widget]):
        """Points subsequent .load() calls at this widget (mpv embeds via its
        native window handle). Pass None to fall back to a floating window."""
        self._embed_widget = widget

    def _snap_geometry(self) -> str:
        """Positions the mpv window to the right of the main app window, matching
        its height, so playback windows don't scatter across the screen."""
        self._root.update_idletasks()
        x = self._root.winfo_x() + self._root.winfo_width() + self._window_x_offset
        y = self._root.winfo_y()
        h = max(200, self._root.winfo_height())
        return f"{self._window_width}x{h}+{x}+{y}"

    @property
    def duration_s(self) -> float:
        return self._duration_s

    @property
    def current_path(self) -> Optional[Path]:
        return self._current_path

    def set_tick_callback(self, fn: TickCB):
        """Only one tab should own the callback at a time; the active tab sets
        this when it becomes visible and clears it (pass None) when it hides."""
        self._tick_cb = fn

    def load(self, path: Path, on_ready: Optional[Callable[[], None]] = None, is_image: bool = False):
        self._stop_polling()
        if self._player is not None:
            self._player.terminate()
            self._player = None
        self._generation += 1
        generation = self._generation

        log.info("Player: loading %s", path.name)
        self._current_path = path
        self._duration_s = 0.0
        try:
            if self._embed_widget is not None:
                self._embed_widget.update_idletasks()
                wid = str(self._embed_widget.winfo_id())
                self._player = mpv.MPV(wid=wid, keep_open=True, pause=True)
            else:
                self._player = mpv.MPV(keep_open=True, pause=True, geometry=self._snap_geometry())
            self._player.play(str(path))
        except Exception:
            log.exception("Player: failed to load %s", path)
            self._player = None
            return

        if is_image:
            # mpv never reports a positive `duration` for a still image, so
            # the normal wait-for-duration poll below would hang forever.
            self._duration_s = 1.0
            self._start_polling()
            if on_ready:
                on_ready()
        else:
            self._wait_for_duration(on_ready, generation, attempt=0)

    def _wait_for_duration(self, on_ready, generation: int, attempt: int):
        # generation guards against a poll loop from a previous load() still
        # running when a newer load() has since replaced self._player --
        # without this check it would keep polling the *new* player and could
        # eventually fire the *old* on_ready callback against the new clip.
        if generation != self._generation or self._player is None:
            return
        if attempt >= 100:  # ~10s at 100ms -- duration never resolved (corrupt/unsupported file)
            log.error("Player: timed out waiting for duration of %s, giving up", self._current_path)
            return
        try:
            d = self._player.duration
        except Exception:
            d = None
        if d is None or d <= 0:
            self._root.after(100, lambda: self._wait_for_duration(on_ready, generation, attempt + 1))
            return
        self._duration_s = d
        self._start_polling()
        if on_ready:
            on_ready()

    def play_pause(self):
        if self._player is None:
            return
        self._player.pause = not self._player.pause

    def is_paused(self) -> bool:
        if self._player is None:
            return True
        try:
            return bool(self._player.pause)
        except Exception:
            return True

    def pause(self):
        if self._player is not None:
            self._player.pause = True

    def suspend_polling(self):
        """Stops the periodic mpv IPC poll without touching playback state.
        Call before any native OS dialog (file/folder picker) -- Windows'
        common-item-dialog runs its own nested message loop while Python is
        blocked inside it, and Tcl's `after()` timers still fire through that
        nested loop, so the poll below can hit a live mpv instance re-entrantly
        while the dialog is open. Pair with resume_polling() once the dialog
        call returns."""
        self._stop_polling()

    def resume_polling(self):
        if self._player is not None and self._poll_id is None:
            self._start_polling()

    def current_pos_s(self) -> float:
        if self._player is None:
            return 0.0
        try:
            return float(self._player.time_pos or 0.0)
        except Exception:
            return 0.0

    def seek(self, frac: float):
        if self._player is None or self._duration_s <= 0:
            return
        self._player.pause = True
        frac = max(0.0, min(1.0, frac))
        self._player.seek(frac * self._duration_s, reference="absolute", precision="exact")

    def _start_polling(self):
        self._stop_polling()
        self._poll()

    def _stop_polling(self):
        if self._poll_id is not None:
            self._root.after_cancel(self._poll_id)
            self._poll_id = None

    def _poll(self):
        if self._player is None:
            return
        try:
            pos_s = self._player.time_pos
            paused = self._player.pause
        except Exception:
            pos_s, paused = None, True

        if self._tick_cb and self._duration_s > 0 and pos_s is not None:
            self._tick_cb(pos_s / self._duration_s, self._duration_s, bool(paused))

        self._poll_id = self._root.after(80, self._poll)

    def terminate(self):
        self._stop_polling()
        self._generation += 1
        if self._player is not None:
            self._player.terminate()
            self._player = None
