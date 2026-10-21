"""Single source of truth for where each clip sits in the *rendered* output.

Transitions overlap the clips they join (xfade/acrossfade consume
`duration` seconds from both sides), so the composed video is shorter than
the plain sum of clip durations. The timeline widget, snapping, and the
renderer all have to agree on that, or text/sound placed against a clip on
the timeline lands somewhere else in the final video. Everything that needs a
clip's output position goes through clip_layout().
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

FPS = 30
MIN_TRANSITION_S = 0.05


@dataclass(frozen=True)
class Join:
    """How clip i (i >= 1) is joined onto everything before it."""
    kind: str          # "cut" (concat, no overlap) or "xfade"
    duration: float    # overlap in seconds; 0.0 for a cut
    transition: str    # the clip's transition_type
    direction: str


def effective_join(prev_duration: float, clip) -> Join:
    """A transition can't be longer than either clip it overlaps (xfade needs
    `duration` seconds from both sides). Clamp it, and fall back to a hard
    cut when there isn't room for even a minimal transition."""
    t_type = clip.transition_type
    if t_type == "cut":
        return Join("cut", 0.0, t_type, clip.transition_direction)
    room = min(prev_duration, clip.duration) - 1.0 / FPS
    if room < MIN_TRANSITION_S:
        return Join("cut", 0.0, "cut", clip.transition_direction)
    d = max(MIN_TRANSITION_S, min(clip.transition_duration, room))
    return Join("xfade", round(d, 4), t_type, clip.transition_direction)


def clip_layout(clips: Sequence) -> tuple[list[float], list[Join | None], float]:
    """Returns (starts, joins, total_s): each clip's start time in the
    rendered output, the join used to attach it (None for clip 0), and the
    composed length. Intro/outro transitions overlap fully with black and
    never change these numbers (see edge_transition_duration)."""
    starts: list[float] = []
    joins: list[Join | None] = []
    end = 0.0
    prev_dur = 0.0
    for i, c in enumerate(clips):
        if i == 0:
            starts.append(0.0)
            joins.append(None)
        else:
            j = effective_join(prev_dur, c)
            starts.append(max(0.0, end - j.duration))
            joins.append(j)
        end = starts[-1] + c.duration
        prev_dur = c.duration
    return starts, joins, end


def edge_transition_duration(t_type: str, requested: float, available: float) -> float:
    """Intro (black -> first clip) / outro (timeline -> black) overlap length,
    clamped to what the clip/timeline can supply. 0.0 means no transition."""
    if t_type == "cut":
        return 0.0
    d = min(max(MIN_TRANSITION_S, requested), available - 1.0 / FPS)
    return round(d, 4) if d >= MIN_TRANSITION_S else 0.0
