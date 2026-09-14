"""src/patch2.py — corrections for four bugs visible in the 2026-09-14 run.

1. FOMC clock said "in 24d" when the meeting is 15-16 September. The old regex
   scraped the whole calendar page and matched minutes-release strings such as
   "(Released October 08, 2025)", then forced the current year -> 2026-10-08,
   i.e. 24 days out. Now: tag-stripped, year-scoped block parsing of the real
   month/day-range layout, decision day = LAST day of the range, plus a verified
   hardcoded table for 2026-2027 as a floor.

2. "FOMC Press Release 0d | 1d | 2d | 3d" - caused by
   include_release_dates_with_no_data=true, which emits filler dates. Removed,
   plus de-duplication by (date, name).

3. "hawkish lean +0.00" - the old logic needed the literal words hike/cut in a
   Kalshi market title. Real Fed markets are expressed as target ranges or
   "25 bps decrease". Now parses basis-point outcomes into a probability-weighted
   expected rate change, with the old keyword method as fallback.

4. Duplicate POLICY lines - Federal Register results are de-duplicated by title.

Also: 'gsr' only applies to silver, so it no longer counts as a missing component
for gold (confidence was being under-reported at 0.71).

Imported for its side effects at the end of src/sources.py.
"""
from __future__ import annotations

import datetime as dt
import math
import re

import requests

from src import score as _score

UA = {"User-Agent": "vibe-factory/1.2 (personal research)"}
T = 15
S = requests.Session()
S.headers.update(UA)

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}

# Verified against federalreserve.gov/monetarypolicy/fomccalendars.htm (2026-09-14).
# Decision day = second day of each two-day meeting. * = with SEP projections.
FOMC_TABLE = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]
SEP_MEETINGS = {"2026-03-18", "2026-06-17", "2026-09-16", "2026-12-09",
                "2027-03-17", "2027-06-09", "2027-09-15", "2027-12-08"}


# ---------------------------------------------------------------- 1. FOMC clock
def _parse_calendar(html: str) -> list[str]:
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"\(Released[^)]*\)", " ", text)          # kill minutes-release dates
    out = []
    for year in (dt.date.today().year, dt.date.today().year + 1):
        m = re.search(rf"{year}\s+FOMC\s+Meetings(.*?)(?:\d{{4}}\s+FOMC\s+Meetings|$)",
                      text, re.S | re.I)
        if not m:
            continue
        block = m.group(1)
        pattern = (r"\b(January|February|March|April|May|June|July|August|September|"
                   r"October|November|December)\b[^0-9]{0,40}?(\d{1,2})"
                   r"(?:\s*[-\u2013]\s*(\d{1,2}))?")
        for mon, d1, d2 in re.findall(pattern, block, re.I):
            month = MONTHS[mon.lower()]
            start = int(d1)
            end = int(d2) if d2 else start
            y, mo = year, month
            if end < start:                                  # e.g. Apr/May 30-1
                mo = month + 1
                if mo == 13:
                    mo, y = 1, year + 1
            try:
                out.append(dt.date(y, mo, end).isoformat())
            except ValueError:
                continue
    return out


def fomc_dates() -> list[str]:
    dates = set(FOMC_TABLE)
    try:
        scraped = _parse_calendar(
            S.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                  timeout=T).text)
        # only trust scraped dates that are near a known meeting month
        known_months = {d[:7] for d in FOMC_TABLE}
        dates |= {d for d in scraped if d[:7] in known_months}
    except requests.RequestException as exc:
        print(f"[info] fomc calendar scrape failed, using verified table ({exc})")
    return sorted(dates)


def next_fomc() -> tuple[int | None, str | None]:
    today = dt.date.today()
    fut = [d for d in fomc_dates() if d >= today.isoformat()]
    if not fut:
        return None, None
    return (dt.date.fromisoformat(fut[0]) - today).days, fut[0]


# ------------------------------------------------------------ 2. FRED calendar
TIER1 = re.compile(r"consumer price index|employment situation|personal income and outlays|"
                   r"producer price|gross domestic product|fomc", re.I)


def fred_releases(days: int = 21) -> list[dict]:
    import os
    key = os.environ["FRED_API_KEY"]
    today = dt.date.today()
    j = S.get("https://api.stlouisfed.org/fred/releases/dates", timeout=25, params={
        "api_key": key, "file_type": "json",
        "realtime_start": today.isoformat(),
        "realtime_end": (today + dt.timedelta(days=days)).isoformat(),
        "sort_order": "asc", "limit": 400}).json()

    seen, out = set(), []
    for r in j.get("release_dates", []):
        d, name = r.get("date"), (r.get("release_name") or "")
        if not d or d < today.isoformat():
            continue
        key2 = (d, name)
        if key2 in seen:
            continue
        seen.add(key2)
        out.append({"date": d, "name": name, "tier1": bool(TIER1.search(name)),
                    "days_out": (dt.date.fromisoformat(d) - today).days})

    fomc_days, fomc_date = next_fomc()
    if fomc_date and not any(x["date"] == fomc_date and "FOMC" in x["name"] for x in out):
        label = "FOMC decision" + (" + SEP" if fomc_date in SEP_MEETINGS else "")
        out.append({"date": fomc_date, "name": label, "tier1": True,
                    "days_out": fomc_days})
    return sorted(out, key=lambda x: (x["date"], x["name"]))


