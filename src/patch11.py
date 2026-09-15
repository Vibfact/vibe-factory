"""src/patch11.py — trend sentence (v2)

One compact line describing how the current reading compares to the previous
message, the last 24 hours, and the last few days. Collapses to a single
sentence when metals agree, which they usually do.

    XAU and XAG a touch more bearish than the last update, and a bit more
    bearish than the 3-day mean (-33)

    XAU a touch more bearish than the last update; XAG steady

Design notes
  - No raw before/after numbers: fmt.py already prints the per-run delta arrow,
    so repeating "-40 -> -44" is noise. Magnitudes appear as adverbs.
  - Direction words respect the current stance: when the score is negative a
    rise is "less bearish", not "more bullish".
  - Reads state/history.csv, discovering the score/timestamp columns by name
    (xau_vibe, ts_utc, and common alternatives) so schema drift degrades to
    silence rather than an exception.

Env
  TREND_BASELINE_DAYS   multi-day window, default 3
  TREND_FLOOR           ISO timestamp; rows before it are excluded from the
                        multi-day mean only (use to ignore pre-rework scores)
  STATE_HISTORY         path override, default state/history.csv

Public API
    summary({"XAU": -44, "XAG": -47})   -> str   (what fmt.py should print)
    trend_line("XAU", -44)              -> str   (single metal, verbose)
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import pathlib

HISTORY = pathlib.Path(os.environ.get("STATE_HISTORY", "state/history.csv"))
BASELINE_DAYS = float(os.environ.get("TREND_BASELINE_DAYS", "3"))
MIN_POINTS = 3

TS_KEYS = ("ts_utc", "ts", "time", "timestamp", "datetime", "date", "asof")

# (exclusive upper bound on |delta|, adverb)
BANDS = ((1.5, ""),                    # empty adverb -> "steady"
         (5.0, "a touch"),
         (12.0, "a bit"),
         (25.0, "notably"),
         (float("inf"), "sharply"))


def _score_keys(metal: str) -> tuple[str, ...]:
    m = metal.lower()
    return (f"{m}_vibe", f"{m}_score", f"score_{m}", f"{m}vibe", f"{m}score",
            f"{m}_total", f"{m}_bias", f"{m}_signal", m)


def _parse_ts(raw: str) -> dt.datetime | None:
    raw = (raw or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = (dt.datetime.fromisoformat(raw) if fmt is None
                 else dt.datetime.strptime(raw, fmt))
        except (ValueError, TypeError):
            continue
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return None


def _rows() -> list[dict]:
    if not HISTORY.exists():
        return []
    try:
        with HISTORY.open(newline="", encoding="utf-8", errors="replace") as fh:
            return [r for r in csv.DictReader(fh) if r]
    except OSError:
        return []


def _pick(row: dict, keys: tuple[str, ...]) -> str | None:
    low = {str(k).strip().lower(): v for k, v in row.items() if k}
    for k in keys:
        v = low.get(k)
        if v is not None and str(v).strip() not in ("", "None", "nan", "NaN"):
            return str(v)
    return None


def series(metal: str) -> list[tuple[dt.datetime | None, float]]:
    out: list[tuple[dt.datetime | None, float]] = []
    for r in _rows():
        raw = _pick(r, _score_keys(metal))
        if raw is None:
            continue
        try:
            out.append((_parse_ts(_pick(r, TS_KEYS) or ""),
                        float(str(raw).replace("+", "").strip())))
        except ValueError:
            continue
    return out


def _adverb(delta: float) -> str:
    for limit, word in BANDS:
        if abs(delta) < limit:
            return word
    return "sharply"


def _direction(delta: float, current: float) -> str:
    bearish_now = current < 0
    if delta > 0:
        return "less bearish" if bearish_now else "more bullish"
    return "more bearish" if bearish_now else "less bullish"


def _clause(delta: float, current: float, anchor: str) -> str:
    """'a touch more bearish than the last update', or '' when flat."""
    adverb = _adverb(delta)
    if not adverb:
        return ""
    return f"{adverb} {_direction(delta, current)} than {anchor}"


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _windows(metal: str, now: dt.datetime) -> dict:
    """prev value, 24h mean, multi-day mean."""
    hist = series(metal)
    if not hist:
        return {}
    floor = _parse_ts(os.environ.get("TREND_FLOOR", ""))

    day_lo = now - dt.timedelta(hours=24)
    base_lo = now - dt.timedelta(days=BASELINE_DAYS)
    if floor:
        base_lo = max(base_lo, floor)
        day_lo = max(day_lo, floor)

    day = [v for ts, v in hist if ts is not None and ts >= day_lo]
    base = [v for ts, v in hist if ts is None or ts >= base_lo]
    return {"prev": hist[-1][1],
            "day": _mean(day) if len(day) >= MIN_POINTS else None,
            "base": _mean(base) if len(base) >= MIN_POINTS else None,
            "n": len(hist)}


def _clauses(metal: str, score: float, now: dt.datetime) -> list[str]:
    w = _windows(metal, now)
    if not w:
        return []
    out = []
    c = _clause(score - w["prev"], score, "the last update")
    out.append(c or "steady since the last update")

    days = int(BASELINE_DAYS)
    if w["day"] is not None:
        c = _clause(score - w["day"], score, "the 24h mean")
        if c:
            out.append(f"{c} ({w['day']:+.0f})")
    if w["base"] is not None:
        c = _clause(score - w["base"], score, f"the {days}-day mean")
        if c:
            out.append(f"{c} ({w['base']:+.0f})")
    return out


def trend_line(metal: str, score: float, now: dt.datetime | None = None) -> str:
    cl = _clauses(metal, float(score), now or dt.datetime.now(dt.timezone.utc))
    if not cl:
        return ""
    body = cl[0] if len(cl) == 1 else ", and ".join([", ".join(cl[:-1]), cl[-1]])
    return body


def summary(scores: dict, now: dt.datetime | None = None) -> str:
    """Single line for the digest header. Collapses metals that agree."""
    now = now or dt.datetime.now(dt.timezone.utc)
    per: dict[str, list[str]] = {}
    for metal, score in (scores or {}).items():
        if metal in ("extras",) or score is None:
            continue
        try:
            cl = _clauses(str(metal).upper(), float(score), now)
        except Exception as exc:                          # noqa: BLE001
            print(f"[warn] patch11 {metal}: {type(exc).__name__}: {exc}")
            continue
        if cl:
            per[str(metal).upper()] = cl
    if not per:
        return ""

    # Group metals whose phrasing is identical (ignoring the numeric tails).
    def shape(cl: list[str]) -> str:
        return " | ".join(c.split(" (")[0] for c in cl)

    groups: dict[str, list[str]] = {}
    for metal, cl in per.items():
        groups.setdefault(shape(cl), []).append(metal)

    chunks = []
    for sh, metals in groups.items():
        cl = per[metals[0]]
        body = cl[0] if len(cl) == 1 else ", and ".join(
            [", ".join(cl[:-1]), cl[-1]])
        label = " and ".join(metals) if len(metals) <= 2 else ", ".join(metals)
        chunks.append(f"{label} {body}")
    line = "; ".join(chunks)
    return line[0].upper() + line[1:]


# Back-compat with v1 callers.
def trend_lines(scores: dict) -> dict:
    return {m: trend_line(m, s) for m, s in (scores or {}).items()
            if m not in ("extras",) and s is not None}


if __name__ == "__main__":
    print(f"history: {HISTORY} exists={HISTORY.exists()} rows={len(_rows())}")
    print(f"floor  : {os.environ.get('TREND_FLOOR') or '(none)'}")
    demo = {}
    for m in ("XAU", "XAG"):
        s = series(m)
        print(f"\n{m}: {len(s)} points  last5={[f'{v:+.0f}' for _, v in s[-5:]]}")
        if s:
            demo[m] = s[-1][1] - 4
            print(f"  verbose: {trend_line(m, demo[m])}")
    print(f"\nDIGEST LINE\n  {summary(demo)}")
