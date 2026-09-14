"""src/patch4.py — efficiency and diagnostics after the first successful run.

1. hike_pressure_from was hitting Kalshi twice per run (once for XAU, once for
   XAG), visible as the duplicated "[info] KXFEDDECISION-26SEP" line. Now
   memoised for 90 seconds, so both metals share one quote.

2. media_tone came back None while no source was reported as failed: gdelt_tone
   swallows every exception per query and returns None. Now it prints the reason,
   retries with a wider timespan, and drops the sourcelang filter as a last
   attempt before giving up.

Imported for side effects at the end of src/sources.py.
"""
from __future__ import annotations

import time

import requests

from src import patch3

_CACHE: dict = {"ts": 0.0, "value": None}
TTL_S = 90


def hike_pressure_from(topics: dict | None = None) -> float:
    if _CACHE["value"] is not None and (time.time() - _CACHE["ts"]) < TTL_S:
        return _CACHE["value"]
    v = patch3.hike_pressure_from(topics)
    _CACHE.update({"ts": time.time(), "value": v})
    return v


GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERIES = {
    "gold": '(gold price OR bullion OR "safe haven")',
    "silver": '(silver price OR "silver squeeze")',
    "fed": '("federal reserve" OR "rate hike" OR "rate cut")',
    "tariff": '(tariff OR "trade war")',
}
S = requests.Session()
S.headers.update({"User-Agent": "vibe-factory/1.4 (personal research)"})


def _tone_once(query: str, timespan: str) -> float | None:
    r = S.get(GDELT, timeout=20, params={"query": query, "mode": "timelinetone",
                                         "timespan": timespan, "format": "json"})
    r.raise_for_status()
    body = r.text.strip()
    if not body.startswith("{"):
        raise ValueError(f"non-JSON reply: {body[:90]}")
    j = r.json()
    tl = j.get("timeline") or []
    if not tl or not tl[0].get("data"):
        raise ValueError("empty timeline")
    data = tl[0]["data"]
    return round(sum(d["value"] for d in data) / len(data), 4)


def gdelt_tone(timespan: str = "3d") -> dict:
    out = {}
    for name, base in QUERIES.items():
        attempts = [
            (f"{base} sourcelang:english", timespan),
            (f"{base} sourcelang:english", "7d"),
            (base, "7d"),
        ]
        for q, span in attempts:
            try:
                out[name] = _tone_once(q, span)
                break
            except (requests.RequestException, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
        else:
            out[name] = None
            print(f"[warn] gdelt '{name}' unavailable ({last})")
    live = {k: v for k, v in out.items() if v is not None}
    if live:
        print("[info] gdelt tone " +
              " ".join(f"{k} {v:+.2f}" for k, v in live.items()))
    return out
