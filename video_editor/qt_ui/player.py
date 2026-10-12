"""Single shared mpv player used across all tabs. Normally embeds directly
into a QWidget the owning tab designates via set_embed_widget() (mpv's `wid`
option, driven off the widget's native winId()), so playback renders inline
in a fixed pane instead of a floating OS window. Falls back to a floating
window snapped beside the main app if no embed widget is set.
"""
from __future__ import annotations

import weakref
from pathlib import Path
from typing import Callable, Optional

import mpv
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from ..logging_config import get_logger

log = get_logger()

TickCB = Optional[Callable[[float, float, bool], None]]  # (pos_frac, duration_s, paused)

# Every SharedPlayer registers itself here (see __init__) so dialogs.py can
# release every embedded mpv instance in the app before any native dialog
# opens, and reacquire them after -- mpv's --wid embedding parents its video
# window into a Qt widget owned by a different thread, and Win32 can deadlock
# that cross-thread parent/child pair the moment a native modal dialog starts
# disabling/re-enabling windows around it. A WeakSet so a player that's
# already been torn down (editor closed) drops out on its own.
_live_players: "weakref.WeakSet[SharedPlayer]" = weakref.WeakSet()


def release_all_for_dialog() -> list[tuple["SharedPlayer", Path, float, bool, bool]]:
    """Call before opening any native file/folder dialog. Terminates every
    live embedded mpv player (releasing its --wid window) and returns enough
    state for reacquire_all_after_dialog() to reload each one afterward."""
    saved = []
    for player in list(_live_players):
        if player._player is None or player._current_path is None:
            continue
        saved.append((player, player._current_path, player.current_pos_s(),
                      player.is_paused(), player._is_image))
        player.terminate()
    return saved


def reacquire_all_after_dialog(saved: list[tuple["SharedPlayer", Path, float, bool, bool]]):
    """Reloads and seeks back every player released by release_all_for_dialog().
    This runs from a dialog wrapper's `finally` block (see dialogs.py) after
    the OS dialog has already returned a result -- a failure reacquiring any
    one player must never raise out of here, or it silently swallows that
    already-obtained result (e.g. a chosen Save path) before the caller ever
    gets to act on it. Best-effort per player; log and move on."""
    for player, path, pos_s, was_paused, is_image in saved:
        def on_ready(player=player, pos_s=pos_s, was_paused=was_paused, is_image=is_image):
            try:
                # A still image has no meaningful playback position to
                # restore, and mpv may not yet be ready to accept commands
                # this soon after load() for an image (on_ready fires
                # synchronously, before mpv has necessarily finished opening
                # the file) -- seeking here is both pointless and unsafe.
                if not is_image and player.duration_s > 0:
                    player.seek(pos_s / player.duration_s)
                if not was_paused:
                    player.resume_playback()
            except Exception:
                log.exception("Player: failed to restore playback state for %s", path)
        try:
            player.load(path, on_ready=on_ready, is_image=is_image)
        except Exception:
            log.exception("Player: failed to reacquire %s after dialog", path)


class SharedPlayer:
    def __init__(self, anchor_widget: Optional[QWidget] = None, window_width: int = 480, window_x_offset: int = 0):
        self._anchor_widget = anchor_widget  # used only to position a floating fallback window
        self._window_width = window_width
        self._window_x_offset = window_x_offset
        self._player: Optional[mpv.MPV] = None
        self._duration_s = 0.0
        self._tick_cb: TickCB = None
        self._current_path: Optional[Path] = None
        self._is_image = False
        self._embed_widget: Optional[QWidget] = None
        # Bumped on every load()/terminate() so a _wait_for_duration poll
        # loop started by an earlier load() can tell it's been superseded and
        # stop instead of eventually firing its on_ready callback against a
        # newer player instance (see _wait_for_duration).
        self._generation = 0

        self._poll_timer = QTimer()
        self._poll_timer.setInterval(80)
        self._poll_timer.timeout.connect(self._poll)

        _live_players.add(self)

    def set_embed_widget(self, widget: Optional[QWidget]):
        """Points subsequent .load() calls at this widget (mpv embeds via its
        native window handle). Pass None to fall back to a floating window."""
        self._embed_widget = widget

    def _snap_geometry(self) -> str:
        """Positions the mpv window to the right of the main app window,
        matching its height, so playback windows don't scatter."""
        if self._anchor_widget is None:
            return f"{self._window_width}x600+100+100"
        g = self._anchor_widget.window().frameGeometry()
        x = g.x() + g.width() + self._window_x_offset
        y = g.y()
        h = max(200, g.height())
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
        self._is_image = is_image
        self._duration_s = 0.0
        try:
            if self._embed_widget is not None:
                wid = str(int(self._embed_widget.winId()))
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
            QTimer.singleShot(100, lambda: self._wait_for_duration(on_ready, generation, attempt + 1))
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

    def resume_playback(self):
        if self._player is not None:
            self._player.pause = False

    def suspend_polling(self):
        """Stops the periodic mpv IPC poll without touching playback state.
        Call before any native OS dialog (file/folder picker) -- while a modal
        QFileDialog runs its own nested event loop, this app's own QTimer
        polling can otherwise still fire re-entrantly against a live mpv
        instance. Pair with resume_polling() once the dialog call returns."""
        self._stop_polling()

    def resume_polling(self):
        if self._player is not None and not self._poll_timer.isActive():
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
        self._poll_timer.start()
        self._poll()

    def _stop_polling(self):
        self._poll_timer.stop()

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

    def terminate(self):
        self._stop_polling()
        self._generation += 1
        if self._player is not None:
            self._player.terminate()
            self._player = None
