"""Scale-animation curve presets for text overlays. eval_curve(t_norm) returns
a scale multiplier for t_norm in [0, 1] (position within the overlay's active
duration). Kept as pure Python so all animation math is baked into rendered
pixels before ffmpeg ever sees them (see text_render.render_overlay_sequence).
"""
from __future__ import annotations

import math

# preset key -> (label, {param_name: default_value})
CURVE_PRESETS: dict[str, tuple[str, dict[str, float]]] = {
    "none": ("None (static size)", {}),
    "linear": ("Linear", {"in_pct": 20.0}),
    "ease_in": ("Ease In", {"in_pct": 20.0}),
    "ease_in_out": ("Ease In-Out", {"in_pct": 20.0, "out_pct": 20.0}),
    "pop": ("Pop (overshoot)", {"in_pct": 15.0, "overshoot": 1.3}),
    "bounce": ("Bounce", {"in_pct": 30.0, "bounces": 3.0}),
    "elastic": ("Elastic", {"in_pct": 35.0, "wobble": 3.0}),
    "wiggle": ("Shake/Wiggle (continuous)", {"amplitude_pct": 10.0, "frequency": 3.0}),
}


def _smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def eval_curve(preset: str, params: dict[str, float], t_norm: float) -> float:
    t_norm = max(0.0, min(1.0, t_norm))
    label_params = CURVE_PRESETS.get(preset, CURVE_PRESETS["none"])[1]
    p = {**label_params, **(params or {})}

    if preset == "none":
        return 1.0

    if preset == "linear":
        in_frac = max(0.001, p.get("in_pct", 20.0) / 100.0)
        if t_norm >= in_frac:
            return 1.0
        return t_norm / in_frac

    if preset == "ease_in":
        in_frac = max(0.001, p.get("in_pct", 20.0) / 100.0)
        if t_norm >= in_frac:
            return 1.0
        return _smoothstep(t_norm / in_frac)

    if preset == "ease_in_out":
        in_frac = max(0.001, p.get("in_pct", 20.0) / 100.0)
        out_frac = max(0.001, p.get("out_pct", 20.0) / 100.0)
        if t_norm < in_frac:
            return _smoothstep(t_norm / in_frac)
        if t_norm > 1.0 - out_frac:
            return _smoothstep((1.0 - t_norm) / out_frac)
        return 1.0

    if preset == "pop":
        in_frac = max(0.001, p.get("in_pct", 15.0) / 100.0)
        overshoot = p.get("overshoot", 1.3)
        if t_norm >= in_frac:
            return 1.0
        u = t_norm / in_frac
        # Ramp 0 -> overshoot -> settle at 1.0, via two smoothstep halves.
        if u < 0.7:
            return _smoothstep(u / 0.7) * overshoot
        settle = _smoothstep((u - 0.7) / 0.3)
        return overshoot + (1.0 - overshoot) * settle

    if preset == "bounce":
        in_frac = max(0.001, p.get("in_pct", 30.0) / 100.0)
        bounces = max(1.0, p.get("bounces", 3.0))
        if t_norm >= in_frac:
            return 1.0
        u = t_norm / in_frac
        # Damped cosine growing from 0, oscillating around 1, settling at 1.
        return 1.0 - math.exp(-3.0 * u) * math.cos(bounces * math.pi * u)

    if preset == "elastic":
        in_frac = max(0.001, p.get("in_pct", 35.0) / 100.0)
        wobble = max(1.0, p.get("wobble", 3.0))
        if t_norm >= in_frac:
            return 1.0
        if t_norm <= 0.0:
            return 0.0
        u = t_norm / in_frac
        c = (2 * math.pi) / wobble
        return (2 ** (-10 * u)) * math.sin((u * 10 - 0.75) * c) + 1.0

    if preset == "wiggle":
        amplitude = max(0.0, p.get("amplitude_pct", 10.0) / 100.0)
        frequency = max(0.1, p.get("frequency", 3.0))
        return 1.0 + amplitude * math.sin(2 * math.pi * frequency * t_norm)

    return 1.0


def fade_opacity(t_s: float, duration_s: float, fade_in_s: float, fade_out_s: float) -> float:
    """Opacity multiplier (0..1) at absolute time t_s within the overlay's
    active duration, independent of the scale curve. Triangular if the two
    fades overlap (i.e. fade_in_s + fade_out_s > duration_s)."""
    ramp_in = 1.0 if fade_in_s <= 0 else max(0.0, min(1.0, t_s / fade_in_s))
    ramp_out = 1.0 if fade_out_s <= 0 else max(0.0, min(1.0, (duration_s - t_s) / fade_out_s))
    return max(0.0, min(ramp_in, ramp_out))
