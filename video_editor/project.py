from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .logging_config import get_logger

log = get_logger()

DEFAULT_TRANSITION_DURATION = 0.25


@dataclass
class TimelineClip:
    source: str          # absolute path, string for JSON round-trip
    in_s: float
    out_s: float
    # Describes the transition used to join this clip after the previous one.
    # Ignored for the first clip on the timeline.
    transition_type: str = "cut"          # crossfade | fade_black | wipe | slide | cut
    transition_duration: float = DEFAULT_TRANSITION_DURATION
    transition_direction: str = "left"    # left | right | up | down -- only used by wipe/slide

    # Images are timeline clips too: in_s=0/out_s=duration is reused as-is (no
    # trimming, just a length), rendered as a zoomable still instead of a
    # decoded video stream -- see image_render.py.
    is_image: bool = False
    image_zoom_enabled: bool = False
    image_zoom_factor: float = 1.2        # >= 1.0; end-of-curve zoom level
    image_zoom_curve: str = "none"        # see curves.CURVE_PRESETS
    image_zoom_curve_params: dict = field(default_factory=dict)

    # 0.0-2.0 (0-200%) gain applied to this clip's own embedded audio track.
    # Defaults muted -- clips in this app are typically screen recordings
    # whose native audio is scratch/system sound, not something meant to ship;
    # music/SFX are meant to be added deliberately via the sound-clip library
    # instead of inheriting whatever a source clip happened to capture.
    audio_volume: float = 0.0

    # Crop applied to this clip alone, in source-pixel space (0 = uncropped
    # on that edge). Set via the crop box embedded in the clip's edit tab
    # (clip_trim_editor.py) and applied at render time (ffmpeg_ops.render_timeline).
    crop_top: int = 0
    crop_bottom: int = 0
    crop_left: int = 0
    crop_right: int = 0

    @property
    def path(self) -> Path:
        return Path(self.source)

    @property
    def duration(self) -> float:
        return max(0.0, self.out_s - self.in_s)


@dataclass
class TextOverlay:
    text: str
    start_s: float
    duration_s: float
    x: float
    y: float
    rotation_deg: float = 0.0
    font_family: str = "Arial"
    font_size: int = 64
    color: str = "#ffffff"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    bg_color: str = "#000000"
    bg_opacity: float = 0.0          # 0..1, 0 = no background box
    outline_enabled: bool = False
    outline_color: str = "#000000"
    outline_width: int = 2           # px, at font_size's native scale
    curve_preset: str = "none"       # see curves.CURVE_PRESETS
    curve_params: dict = field(default_factory=dict)
    fade_in_s: float = 0.25          # opacity fade, independent of the scale curve
    fade_out_s: float = 0.25

    # Placement bounding box (project-canvas pixel space, same space as x/y),
    # centered on (x, y) -- set via the draggable/resizable box in the text
    # overlay editor. wrap_enabled controls whether text_render word-wraps to
    # box_w; box_h is otherwise just a placement/sizing reference, not a hard
    # clip. Rotation has no box handle -- it's edited via rotation_deg only.
    box_w: float = 400.0
    box_h: float = 150.0
    wrap_enabled: bool = True

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass
class SoundClip:
    """A library sound/music file placed at a moment on the composed
    timeline (independent of any TimelineClip) -- see TimelineClip.audio_volume
    for a clip's own embedded audio, which is edited from the clip's own edit
    tab instead of appearing here."""
    source: str                    # absolute path, string for JSON round-trip
    start_s: float                 # position in the composed timeline
    clip_start_s: float = 0.0      # offset into the source file to begin reading from
    clip_duration_s: float = 0.0   # length of source used per repeat (set from the source's own duration at insertion)
    volume: float = 1.0            # 0.0-2.0 (0-200%)
    speed: float = 1.0             # linked pitch+speed factor (asetrate) -- 1.0 = unchanged
    repeat_count: int = 1          # >=1 total plays; ignored if repeat_infinite
    repeat_infinite: bool = False  # loop until the composed timeline ends
    fade_in_s: float = 0.0
    fade_out_s: float = 0.0

    def played_duration_s(self, timeline_total_s: float) -> float:
        """How long this actually plays for, clamped so it can never outlast
        the composed timeline (no audio-only tail after the video ends)."""
        remaining = max(0.0, timeline_total_s - self.start_s)
        if self.repeat_infinite:
            return remaining
        one_play = max(0.0, self.clip_duration_s) / max(0.01, self.speed)
        return min(remaining, one_play * max(1, self.repeat_count))

    def end_s(self, timeline_total_s: float) -> float:
        return self.start_s + self.played_duration_s(timeline_total_s)


