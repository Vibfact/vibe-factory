"""src/schema.py — the history file. Freeze this before adding more sources:
scores can be recomputed, a broken history cannot be recovered."""
from __future__ import annotations

import csv
import os

HEADER = [
    "ts_utc",
    # outputs
    "xau_vibe", "xau_label", "xau_regime", "xau_pressure", "xau_conf",
    "xag_vibe", "xag_label", "xag_regime", "xag_pressure", "xag_conf",
    # raw inputs (needed for any later re-fit)
    "xau_px", "xag_px", "gsr",
    "xau_mom20", "xag_mom20", "xau_rv20", "xag_rv20", "xau_dd1y", "xag_dd1y",
    "real10y", "real10y_chg5d", "usd", "breakeven", "effr",
    "hike_pressure", "pm_market_count",
    "next_tier1_days", "next_fomc_days", "next_tier1_name",
    "trump_pressure", "trump_hit_count",
    "fedreg_hot_count",
    "tone_gold", "tone_silver", "tone_fed", "tone_tariff",
    "fed_speech_tone",
    "failed_sources",
]


def init(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(HEADER)


def _g(d: dict | None, *keys, default=""):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def append_row(path: str, ts: str, results: list[dict], data: dict, meta: dict) -> None:
    init(path)
    r = {x["metal"]: x for x in results}
    met, macro = data.get("metals") or {}, data.get("macro") or {}
    gdelt, fed = data.get("gdelt") or {}, data.get("fed_feed") or {}
    clock = meta.get("clock") or {}
    events = meta.get("events") or []
    fedreg = data.get("fedreg") or []
    pm = data.get("polymarket") or []

    row = [
        ts,
        _g(r, "XAU", "vibe"), _g(r, "XAU", "label"), _g(r, "XAU", "regime"),
        _g(r, "XAU", "pressure"), _g(r, "XAU", "confidence"),
        _g(r, "XAG", "vibe"), _g(r, "XAG", "label"), _g(r, "XAG", "regime"),
        _g(r, "XAG", "pressure"), _g(r, "XAG", "confidence"),
        _g(met, "XAU", "px"), _g(met, "XAG", "px"), _g(met, "ratio", "level"),
        _g(met, "XAU", "mom_20"), _g(met, "XAG", "mom_20"),
        _g(met, "XAU", "rv20_ann"), _g(met, "XAG", "rv20_ann"),
        _g(met, "XAU", "dd_1y"), _g(met, "XAG", "dd_1y"),
        _g(macro, "real10y", "level"), _g(macro, "real10y", "chg_5d"),
        _g(macro, "usd", "level"), _g(macro, "breakeven", "level"),
        _g(macro, "effr", "level"),
        meta.get("hike", ""), len(pm),
        clock.get("next_tier1_days", ""), clock.get("next_fomc_days", ""),
        (clock.get("upcoming") or [""])[0],
        round(sum(e["impact"] for e in events), 4) if events else 0.0, len(events),
        sum(1 for d in fedreg if d.get("hot")),
        gdelt.get("gold", ""), gdelt.get("silver", ""),
        gdelt.get("fed", ""), gdelt.get("tariff", ""),
        fed.get("speech_tone", ""),
        "|".join(_g(r, "XAU", "missing", default=[]) or []),
    ]
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)
