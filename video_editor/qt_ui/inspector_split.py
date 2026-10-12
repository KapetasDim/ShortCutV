"""A draggable vertical splitter for the per-item Inspector editors: preview
on top, scrollable controls below, one movable divider between them (NOT
separate tab pages -- both stay visible, you just trade space between them).

The divider position is remembered per editor kind (text / image / sound) via
QSettings, so each editor reopens at the height you last left it.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QSplitter, QWidget

_ORG, _APP = "ShortCutV", "ShortCutV"


def make_preview_splitter(preview: QWidget, controls: QWidget, settings_key: str,
                          default_preview_h: int = 340) -> QSplitter:
    """Vertical splitter with `preview` on top and `controls` below. `controls`
    takes any extra space (so growing the dock grows the options area, not the
    preview); the preview keeps the height last saved under `settings_key`."""
    splitter = QSplitter(Qt.Orientation.Vertical)
    splitter.setChildrenCollapsible(False)
    splitter.addWidget(preview)
    splitter.addWidget(controls)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)

    settings = QSettings(_ORG, _APP)
    saved = settings.value(settings_key, 0, type=int)
    top = saved if saved and saved > 60 else default_preview_h
    # A large second value just means "give the rest to the controls"; the
    # splitter clamps both to the actual available height when laid out.
    splitter.setSizes([top, 100000])

    def _save(_pos, _index):
        sizes = splitter.sizes()
        if sizes and sizes[0] > 0:
            settings.setValue(settings_key, sizes[0])

    splitter.splitterMoved.connect(_save)
    return splitter
