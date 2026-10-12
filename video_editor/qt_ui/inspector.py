"""Inspector dock panel: hosts the Source viewer (previewing whatever's
selected in the Bin -- trimming/exporting a source clip counts as
"inspecting" it) as its default content, and swaps in the appropriate
per-item editor (ClipTrimEditor, ImageClipEditor, TextOverlayEditor,
SoundClipEditor) whenever something is selected on the Timeline instead.
A QStackedWidget keeps the Source viewer's mpv-embedded widget alive and
never torn down, so switching back to it doesn't need to reload anything."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget


class InspectorPanel(QWidget):
    def __init__(self, source_viewer: QWidget, parent=None):
        super().__init__(parent)
        self.source_viewer = source_viewer
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        layout.addWidget(self._stack)
        self._stack.addWidget(source_viewer)
        self._stack.setCurrentWidget(source_viewer)
        self._current_editor: Optional[QWidget] = None
        self._current_teardown: Optional[Callable[[], None]] = None

    def show_editor(self, widget: QWidget, teardown: Optional[Callable[[], None]] = None):
        self.clear_editor()
        self._stack.addWidget(widget)
        self._stack.setCurrentWidget(widget)
        self._current_editor = widget
        self._current_teardown = teardown

    def clear_editor(self):
        """Tears down whatever per-item editor is currently shown (if any)
        and falls back to the Source viewer."""
        if self._current_editor is not None:
            if self._current_teardown is not None:
                try:
                    self._current_teardown()
                except Exception:
                    pass
            self._stack.removeWidget(self._current_editor)
            self._current_editor.deleteLater()
            self._current_editor = None
            self._current_teardown = None
        self._stack.setCurrentWidget(self.source_viewer)

    # Kept as an alias -- app.py's undo/new/load-project paths call this
    # generically without needing to know an editor happens to be open.
    clear = clear_editor
