"""src/patch5.py — GDELT politeness + event-clock correction.

Problem 1: patch4 fired 4 GDELT queries with up to 3 attempts each, in one run.
GDELT's own throttle message asks for one request every 5 seconds, and measured
behaviour in 2026 shows that once you trip it, retrying more makes the block
last longer rather than shorter. Hence the 429 plus three RemoteDisconnected.

New policy:
  * at most ONE GDELT request per run, round-robin across the four topics
  * a hard 6-second floor between requests inside a process
  * single attempt, no retry ladder
  * on 429 (or a dropped connection) open a 45-minute cooldown persisted in
    state/gdelt_state.json, so subsequent runs skip GDELT entirely
  * tone values cached for 12 hours each, so all four topics fill over a few
    runs and then stay warm

Problem 2: FRED lists an "FOMC Press Release" release date for today, which the
tier-1 filter read as an event 0 days away. That drove the event skew to full
strength (pressure -78) even though the decision is Wednesday. The real FOMC
date is injected separately by patch2, so 'fomc' is removed from the FRED
tier-1 pattern.

Imported for side effects at the end of src/sources.py.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import time

import requests

STATE = pathlib.Path("state/gdelt_state.json")
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_GAP_S = 6.0
COOLDOWN_S = 45 * 60
TONE_TTL_S = 12 * 3600

QUERIES = {
    "gold": '(gold price OR bullion OR "safe haven") sourcelang:english',
    "silver": '(silver price OR "silver squeeze") sourcelang:english',
    "fed": '("federal reserve" OR "rate hike" OR "rate cut") sourcelang:english',
    "tariff": '(tariff OR "trade war") sourcelang:english',
}
ORDER = list(QUERIES)
_LAST_CALL = 0.0

S = requests.Session()
S.headers.update({"User-Agent": "vibe-factory/1.5 (personal research)"})


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except ValueError:
            pass
    return {"tones": {}, "cooldown_until": 0.0, "cursor": 0}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d))


def _fetch_one(topic: str, timespan: str = "3d") -> float:
    global _LAST_CALL
    gap = time.time() - _LAST_CALL
    if gap < MIN_GAP_S:
        time.sleep(MIN_GAP_S - gap)
    _LAST_CALL = time.time()
    r = S.get(GDELT, timeout=20, params={"query": QUERIES[topic],
                                        "mode": "timelinetone",
                                        "timespan": timespan, "format": "json"})
    if r.status_code == 429:
        retry = r.headers.get("Retry-After")
        raise PermissionError(f"429 rate limited (Retry-After={retry})")
    r.raise_for_status()
    body = r.text.strip()
    if not body.startswith("{"):
        raise ValueError(f"non-JSON: {body[:80]}")
    tl = r.json().get("timeline") or []
    if not tl or not tl[0].get("data"):
        raise ValueError("empty timeline")
    data = tl[0]["data"]
    return round(sum(d["value"] for d in data) / len(data), 4)


def gdelt_tone(timespan: str = "3d") -> dict:
    st = _load()
    now = time.time()
    tones = st.get("tones", {})

    out = {}
    for topic in ORDER:
        e = tones.get(topic)
        out[topic] = e["v"] if e and (now - e["ts"]) < TONE_TTL_S else None

    if now < st.get("cooldown_until", 0):
        left = int((st["cooldown_until"] - now) / 60)
        print(f"[info] gdelt in cooldown for {left} more min; serving cache")
        return out

    # refresh exactly one stale topic per run, round-robin
    stale = [t for t in ORDER if out[t] is None]
    if not stale:
        return out
    cursor = st.get("cursor", 0) % len(ORDER)
    pick = next((ORDER[(cursor + i) % len(ORDER)] for i in range(len(ORDER))
                 if ORDER[(cursor + i) % len(ORDER)] in stale), stale[0])
    st["cursor"] = (ORDER.index(pick) + 1) % len(ORDER)

    try:
        v = _fetch_one(pick, timespan)
        tones[pick] = {"v": v, "ts": now}
        out[pick] = v
        print(f"[info] gdelt {pick} tone {v:+.2f} "
              f"({sum(1 for t in ORDER if out[t] is not None)}/4 topics warm)")
    except (PermissionError, requests.RequestException, ValueError) as exc:
        st["cooldown_until"] = now + COOLDOWN_S
        print(f"[warn] gdelt {pick} failed, cooling down 45min ({type(exc).__name__}: {exc})")

    st["tones"] = tones
    _save(st)
    return out


# ---------------------------------------------------- event clock correction
TIER1 = re.compile(r"consumer price index|employment situation|"
                   r"personal income and outlays|producer price|"
                   r"gross domestic product", re.I)          # 'fomc' deliberately absent


def fred_releases(days: int = 21) -> list[dict]:
    from src.patch2 import next_fomc, SEP_MEETINGS
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
        if re.search(r"fomc", name, re.I):        # handled explicitly below
            continue
        k = (d, name)
        if k in seen:
            continue
        seen.add(k)
        out.append({"date": d, "name": name, "tier1": bool(TIER1.search(name)),
                    "days_out": (dt.date.fromisoformat(d) - today).days})

    days_out, date_s = next_fomc()
    if date_s:
        label = "FOMC decision" + (" + SEP" if date_s in SEP_MEETINGS else "")
        out.append({"date": date_s, "name": label, "tier1": True, "days_out": days_out})
    return sorted(out, key=lambda x: (x["date"], x["name"]))
