"""Project-wide undo/redo, backed by Qt's own QUndoStack.

TimelineProject is a small tree of plain dataclasses (no video bytes, just
paths/floats/strings), so rather than writing a bespoke QUndoCommand for each
of the dozen or so edit operations (reorder clip, trim, crop, add/remove
overlay, add/remove sound, transition change, ...), this uses one generic
snapshot command: callers grab a deep copy of the project before mutating it,
mutate it, then push (before, after) here. Undo/redo just swaps the whole
project object back in and asks the workspace to refresh -- correct by
construction, and cheap enough given the data sizes involved.
"""
from __future__ import annotations

import copy
from typing import Callable

from PySide6.QtGui import QUndoCommand, QUndoStack

from ..project import TimelineProject


class _SnapshotCommand(QUndoCommand):
    def __init__(self, label: str, before: TimelineProject, after: TimelineProject,
                 apply: Callable[[TimelineProject], None]):
        super().__init__(label)
        self._before = before
        self._after = after
        self._apply = apply

    def undo(self):
        self._apply(self._before)

    def redo(self):
        self._apply(self._after)


class ProjectUndoStack:
    def __init__(self, apply: Callable[[TimelineProject], None]):
        """`apply` swaps the workspace's active project reference and
        refreshes every panel bound to it -- provided by the owning window."""
        self._stack = QUndoStack()
        self._apply = apply
        self._before_snapshot: TimelineProject | None = None

    @property
    def qt_stack(self) -> QUndoStack:
        return self._stack

    def begin(self, project: TimelineProject):
        """Call before mutating `project` in place."""
        self._before_snapshot = copy.deepcopy(project)

    def commit(self, label: str, project: TimelineProject):
        """Call right after mutating `project` in place; pushes the edit onto
        the undo stack. No-op if begin() was never called (or the project
        didn't actually change, judged by equality of the JSON-able fields)."""
        if self._before_snapshot is None:
            return
        before = self._before_snapshot
        self._before_snapshot = None
        after = copy.deepcopy(project)
        if before == after:
            return
        self._stack.push(_SnapshotCommand(label, before, after, self._apply))

    def undo(self):
        self._stack.undo()

    def redo(self):
        self._stack.redo()

    def can_undo(self) -> bool:
        return self._stack.canUndo()

    def can_redo(self) -> bool:
        return self._stack.canRedo()
