"""src/patch7.py — de-Fed-ify the score. DEPLOY AFTER WEDNESDAY'S FOMC.

Do not import this before the FOMC window closes: changing the scoring function
mid-window makes Monday/Tuesday/Wednesday's logged points incomparable, which
destroys the first real test of the pre-event thesis.

Four additions, all free:

1. MACRO SURPRISE (was missing entirely). BLS Public Data API v2 - free key,
   500 queries/day, 50 series per request; v1 works with no key at 25/day. One
   POST covers all series. Verified IDs: CUSR0000SA0 (CPI all items SA),
   CUSR0000SA0L1E (core CPI SA), CES0000000001 (total nonfarm), LNS14000000
   (unemployment rate SA), CES0500000003 (average hourly earnings).
   -> component macro_heat: hot prints = hawkish = bearish metals.

2. PRICED INFLATION, not just priced Fed. Kalshi runs monthly CPI ladders under
   KXCPI (signed one-month percent change in SA CPI-U, payout strictly greater
   than the threshold) and core under KXCPICORE. The Fed's own research notes
   Kalshi covers CPI MoM/YoY, core, PCE, unemployment, payrolls, GDP and
   recession probability - so there is no reason to read only the rate ladder.
   -> component inflation_priced.

3. STRESS AND GEOPOLITICS as their own weighted inputs rather than only
   transient pressure: shutdown/tariff market odds plus GDELT tariff tone and
   Federal Register hot-action count.
   -> components stress_priced, geo_risk.

4. FED SPEAKER TONE finally weighted. fed_feed already computes hawk/dove word
   balance and schema.py logs it, but blend gave it zero weight.
   -> component speech_tone.

Plus per-event-type skew: CPI proximity is now signed by the inflation lean, not
by the rate lean, and payroll proximity by labour heat.

Weight share of Fed/rates inputs drops from ~60% to ~49%.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import re
import time

import requests

from src import patch2, patch3, patch5, patch6, score as _score

S = requests.Session()
S.headers.update({"User-Agent": "vibe-factory/1.7 (personal research)"})
CACHE_FILE = pathlib.Path("state/cache.json")
BLS_CACHE = pathlib.Path("state/bls.json")
_MEMO: dict = {}
MEMO_TTL = 300


def _memo(key, fn):
    e = _MEMO.get(key)
    if e and (time.time() - e[0]) < MEMO_TTL:
        return e[1]
    v = fn()
    _MEMO[key] = (time.time(), v)
    return v


def _pct(hist, x):
    h = [v for v in hist if v is not None]
    if len(h) < 8 or x is None:
        return None
    return 2 * (sum(1 for v in h if v <= x) / len(h)) - 1


# ============================================================ 1. BLS surprise
BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
SERIES = {
    "cpi": "CUSR0000SA0",
    "core": "CUSR0000SA0L1E",
    "nfp": "CES0000000001",
    "unrate": "LNS14000000",
    "ahe": "CES0500000003",
}
BLS_TTL = 24 * 3600


def bls_raw() -> dict:
    if BLS_CACHE.exists():
        try:
            blob = json.loads(BLS_CACHE.read_text())
            if (time.time() - blob.get("ts", 0)) < BLS_TTL and blob.get("data"):
                return blob["data"]
        except ValueError:
            pass
    year = dt.date.today().year
    body = {"seriesid": list(SERIES.values()),
            "startyear": str(year - 4), "endyear": str(year)}
    key = os.environ.get("BLS_API_KEY")
    url = BLS_V2 if key else BLS_V1
    if key:
        body["registrationkey"] = key
    r = S.post(url, json=body, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError(f"BLS: {j.get('status')} {j.get('message')}")
    data = {}
    inv = {v: k for k, v in SERIES.items()}
    for s in j["Results"]["series"]:
        name = inv.get(s["seriesID"])
        vals = []
        for d in s["data"]:
            if d.get("period", "").startswith("M") and d.get("value") not in (None, "-"):
                try:
                    vals.append((f"{d['year']}-{d['period'][1:]}", float(d["value"])))
                except ValueError:
                    continue
        vals.sort()
        data[name] = vals
    BLS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    BLS_CACHE.write_text(json.dumps({"ts": time.time(), "data": data}))
    return data


def macro_heat() -> float | None:
    try:
        d = _memo("bls", bls_raw)
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"[warn] bls unavailable: {type(exc).__name__}: {exc}")
        return None

    parts = []
    for name, invert in (("core", False), ("cpi", False), ("nfp", False),
                         ("unrate", True), ("ahe", False)):
        v = d.get(name) or []
        if len(v) < 15:
            continue
        if name == "unrate":
            latest, hist = v[-1][1], [x[1] for x in v[-37:]]
        else:
            mom = [(v[i][0], (v[i][1] / v[i - 1][1] - 1) * 100)
                   for i in range(1, len(v)) if v[i - 1][1]]
            latest, hist = mom[-1][1], [x[1] for x in mom[-37:]]
        p = _pct(hist, latest)
        if p is None:
            continue
        parts.append(-p if invert else p)

    if not parts:
        return None
    heat = max(-1.0, min(1.0, sum(parts) / len(parts)))
    print(f"[info] macro heat {heat:+.2f} from {len(parts)} BLS series")
    return round(heat, 4)


# ==================================================== 2. priced inflation
def _ladder_expected(event: dict) -> float | None:
    """Threshold ladders quote P(value > strike); recover an expected value."""
    rungs = []
    for m in event.get("markets") or []:
        strike = m.get("floor_strike")
        if strike is None:
            strike = (m.get("strike") or {}).get("floor") if isinstance(
                m.get("strike"), dict) else None
        p = patch3._px(m)
        if strike is None or p is None:
            continue
        rungs.append((float(strike), p))
    if len(rungs) < 3:
        return None
    rungs.sort()
    steps = [rungs[i + 1][0] - rungs[i][0] for i in range(len(rungs) - 1)]
    step = min(s for s in steps if s > 0) if any(s > 0 for s in steps) else 0.1
    return rungs[0][0] + sum(p * step for _, p in rungs)


def inflation_priced() -> float | None:
    def go():
        for series in ("KXCPICORE", "KXCPI"):
            try:
                events = patch3._series_events(series)
            except requests.RequestException:
                continue
            ev = patch3._pick_event(events, None)
            if not ev:
                continue
            exp = _ladder_expected(ev)
            if exp is None:
                continue
            print(f"[info] {ev.get('event_ticker')} implied MoM {exp:+.2f}%")
            # 0.2%/m is roughly neutral; 0.4%+ is hot
            return round(max(-1.0, min(1.0, (exp - 0.2) / 0.2)), 4)
        return None
    return _memo("infl", go)


# ============================================= 3. stress and geopolitics
STRESS_SERIES = ("KXGOVSHUTDOWN", "KXSHUTDOWN", "KXTARIFF", "KXRECESSION",
                 "KXUSRECESSION")


def stress_priced() -> float | None:
    def go():
        best, found = 0.0, []
        for series in STRESS_SERIES:
            try:
                events = patch3._series_events(series)
            except requests.RequestException:
                continue
            if not events:
                continue
            for ev in events[:2]:
                for m in ev.get("markets") or []:
                    p = patch3._px(m)
                    if p is not None:
                        best = max(best, p)
                found.append(series)
        if not found:
            return None
        print(f"[info] stress markets {found} max P {best:.2f}")
        return round(max(-1.0, min(1.0, (best - 0.3) / 0.5)), 4)
    return _memo("stress", go)


def geo_risk() -> float | None:
    parts = []
    tones = _memo("gdelt", lambda: patch5.gdelt_tone())
    t = tones.get("tariff")
    if t is not None:
        parts.append(max(-1.0, min(1.0, -t / 4.0)))     # negative tone = risk on the wire
    hot = 0
    if CACHE_FILE.exists():
        try:
            blob = json.loads(CACHE_FILE.read_text())
            docs = (blob.get("fedreg") or {}).get("value") or []
            hot = sum(1 for d in docs if d.get("hot"))
        except (ValueError, AttributeError):
            hot = 0
    parts.append(min(hot, 3) / 3.0)
    if not parts:
        return None
    return round(max(-1.0, min(1.0, sum(parts) / len(parts))), 4)


def speech_tone() -> float | None:
    if not CACHE_FILE.exists():
        return None
    try:
        blob = json.loads(CACHE_FILE.read_text())
        v = (blob.get("fed_feed") or {}).get("value") or {}
        t = v.get("speech_tone")
        return None if t is None else round(max(-1.0, min(1.0, float(t))), 4)
    except (ValueError, TypeError, AttributeError):
        return None


# ==================================================== 4. per-event-type skew
CPI_RX = re.compile(r"consumer price index", re.I)
JOBS_RX = re.compile(r"employment situation", re.I)
FOMC_RX = re.compile(r"fomc", re.I)


def typed_skew() -> tuple[float, str]:
    def go():
        try:
            rel = patch5.fred_releases(21)
        except Exception:                                      # noqa: BLE001
            return 0.0, "none"
        tier1 = [r for r in rel if r.get("tier1") and r["days_out"] is not None
                 and 0 <= r["days_out"] <= 5]
        if not tier1:
            return 0.0, "none"
        nearest = min(tier1, key=lambda r: r["days_out"])
        prox = (6 - nearest["days_out"]) / 6

        name = nearest["name"]
        if FOMC_RX.search(name) or "decision" in name.lower():
            lean, mag, kind = patch3.hike_pressure_from({}), 1.00, "FOMC"
        elif CPI_RX.search(name):
            lean, mag, kind = (inflation_priced() or 0.0), 0.85, "CPI"
        elif JOBS_RX.search(name):
            lean, mag, kind = (macro_heat() or 0.0), 0.70, "JOBS"
        else:
            lean, mag, kind = (macro_heat() or 0.0), 0.45, "MACRO"

        skew = -1.0 * lean * prox * mag        # hot/hawkish lean = bearish metals
        return round(skew, 4), f"{kind} {nearest['days_out']}d lean {lean:+.2f}"
    return _memo("skew", go)


# ============================================================ new weight set
BASE = {
    "real_rate": -0.18,
    "usd": -0.12,
    "hike_pressure": -0.16,
    "inflation_priced": -0.08,
    "macro_heat": -0.08,
    "speech_tone": +0.05,
    "geo_risk": +0.08,
    "stress_priced": +0.06,
    "cot_crowding": -0.10,
    "momentum": +0.15,
    "media_tone": +0.05,
}
WEIGHTS = {"XAU": dict(BASE), "XAG": {**BASE, "gsr": -0.08}}
FED_KEYS = ("real_rate", "usd", "hike_pressure", "speech_tone")


def fed_share(metal: str = "XAU") -> float:
    w = WEIGHTS[metal]
    return sum(abs(w[k]) for k in FED_KEYS if k in w) / sum(abs(v) for v in w.values())


def _extras(metal: str) -> dict:
    out = {
        "macro_heat": _memo("heat", macro_heat),
        "inflation_priced": inflation_priced(),
        "stress_priced": stress_priced(),
        "geo_risk": geo_risk(),
        "speech_tone": speech_tone(),
    }
    if comp_cot := patch6.cot().get(metal):
        out["cot_crowding"] = _score.rank(comp_cot["hist"], comp_cot["latest"])
    return out


def _blend(components, pressure_score, skew, metal="XAU", weights=None):
    comp = {**components}
    for k, v in _extras(metal).items():
        if comp.get(k) is None:
            comp[k] = v

    own_skew, why = typed_skew()
    skew_used = own_skew if own_skew else skew

    w = weights or WEIGHTS.get(metal, BASE)
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
    return {"metal": metal, "vibe": round(total, 1), "label": label,
            "regime": round(regime, 1), "pressure": round(press, 1),
            "contributions": used, "missing": missing, "skew_why": why,
            "confidence": round(1 - len(missing) / max(len(w), 1), 2)}


_score.blend = _blend
print(f"[info] patch7 active: {len(BASE)} base components, "
      f"Fed/rates weight share {fed_share('XAU'):.0%}")
