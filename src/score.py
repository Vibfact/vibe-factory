"""src/score.py — rank-based components, event skew, decaying pressure, blend.
Positive score = bullish vibe for the metal. Weights are priors; calibrate later."""
from __future__ import annotations

import json
import math
import os

REGIME_WEIGHTS = {
    "real_rate": -0.22,      # rising real yields -> bearish metals
    "usd": -0.16,
    "hike_pressure": -0.22,  # Kalshi/Polymarket hawkish lean
    "cot_crowding": -0.10,   # placeholder until COT module lands
    "momentum": +0.16,
    "gsr": -0.08,            # rising gold/silver ratio -> risk-off, bearish silver
    "media_tone": +0.06,
}
EVENT_SKEW_SCALE = 2.0
SILVER_BETA = 1.3
LABELS = ((-60, "Strongly bearish"), (-20, "Bearish"), (20, "Neutral"),
          (60, "Bullish"), (1e9, "Strongly bullish"))


def rank(hist: list[float] | None, x: float | None) -> float | None:
    """Rolling empirical percentile mapped to [-1, 1]. Robust to fat tails."""
    if x is None or not hist:
        return None
    h = [v for v in hist if v is not None]
    if len(h) < 30:
        return None
    pct = sum(1 for v in h if v <= x) / len(h)
    return round(2 * pct - 1, 4)


def residualise(y: list[float], x: list[float]) -> float | None:
    """Strip the part of a reflexive signal (tone, chatter) explained by price."""
    n = min(len(y), len(x))
    if n < 30:
        return None
    y, x = y[-n:], x[-n:]
    mx, my = sum(x) / n, sum(y) / n
    var = sum((v - mx) ** 2 for v in x)
    if var <= 0:
        return None
    beta = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / var
    return round(y[-1] - (my + beta * (x[-1] - mx)), 4)


def decay(age_min: float, half_life: float = 240.0) -> float:
    return 0.5 ** (age_min / max(half_life, 1e-9))


def pressure(events: list[dict]) -> float:
    return round(sum(e["impact"] * decay(e["age_min"], e.get("hl", 240)) for e in events), 4)


def event_skew(days_out: int | None, hike_pressure: float, magnitude: float = 1.0) -> float:
    """Pre-event de-risking: ramps up 0-5 days out, direction from the hawkish lean."""
    if days_out is None or days_out > 5 or days_out < 0:
        return 0.0
    proximity = (6 - days_out) / 6
    sign = -1.0 if hike_pressure >= 0 else 1.0
    return round(sign * abs(hike_pressure) * proximity * magnitude, 4)


def load_weights(path: str = "state/weights.json") -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                w = json.load(f)
            return {**REGIME_WEIGHTS, **w.get("regime", {})}
        except (OSError, ValueError):
            pass
    return dict(REGIME_WEIGHTS)


def blend(components: dict[str, float | None], pressure_score: float,
          skew: float, metal: str = "XAU", weights: dict | None = None) -> dict:
    w = weights or load_weights()
    used, missing, raw, wsum = {}, [], 0.0, 0.0
    for k, weight in w.items():
        v = components.get(k)
        if v is None:
            missing.append(k)
            continue
        used[k] = round(weight * v, 4)
        raw += weight * v
        wsum += abs(weight)

    if wsum > 0:                                   # renormalise around dead sources
        raw *= sum(abs(x) for x in w.values()) / wsum

    regime = 100 * math.tanh(raw / 1.5)
    press = 100 * math.tanh((pressure_score + EVENT_SKEW_SCALE * skew) / 1.5)
    total = 0.55 * regime + 0.45 * press
    if metal == "XAG":
        total = 100 * math.tanh(SILVER_BETA * total / 100)

    label = next(name for cut, name in LABELS if total < cut)
    confidence = round(1 - len(missing) / max(len(w), 1), 2)
    return {"metal": metal, "vibe": round(total, 1), "label": label,
            "regime": round(regime, 1), "pressure": round(press, 1),
            "contributions": used, "missing": missing, "confidence": confidence}
