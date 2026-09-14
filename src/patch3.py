"""src/patch3.py — real policy-expectations parser, written against the actual
payloads your probe returned on 2026-09-14.

What the probe proved:
  * Series KXFEDDECISION exists and carries the decision ladder as custom_strike:
      {"Cut": ">25"} / {"Cut": "25"} / {"Hike": "0"} / {"Hike": "25"} / {"Hike": ">25"}
    For KXFEDDECISION-26SEP the prices were 0.01 / 0.01 / 0.20 / 0.80 / 0.02.
  * Series KXFED carries a cumulative "Above X%" strike ladder (floor_strike),
    which recovers the implied target rate level after the meeting.
  * Unfiltered /events page 1 returns long-dated 2034-2036 contracts, which is
    why keyword scanning never found the Fed markets. Series filtering is required.
  * Polymarket needs /public-search with q= (limit is ignored; use limit_per_type).

Imported for side effects at the end of src/sources.py.
"""
from __future__ import annotations

import datetime as dt
import re

import requests

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
S = requests.Session()
S.headers.update({"User-Agent": "vibe-factory/1.3 (personal research)"})
T = 15

DECISION_SERIES = "KXFEDDECISION"
LEVEL_SERIES = "KXFED"
BIG_MOVE_BPS = 50.0          # ">25" resolves to 50 bps in practice
MONTH3 = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

LAST_DETAIL: dict = {}


def _px(m: dict) -> float | None:
    pairs = (("yes_bid_dollars", "yes_ask_dollars", 2.0),
             ("yes_bid", "yes_ask", 200.0),
             ("previous_yes_bid_dollars", "previous_yes_ask_dollars", 2.0))
    for a, b, div in pairs:
        x, y = m.get(a), m.get(b)
        try:
            if x is not None and y is not None and (float(x) + float(y)) > 0:
                return (float(x) + float(y)) / div
        except (TypeError, ValueError):
            continue
    for k, div in (("last_price_dollars", 1.0), ("last_price", 100.0)):
        v = m.get(k)
        try:
            if v is not None:
                return float(v) / div
        except (TypeError, ValueError):
            continue
    return None


def _bps_from_custom_strike(m: dict) -> float | None:
    cs = m.get("custom_strike")
    if isinstance(cs, dict):
        for side, raw in cs.items():
            s = str(side).lower()
            sign = +1.0 if "hike" in s else (-1.0 if "cut" in s else 0.0)
            val = str(raw).strip()
            if val in ("0", "0bps"):
                return 0.0
            mag = BIG_MOVE_BPS if val.startswith(">") else None
            if mag is None:
                hit = re.search(r"(\d{1,3})", val)
                mag = float(hit.group(1)) if hit else None
            if mag is None:
                return None
            return sign * mag
    # fallback: read the human label
    label = " ".join(str(m.get(k) or "") for k in
                     ("yes_sub_title", "no_sub_title", "subtitle", "title"))
    if re.search(r"maintain|no change|unchanged|hold", label, re.I):
        return 0.0
    hit = re.search(r"(\d{1,3})\s*bps", label, re.I)
    if not hit:
        return None
    mag = float(hit.group(1))
    if re.search(r">\s*\d+\s*bps", label):
        mag = BIG_MOVE_BPS
    if re.search(r"cut|decrease|lower", label, re.I):
        return -mag
    if re.search(r"hike|increase|raise", label, re.I):
        return +mag
    return None


def _series_events(series: str) -> list[dict]:
    r = S.get(f"{KALSHI}/events", timeout=T, params={
        "series_ticker": series, "status": "open",
        "with_nested_markets": "true", "limit": 50})
    r.raise_for_status()
    return r.json().get("events", [])


def _target_suffix(when: dt.date) -> str:
    return f"{when.year % 100:02d}{MONTH3[when.month - 1]}"


def _pick_event(events: list[dict], target: dt.date | None) -> dict | None:
    if not events:
        return None
    if target:
        want = _target_suffix(target)
        for e in events:
            if str(e.get("event_ticker", "")).upper().endswith(want):
                return e
    def key(e):
        t = str(e.get("event_ticker", ""))
        m = re.search(r"(\d{2})([A-Z]{3})$", t.upper())
        if not m:
            return (99, 99)
        return (int(m.group(1)), MONTH3.index(m.group(2)) if m.group(2) in MONTH3 else 99)
    return sorted(events, key=key)[0]


