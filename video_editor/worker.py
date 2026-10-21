"""Out-of-process runner for every long job: renders, editor bakes, exports,
batch crop/resize, cache pruning.

    python -m video_editor.worker <job.json>

The GUI starts this through QProcess (qt_ui/worker_client.py), so the GUI
process -- which hosts libmpv -- never runs Python worker threads or blocking
subprocesses itself. Here, threads, subprocesses and a process pool are all
fine: there's no mpv in this process.

Protocol (UTF-8, one record per line):
    stdout:  PID <tab> pid                     (first line)
             PROGRESS <tab> fraction <tab> message
             RESULT <tab> json                 (exactly once, last)
    stderr:  log lines, forwarded by the GUI into its own log

Cancelling = the GUI terminates <pid> (QProcess only knows the venv
launcher shim's pid, not this one -- hence the PID line). This process also
watches the GUI's own pid (passed as "gui_pid" in the job file) and exits if
the GUI dies. Either way the Windows job object below takes every ffmpeg /
pool child down with it. (stdin is deliberately not used as the cancel
channel: a thread blocked reading it breaks multiprocessing's child spawn on
Windows -- DuplicateHandle fails with access denied.)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import active_processes, ffmpeg_ops, render_assets
from .logging_config import configure_logging, get_logger
from .project import clip_from_dict, overlay_from_dict, project_from_dict, sound_from_dict

log = get_logger()

EXIT_OK, EXIT_FAILED, EXIT_CANCELLED = 0, 1, 2
_out_lock = threading.Lock()


def _emit(line: str) -> None:
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _progress(frac: float, msg: str) -> None:
    _emit(f"PROGRESS\t{max(0.0, min(1.0, frac)):.4f}\t{msg.replace(chr(9), ' ').replace(chr(10), ' ')}")


def _kill_children_with_me() -> None:
    """Puts this process in a Windows job object with KILL_ON_JOB_CLOSE.
    Every child (ffmpeg, process-pool workers) inherits the job, so they all
    die the moment this process exits for any reason -- including being
    killed outright -- instead of lingering as orphans."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                         "OtherOperationCount", "ReadTransferCount",
                                                         "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        # The handle is intentionally never closed: it closes when we exit.
    except Exception:
        log.warning("Worker: could not set up child-process job object; children may outlive a crash",
                    exc_info=True)


def _watch_gui(gui_pid: int) -> None:
    """Exits (taking all children with it) as soon as the GUI process ends --
    e.g. it crashed or was killed mid-render -- so no orphaned ffmpeg keeps
    burning CPU with nobody left to collect its output."""
    if sys.platform != "win32" or not gui_pid:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    handle = kernel32.OpenProcess(0x00100000, False, gui_pid)  # SYNCHRONIZE
    if not handle:
        return
    kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
    ffmpeg_ops.CANCELLED.set()
    active_processes.kill_all()
    os._exit(EXIT_CANCELLED)


# ------------------------------------------------------------------ jobs --

def _render_timeline(p: dict, pool) -> dict:
    project = project_from_dict(p["project"])
    ffmpeg_ops.seed_probe_cache(p.get("probe_hints", []))
    ok = ffmpeg_ops.render_timeline(
        project.clips, project.canvas_w, project.canvas_h, Path(p["output"]),
        proxy=p["proxy"], proxy_height=p.get("proxy_height", 270), progress_cb=_progress,
        text_overlays=project.text_overlays, sound_clips=project.sound_clips,
        intro_type=project.intro_transition_type, intro_duration=project.intro_transition_duration,
        intro_direction=project.intro_transition_direction,
        outro_type=project.outro_transition_type, outro_duration=project.outro_transition_duration,
        outro_direction=project.outro_transition_direction,
        pool=pool)
    return {"ok": ok}


def _bake_image(p: dict, pool) -> dict:
    ok = ffmpeg_ops.render_image_clip_preview(
        clip_from_dict(p["clip"]), p["canvas_w"], p["canvas_h"], Path(p["output"]),
        preview_height=p.get("preview_height"), progress_cb=_progress, pool=pool)
    return {"ok": ok}


