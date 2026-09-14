"""src/patch6.py — CFTC positioning, the last missing weighted component.

Source: Disaggregated Futures-Only Commitments of Traders, Socrata dataset
72hh-3qpy on publicreporting.cftc.gov, no authentication required. Managed Money
is the hedge-fund/CTA bucket; net long as a share of open interest is the
standard crowding gauge. Data is published Fridays at 15:30 ET and reflects
positions as of the prior Tuesday, so a 12-hour cache is generous.

COMEX contract codes: gold 088691, silver 084691. Queried by code with a
name-based fallback.

Wiring note: run.py hard-codes cot_crowding=None inside build_components, and
because `python -m src.run` executes run.py as __main__ that function cannot be
monkeypatched from here. Instead the patched blend() fills the component itself
when it arrives as None - blend already knows which metal it is scoring.

Weekly values are appended to state/cot_history.csv rather than the main history
schema, which stays frozen.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import pathlib
import time

import requests

from src import patch2, score as _score

URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
CODES = {"XAU": "088691", "XAG": "084691"}
NAMES = {"XAU": "GOLD - COMMODITY EXCHANGE INC.",
         "XAG": "SILVER - COMMODITY EXCHANGE INC."}
CACHE = pathlib.Path("state/cot.json")
HIST_CSV = pathlib.Path("state/cot_history.csv")
TTL_S = 12 * 3600
WEEKS = 156

S = requests.Session()
S.headers.update({"User-Agent": "vibe-factory/1.6 (personal research)"})


def _rows(metal: str) -> list[dict]:
    params = {
        "$select": "report_date_as_yyyy_mm_dd,open_interest_all,"
                   "m_money_positions_long_all,m_money_positions_short_all",
        "$where": f"cftc_contract_market_code='{CODES[metal]}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": WEEKS,
    }
    r = S.get(URL, params=params, timeout=25)
    if r.status_code == 400 or (r.ok and not r.json()):
        params["$where"] = f"market_and_exchange_names='{NAMES[metal]}'"
        r = S.get(URL, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def _series(metal: str) -> dict:
    out = []
    for row in _rows(metal):
        try:
            oi = float(row["open_interest_all"])
            lng = float(row["m_money_positions_long_all"])
            sht = float(row["m_money_positions_short_all"])
        except (KeyError, TypeError, ValueError):
            continue
        if oi <= 0:
            continue
        out.append({"date": (row.get("report_date_as_yyyy_mm_dd") or "")[:10],
                    "net_pct_oi": round((lng - sht) / oi, 6)})
    out.sort(key=lambda x: x["date"])
    if not out:
        raise ValueError(f"no COT rows for {metal}")
    return {"latest": out[-1]["net_pct_oi"], "asof": out[-1]["date"],
            "hist": [x["net_pct_oi"] for x in out]}


def cot() -> dict:
    if CACHE.exists():
        try:
            blob = json.loads(CACHE.read_text())
            if (time.time() - blob.get("ts", 0)) < TTL_S and blob.get("data"):
                return blob["data"]
        except ValueError:
            pass

    data = {}
    for metal in CODES:
        try:
            data[metal] = _series(metal)
        except (requests.RequestException, ValueError) as exc:
            print(f"[warn] cot {metal}: {type(exc).__name__}: {exc}")

    if data:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"ts": time.time(), "data": data}))
        _log(data)
        pct = {m: round(_pctile(d["hist"], d["latest"]) * 100)
               for m, d in data.items()}
        asof = next(iter(data.values()))["asof"]
        print("[info] COT " + "  ".join(
            f"{m} net {d['latest']*100:+.1f}%OI ({pct[m]}th pct)"
            for m, d in data.items()) + f"  asof {asof}")
    return data


def _pctile(hist: list[float], x: float) -> float:
    h = [v for v in hist if v is not None]
    return sum(1 for v in h if v <= x) / max(len(h), 1)


def _log(data: dict) -> None:
    row = {"date": next(iter(data.values()))["asof"]}
    for m, d in data.items():
        row[f"{m.lower()}_net_pct_oi"] = d["latest"]
        row[f"{m.lower()}_pctile"] = round(_pctile(d["hist"], d["latest"]), 4)
    HIST_CSV.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if HIST_CSV.exists():
        with HIST_CSV.open() as f:
            existing = {r.get("date") for r in csv.DictReader(f)}
    if row["date"] in existing:
        return
    write_header = not HIST_CSV.exists()
    with HIST_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if write_header:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------------ patched blend
def _blend(components, pressure_score, skew, metal="XAU", weights=None):
    comp = dict(components)
    if comp.get("cot_crowding") is None:
        try:
            d = cot().get(metal)
            if d:
                comp["cot_crowding"] = _score.rank(d["hist"], d["latest"])
        except Exception as exc:                               # noqa: BLE001
            print(f"[warn] cot component unavailable: {exc}")

    w = weights or patch2.WEIGHTS_BY_METAL.get(metal, patch2._BASE)
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
    press = 100 * math.tanh((pressure_score + _score.EVENT_SKEW_SCALE * skew) / 1.5)
    total = 0.55 * regime + 0.45 * press
    if metal == "XAG":
        total = 100 * math.tanh(_score.SILVER_BETA * total / 100)
    label = next(name for cut, name in _score.LABELS if total < cut)
    return {"metal": metal, "vibe": round(total, 1), "label": label,
            "regime": round(regime, 1), "pressure": round(press, 1),
            "contributions": used, "missing": missing,
            "confidence": round(1 - len(missing) / max(len(w), 1), 2)}


_score.blend = _blend


def next_cot_release() -> str:
    """COT drops Friday 15:30 ET; useful for the alert footer."""
    today = dt.date.today()
    days = (4 - today.weekday()) % 7
    return (today + dt.timedelta(days=days)).isoformat()