def fed_expected_bps() -> tuple[float | None, list[dict]]:
    """Probability-weighted expected change in the target rate, in basis points."""
    global LAST_DETAIL
    try:
        from src.patch2 import next_fomc
        days, date_s = next_fomc()
        target = dt.date.fromisoformat(date_s) if date_s else None
    except Exception:                                          # noqa: BLE001
        days, target = None, None

    try:
        events = _series_events(DECISION_SERIES)
    except requests.RequestException as exc:
        print(f"[warn] kalshi {DECISION_SERIES}: {exc}")
        return None, []

    ev = _pick_event(events, target)
    if not ev:
        return None, []

    detail, num, den = [], 0.0, 0.0
    for m in ev.get("markets") or []:
        p, bps = _px(m), _bps_from_custom_strike(m)
        if p is None or bps is None:
            continue
        num += p * bps
        den += p
        detail.append({"outcome": (m.get("yes_sub_title") or m.get("no_sub_title")
                                   or str(m.get("custom_strike"))),
                       "p": round(p, 3), "bps": bps})
    if den < 0.5:                                              # ladder looks broken
        return None, detail

    exp = num / den
    LAST_DETAIL = {"event": ev.get("event_ticker"), "title": ev.get("title"),
                   "days_out": days, "expected_bps": round(exp, 2),
                   "outcomes": sorted(detail, key=lambda d: -d["p"])[:5],
                   "p_hike": round(sum(d["p"] for d in detail if d["bps"] > 0) / den, 3),
                   "p_hold": round(sum(d["p"] for d in detail if d["bps"] == 0) / den, 3),
                   "p_cut": round(sum(d["p"] for d in detail if d["bps"] < 0) / den, 3)}
    print(f"[info] {LAST_DETAIL['event']}: E[change] {exp:+.1f}bps  "
          f"P(hike) {LAST_DETAIL['p_hike']:.2f}  P(hold) {LAST_DETAIL['p_hold']:.2f}  "
          f"P(cut) {LAST_DETAIL['p_cut']:.2f}")
    return round(exp, 2), detail


def implied_target_rate() -> float | None:
    """Recover the implied post-meeting target rate from the KXFED 'Above X%' ladder."""
    try:
        events = _series_events(LEVEL_SERIES)
    except requests.RequestException:
        return None
    try:
        from src.patch2 import next_fomc
        _, date_s = next_fomc()
        target = dt.date.fromisoformat(date_s) if date_s else None
    except Exception:                                          # noqa: BLE001
        target = None
    ev = _pick_event(events, target)
    if not ev:
        return None
    rungs = []
    for m in ev.get("markets") or []:
        strike, p = m.get("floor_strike"), _px(m)
        if strike is None or p is None:
            continue
        rungs.append((float(strike), p))
    if not rungs:
        return None
    rungs.sort()
    # expected level = lowest strike + sum of 25bp steps weighted by P(above)
    expected = rungs[0][0] + sum(p * 0.25 for _, p in rungs)
    return round(expected, 3)


def hike_pressure_from(topics: dict | None = None) -> float:
    """+1 = fully hawkish (bearish metals), -1 = fully dovish. 25 bps == 1.0."""
    bps, _ = fed_expected_bps()
    if bps is not None:
        return round(max(-1.0, min(1.0, bps / 25.0)), 4)
    hike = cut = 0.0
    for m in (topics or {}).get("fed", []):
        t = (m.get("title") or "").lower()
        if re.search(r"hike|increase|raise", t):
            hike = max(hike, m.get("p", 0.0))
        if re.search(r"cut|decrease|lower", t):
            cut = max(cut, m.get("p", 0.0))
    return round(hike - cut, 4)


# ------------------------------------------------------- Polymarket, fixed
PM_QUERIES = ("fed decision", "fed rate", "powell", "tariff", "recession")


def polymarket_odds() -> list[dict]:
    """/public-search takes q= (query= returns 422) and pages via limit_per_type."""
    seen, out = set(), []
    for q in PM_QUERIES:
        try:
            j = S.get(f"{GAMMA}/public-search", timeout=T,
                      params={"q": q, "limit_per_type": 10, "events_status": "active"}).json()
        except (requests.RequestException, ValueError) as exc:
            print(f"[warn] polymarket search '{q}': {exc}")
            continue
        buckets = []
        if isinstance(j, dict):
            for key in ("events", "markets"):
                v = j.get(key)
                if isinstance(v, list):
                    buckets.extend(v)
        for item in buckets:
            for m in ([item] + list(item.get("markets") or [])):
                question = m.get("question") or m.get("title")
                if not question or question in seen:
                    continue
                prices = m.get("outcomePrices") or m.get("outcome_prices")
                p = None
                if isinstance(prices, str):
                    try:
                        import json as _json
                        p = float(_json.loads(prices)[0])
                    except (ValueError, IndexError, TypeError):
                        p = None
                elif isinstance(prices, list) and prices:
                    try:
                        p = float(prices[0])
                    except (ValueError, TypeError):
                        p = None
                if p is None:
                    continue
                seen.add(question)
                out.append({"question": question[:110], "p": round(p, 4), "q": q})
    return out[:40]