def _bake_text(p: dict, pool) -> dict:
    ok = ffmpeg_ops.render_text_overlay_preview(
        Path(p["source"]), overlay_from_dict(p["overlay"]), p["canvas_w"], p["canvas_h"], Path(p["output"]),
        preview_height=p.get("preview_height"), progress_cb=_progress, pool=pool)
    return {"ok": ok}


def _bake_sound(p: dict, pool) -> dict:
    ok = ffmpeg_ops.render_sound_clip_preview(
        Path(p["source"]), sound_from_dict(p["sound"]), Path(p["output"]),
        preview_height=p.get("preview_height"), progress_cb=_progress)
    return {"ok": ok}


def _export_clip(p: dict, pool) -> dict:
    return {"ok": ffmpeg_ops.export_clip(Path(p["path"]), p["in_s"], p["out_s"], Path(p["output"]), _progress)}


def _export_frame(p: dict, pool) -> dict:
    return {"ok": ffmpeg_ops.export_frame(Path(p["path"]), p["pos_s"], Path(p["output"]))}


def _crop_resize(p: dict, pool) -> dict:
    from PIL import Image

    out_dir = Path(p["output_dir"])
    crop = tuple(p["crop_fractions"])
    tw, th = p.get("target_w"), p.get("target_h")
    videos = [Path(v) for v in p["videos"]]
    images = [Path(i) for i in p["images"]]
    total = max(1, len(videos) + len(images))
    failed: list[str] = []
    if videos:
        _done, vfailed = ffmpeg_ops.run_crop_resize(
            videos, out_dir, crop, (tw, th),
            lambda f, m: _progress(f * len(videos) / total, m))
        failed += vfailed
    top_f, bottom_f, left_f, right_f = crop
    for k, path in enumerate(images):
        _progress((len(videos) + k) / total, f"Cropping {path.name}")
        out = out_dir / path.name
        if out.resolve() == path.resolve():
            log.error("Crop/Resize: refusing to overwrite source %s (output folder = input folder)", path)
            failed.append(path.name)
            continue
        try:
            with Image.open(path) as img:
                w, h = img.size
                top, bottom = round(top_f * h), round(bottom_f * h)
                left, right = round(left_f * w), round(right_f * w)
                cw, ch = w - left - right, h - top - bottom
                img = img.crop((left, top, left + cw, top + ch))
                img = img.resize((tw or cw, th or ch), Image.LANCZOS)
                img.save(out)
            log.info("Cropped image %s -> %dx%d", path.name, tw or cw, th or ch)
        except Exception:
            log.exception("Crop/Resize: failed to process image %s", path)
            failed.append(path.name)
    return {"ok": not failed, "processed": len(videos) + len(images) - len(failed), "failed": failed}


def _prune_cache(p: dict, pool) -> dict:
    render_assets.prune_cache(p.get("max_bytes", render_assets.CACHE_MAX_BYTES))
    return {"ok": True}


JOBS = {
    "render_timeline": _render_timeline,
    "bake_image": _bake_image,
    "bake_text": _bake_text,
    "bake_sound": _bake_sound,
    "export_clip": _export_clip,
    "export_frame": _export_frame,
    "crop_resize": _crop_resize,
    "prune_cache": _prune_cache,
}

# Jobs that bake PIL frames and benefit from the process pool.
_POOL_JOBS = {"render_timeline", "bake_image", "bake_text"}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    configure_logging(logging.INFO, stream=sys.stderr)
    _emit(f"PID\t{os.getpid()}")
    _kill_children_with_me()

    job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    threading.Thread(target=_watch_gui, args=(job.get("gui_pid", 0),), daemon=True).start()
    kind = job["kind"]
    pool = (ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1))
            if kind in _POOL_JOBS else None)
    try:
        result = JOBS[kind](job["params"], pool)
    except ffmpeg_ops.Cancelled:
        result = {"ok": False, "cancelled": True}
    except Exception as e:  # noqa: BLE001 -- reported to the GUI, not swallowed
        log.error("Worker: job %s crashed:\n%s", kind, traceback.format_exc())
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    _emit("RESULT\t" + json.dumps(result))
    if result.get("cancelled"):
        return EXIT_CANCELLED
    return EXIT_OK if result.get("ok") else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
