"""Project-wide undo/redo, backed by Qt's own QUndoStack.

TimelineProject is a small tree of plain dataclasses (no video bytes, just
paths/floats/strings), so rather than a bespoke QUndoCommand per edit
operation, this uses one generic snapshot command.

Commit-only model: the stack remembers the last committed state itself.
Every mutation path just mutates the live project and then calls
commit(label, project); the command is (last committed -> now). There is no
begin()/commit() pairing to get out of sync -- the old pairing kept one
pending snapshot that the first commit consumed, so a second edit in the same
editor session (or an editor save after a timeline drag) silently never
reached the stack.

Snapshots handed to `apply` on undo/redo are always fresh copies: the live
project gets mutated in place afterwards, and that must never write through
into a snapshot still sitting on the stack.
"""
from __future__ import annotations

import copy
from typing import Callable

from PySide6.QtGui import QUndoCommand, QUndoStack

from ..project import TimelineProject


class _SnapshotCommand(QUndoCommand):
    def __init__(self, label: str, before: TimelineProject, after: TimelineProject,
                 owner: "ProjectUndoStack"):
        super().__init__(label)
        self._before = before
        self._after = after
        self._owner = owner
        self._first_redo = True

    def undo(self):
        self._owner._restore(self._before)

    def redo(self):
        # QUndoStack.push() calls redo() immediately; the live project
        # already IS the after-state then, so don't swap it out from under
        # whatever editor just made the change.
        if self._first_redo:
            self._first_redo = False
            return
        self._owner._restore(self._after)


class ProjectUndoStack:
    def __init__(self, apply: Callable[[TimelineProject], None]):
        """`apply` swaps the workspace's active project reference and
        refreshes every panel bound to it -- provided by the owning window."""
        self._stack = QUndoStack()
        self._apply = apply
        self._baseline: TimelineProject = TimelineProject()

    @property
    def qt_stack(self) -> QUndoStack:
        return self._stack

    def reset(self, project: TimelineProject):
        """New/loaded project: clears history and takes `project` as the
        baseline the next commit is compared against."""
        self._stack.clear()
        self._baseline = copy.deepcopy(project)

    def commit(self, label: str, project: TimelineProject):
        """Call right after mutating `project`. No-op if nothing changed."""
        after = copy.deepcopy(project)
        if after == self._baseline:
            return
        before = self._baseline
        self._baseline = after
        self._stack.push(_SnapshotCommand(label, before, copy.deepcopy(after), self))

    def _restore(self, snapshot: TimelineProject):
        self._baseline = copy.deepcopy(snapshot)
        self._apply(copy.deepcopy(snapshot))

    def undo(self):
        self._stack.undo()

    def redo(self):
        self._stack.redo()

    def can_undo(self) -> bool:
        return self._stack.canUndo()

    def can_redo(self) -> bool:
        return self._stack.canRedo()