@dataclass
class TimelineProject:
    clips: list[TimelineClip] = field(default_factory=list)
    canvas_w: int = 1080
    canvas_h: int = 1920
    text_overlays: list[TextOverlay] = field(default_factory=list)
    sound_clips: list[SoundClip] = field(default_factory=list)
    # The library's clip/sound source folders, saved per-project so reopening
    # a project restores exactly what it was scanning -- separate from
    # AppState.clip_input_dir/clip_output_dir, which just seed a *new*
    # project's defaults (see TimelineTab.apply_chained_defaults).
    lib_input_dir: str | None = None
    lib_output_dir: str | None = None
    # Fade-in-from-black (or wipe/slide-in) before the first clip; "cut" = start instantly.
    intro_transition_type: str = "cut"
    intro_transition_duration: float = DEFAULT_TRANSITION_DURATION
    intro_transition_direction: str = "left"
    # Fade-out-to-black (or wipe/slide-out) after the last clip; "cut" = end instantly.
    outro_transition_type: str = "cut"
    outro_transition_duration: float = DEFAULT_TRANSITION_DURATION
    outro_transition_direction: str = "left"


def save_project(project: TimelineProject, path: Path) -> None:
    data = {
        "canvas_w": project.canvas_w,
        "canvas_h": project.canvas_h,
        "clips": [asdict(c) for c in project.clips],
        "text_overlays": [asdict(t) for t in project.text_overlays],
        "sound_clips": [asdict(s) for s in project.sound_clips],
        "lib_input_dir": project.lib_input_dir,
        "lib_output_dir": project.lib_output_dir,
        "intro_transition_type": project.intro_transition_type,
        "intro_transition_duration": project.intro_transition_duration,
        "intro_transition_direction": project.intro_transition_direction,
        "outro_transition_type": project.outro_transition_type,
        "outro_transition_duration": project.outro_transition_duration,
        "outro_transition_direction": project.outro_transition_direction,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Saved project: %s (%d clips, %d text overlays, %d sound clips)",
              path.name, len(project.clips), len(project.text_overlays), len(project.sound_clips))


def load_project(path: Path) -> TimelineProject:
    data = json.loads(path.read_text(encoding="utf-8"))
    clips = [TimelineClip(**c) for c in data.get("clips", [])]
    text_overlays = [TextOverlay(**t) for t in data.get("text_overlays", [])]
    sound_clips = [SoundClip(**s) for s in data.get("sound_clips", [])]
    project = TimelineProject(
        clips=clips,
        canvas_w=data.get("canvas_w", 1080),
        canvas_h=data.get("canvas_h", 1920),
        text_overlays=text_overlays,
        sound_clips=sound_clips,
        lib_input_dir=data.get("lib_input_dir"),
        lib_output_dir=data.get("lib_output_dir"),
        intro_transition_type=data.get("intro_transition_type", "cut"),
        intro_transition_duration=data.get("intro_transition_duration", DEFAULT_TRANSITION_DURATION),
        intro_transition_direction=data.get("intro_transition_direction", "left"),
        outro_transition_type=data.get("outro_transition_type", "cut"),
        outro_transition_duration=data.get("outro_transition_duration", DEFAULT_TRANSITION_DURATION),
        outro_transition_direction=data.get("outro_transition_direction", "left"),
    )
    log.info("Loaded project: %s (%d clips, %d text overlays, %d sound clips)",
              path.name, len(project.clips), len(project.text_overlays), len(project.sound_clips))
    return project
