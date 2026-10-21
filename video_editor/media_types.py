"""File-extension groups shared by the Bin and the batch crop/resize tool."""
from __future__ import annotations

from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}

# Containers that can hold an H.264/HEVC re-encode plus a copied audio stream
# as-is. Anything else (webm/avi) is written out as .mp4 with AAC audio.
REMUX_SAFE_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv"}


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "sound"
    return None