# --------------------------------------------------- 3. hawkish lean, real math
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
BPS = re.compile(r"(\d{1,3})\s*(?:bps|bp\b|basis\s*point)", re.I)
UP = re.compile(r"increase|hike|raise|higher", re.I)
DOWN = re.compile(r"decrease|cut|lower|reduce", re.I)
FLAT = re.compile(r"no change|unchanged|hold|same", re.I)


def _px(m: dict) -> float | None:
    for a, b, div in (("yes_bid_dollars", "yes_ask_dollars", 2.0),
                      ("yes_bid", "yes_ask", 200.0)):
        x, y = m.get(a), m.get(b)
        try:
            if x is not None and y is not None:
                return (float(x) + float(y)) / div
        except (TypeError, ValueError):
            continue
    for k in ("last_price_dollars", "last_price"):
        v = m.get(k)
        if v is not None:
            try:
                return float(v) / (1.0 if "dollars" in k else 100.0)
            except (TypeError, ValueError):
                pass
    return None


def _outcome_bps(label: str) -> float | None:
    if FLAT.search(label):
        return 0.0
    hit = BPS.search(label)
    if not hit:
        return None
    mag = float(hit.group(1))
    if UP.search(label):
        return +mag
    if DOWN.search(label):
        return -mag
    return None


def fed_expected_bps() -> tuple[float | None, list[dict]]:
    """Probability-weighted expected change in the target rate, in basis points."""
    try:
        ev = S.get(f"{KALSHI}/events", timeout=T,
                   params={"status": "open", "limit": 200}).json().get("events", [])
    except requests.RequestException as exc:
        print(f"[warn] kalshi events: {exc}")
        return None, []

    cands = [e for e in ev
             if re.search(r"\bfed\b|fomc", (e.get("title") or ""), re.I)
             and re.search(r"rate|decision|target", (e.get("title") or ""), re.I)]
    detail = []
    for e in cands[:4]:
        try:
            j = S.get(f"{KALSHI}/events/{e['event_ticker']}", timeout=T,
                      params={"with_nested_markets": "true"}).json()
        except requests.RequestException:
            continue
        markets = j.get("markets") or (j.get("event") or {}).get("markets") or []
        num = den = 0.0
        for m in markets:
            label = " ".join(str(m.get(k) or "") for k in
                             ("yes_sub_title", "subtitle", "title", "ticker"))
            p, bps = _px(m), _outcome_bps(label)
            if p is None or bps is None:
                continue
            num += p * bps
            den += p
            detail.append({"event": e.get("title"), "outcome": label[:60],
                           "p": round(p, 3), "bps": bps})
        if den > 0.05:
            return round(num / den, 2), detail
    return None, detail


def hike_pressure_from(topics: dict) -> float:
    """+1 = fully hawkish (bearish metals), -1 = fully dovish. 25 bps == 1.0."""
    bps, _ = fed_expected_bps()
    if bps is not None:
        return round(max(-1.0, min(1.0, bps / 25.0)), 4)

    hike = cut = 0.0                                          # keyword fallback
    for m in (topics or {}).get("fed", []):
        t = (m.get("title") or "").lower()
        if UP.search(t):
            hike = max(hike, m.get("p", 0.0))
        if DOWN.search(t):
            cut = max(cut, m.get("p", 0.0))
    return round(hike - cut, 4)


# -------------------------------------------------- 4. Federal Register dedupe
from src.fedreg import federal_register as _fr_raw  # noqa: E402


def federal_register(days: int = 5) -> list[dict]:
    seen, out = set(), []
    for d in _fr_raw(days):
        k = (d.get("title") or "")[:90].lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


# ------------------------------- per-metal weights so confidence is honest
_BASE = dict(_score.REGIME_WEIGHTS)
WEIGHTS_BY_METAL = {
    "XAU": {k: v for k, v in _BASE.items() if k != "gsr"},
    "XAG": _BASE,
}


def _blend(components, pressure_score, skew, metal="XAU", weights=None):
    w = weights or WEIGHTS_BY_METAL.get(metal, _BASE)
    used, missing, raw, wsum = {}, [], 0.0, 0.0
    for k, weight in w.items():
        v = components.get(k)
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


_score.blend = _blend          # monkeypatch: run.py resolves score.blend at call time
