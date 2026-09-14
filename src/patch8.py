"""src/patch8.py — the polish pass. Three things, no new behaviour in the score.

1. STRESS TICKERS, verified instead of guessed. patch7 tried KXGOVSHUTDOWN /
   KXSHUTDOWN / KXTARIFF / KXUSRECESSION, none of which exist. The real series
   are KXGOVTSHUTDOWN (e.g. KXGOVTSHUTDOWN-26OCT01, "Will the US government be
   shut down on Oct 1, 2026?"), KXRECSSNBER (KXRECSSNBER-26 / -27, resolving on
   two consecutive negative real GDP quarters per BEA despite the NBER in the
   name) and KXGOVTCUTS (federal spending cuts). Also picks up KXJOBLESS if
   listed.

2. NOTHING COMPUTED IS THROWN AWAY. patch7 added five components that the frozen
   schema does not log. Every blend call now appends the full component vector,
   its weighted contributions and the resulting vibe to state/components.csv.
   That file is what audit.py and calibrate.py will regress on later.

3. NOTHING BREAKS SILENTLY. API drift has been the only real failure mode here.
   Each run compares its missing-component set with the previous run for the
   same metal. A component that was working and has stopped triggers a REGRESSION
   line and, when Telegram is configured, a message. Recoveries are logged too.

Imported for side effects at the end of src/sources.py. Import AFTER patch7.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import pathlib

import requests

from src import patch3, patch7, score as _score

COMP_CSV = pathlib.Path("state/components.csv")
HEALTH = pathlib.Path("state/health.json")

# Verified 2026-09-14. KXTARIFF and KXGOVSHUTDOWN do not exist.
STRESS_SERIES = ("KXGOVTSHUTDOWN", "KXRECSSNBER", "KXGOVTCUTS", "KXJOBLESS")


def stress_priced() -> float | None:
    """Max implied probability across stress markets, mapped to [-1, 1].
    Higher stress = bullish metals."""
    def go():
        best, found = 0.0, {}
        for series in STRESS_SERIES:
            try:
                events = patch3._series_events(series)
            except requests.RequestException:
                continue
            if not events:
                continue
            top = 0.0
            for ev in events[:3]:
                for m in ev.get("markets") or []:
                    p = patch3._px(m)
                    if p is not None:
                        top = max(top, p)
            if top:
                found[series] = round(top, 3)
                best = max(best, top)
        if not found:
            print("[warn] no stress markets resolved")
            return None
        print("[info] stress " + "  ".join(f"{k} {v:.2f}" for k, v in found.items()))
        return round(max(-1.0, min(1.0, (best - 0.3) / 0.5)), 4)
    return patch7._memo("stress", go)


patch7.stress_priced = stress_priced          # patch7._extras resolves at call time


# ----------------------------------------------------------- component log
def _log_components(metal: str, comp: dict, used: dict, result: dict,
                    skew_why: str) -> None:
    keys = sorted(patch7.BASE) + ["gsr"]
    row = {"ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "metal": metal, "vibe": result["vibe"], "label": result["label"],
           "regime": result["regime"], "pressure": result["pressure"],
           "confidence": result["confidence"], "skew_why": skew_why,
           "weights_version": "patch7"}
    for k in keys:
        row[f"c_{k}"] = comp.get(k, "")
        row[f"w_{k}"] = used.get(k, "")
    row["missing"] = "|".join(result["missing"])

    COMP_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not COMP_CSV.exists()
    with COMP_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------- health regression
def _notify(text: str) -> None:
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if os.environ.get("DRY_RUN", "").lower() == "true" or not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=10)
    except requests.RequestException:
        pass


def _check_health(metal: str, missing: list[str]) -> None:
    state = {}
    if HEALTH.exists():
        try:
            state = json.loads(HEALTH.read_text())
        except ValueError:
            state = {}
    prev = set(state.get(metal, []))
    now = set(missing)

    broke = sorted(now - prev)
    fixed = sorted(prev - now)
    if broke:
        msg = f"REGRESSION {metal}: {', '.join(broke)} stopped reporting"
        print(f"[ALERT] {msg}")
        _notify(f"vibe-factory {msg}")
    if fixed:
        print(f"[info] recovered {metal}: {', '.join(fixed)}")

    state[metal] = sorted(now)
    state["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps(state, indent=1))


# ------------------------------------------------------------ final blend
def _blend(components, pressure_score, skew, metal="XAU", weights=None):
    comp = {**components}
    for k, v in patch7._extras(metal).items():
        if comp.get(k) is None:
            comp[k] = v

    own_skew, why = patch7.typed_skew()
    skew_used = own_skew if own_skew else skew

    w = weights or patch7.WEIGHTS.get(metal, patch7.BASE)
    used, missing, raw, wsum = {}, [], 0.0, 0.0
    for k, weight in w.items():
        v = comp.get(k)
        if v is None:
            missing.append(k)
            continue
        used[k] = round(weight * v, 4)
        raw += weight * v
        wsum += abs(weight)
    if wsum > 0:
        raw *= sum(abs(x) for x in w.values()) / wsum

    regime = 100 * math.tanh(raw / 1.5)
    press = 100 * math.tanh((pressure_score + _score.EVENT_SKEW_SCALE * skew_used) / 1.5)
    total = 0.55 * regime + 0.45 * press
    if metal == "XAG":
        total = 100 * math.tanh(_score.SILVER_BETA * total / 100)
    label = next(name for cut, name in _score.LABELS if total < cut)

    result = {"metal": metal, "vibe": round(total, 1), "label": label,
              "regime": round(regime, 1), "pressure": round(press, 1),
              "contributions": used, "missing": missing, "skew_why": why,
              "confidence": round(1 - len(missing) / max(len(w), 1), 2)}

    try:
        _log_components(metal, comp, used, result, why)
        _check_health(metal, missing)
    except OSError as exc:
        print(f"[warn] component log failed: {exc}")

    return result


_score.blend = _blend
print("[info] patch8 active: verified stress tickers, component logging, "
      "regression alerts")
