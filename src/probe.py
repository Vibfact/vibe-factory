"""src/probe.py — diagnostic. Clears the cache, then shows exactly what each
policy-expectations endpoint returns so the parser can be fixed against reality.

    python -m src.probe

Kalshi organises tickers as Series -> Event -> Market, and Fed rate decisions
use the KXFED series prefix (e.g. KXFED-26MAR19). Filtering by series_ticker is
the supported way to find them; scanning /markets unfiltered mostly returns
sports and never reaches the Fed contracts.
"""
from __future__ import annotations

import json
import pathlib

import requests

S = requests.Session()
S.headers.update({"User-Agent": "vibe-factory-probe/1.0"})
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"

CACHE = pathlib.Path("state/cache.json")
SERIES_CANDIDATES = ["KXFED", "KXFEDDECISION", "FED", "FEDDECISION", "KXFEDRATE"]


def line(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def clear_cache():
    if CACHE.exists():
        CACHE.unlink()
        print("[ok] deleted state/cache.json - next run refetches everything")
    else:
        print("[info] no cache file present")


def probe_clock():
    line("1. FOMC clock and release calendar")
    from src import patch2
    print("fomc_dates (first 6):", patch2.fomc_dates()[:6])
    print("next_fomc:", patch2.next_fomc())
    try:
        rel = patch2.fred_releases(21)
        print(f"releases returned: {len(rel)}")
        for r in rel[:10]:
            flag = "TIER1" if r["tier1"] else "     "
            print(f"  {flag} {r['date']}  +{r['days_out']:>2}d  {r['name'][:52]}")
    except Exception as exc:                                   # noqa: BLE001
        print("releases failed:", type(exc).__name__, exc)


def probe_kalshi_series():
    line("2. Kalshi: which series ticker exists?")
    for st in SERIES_CANDIDATES:
        try:
            r = S.get(f"{KALSHI}/series/{st}", timeout=15)
            print(f"  /series/{st:<14} -> {r.status_code} "
                  f"{str(r.json())[:110] if r.ok else r.text[:80]}")
        except requests.RequestException as exc:
            print(f"  /series/{st:<14} -> {type(exc).__name__}: {exc}")


def probe_kalshi_events():
    line("3. Kalshi: open events per candidate series (with nested markets)")
    for st in SERIES_CANDIDATES:
        try:
            r = S.get(f"{KALSHI}/events", timeout=20, params={
                "series_ticker": st, "status": "open",
                "with_nested_markets": "true", "limit": 20})
            if not r.ok:
                print(f"  {st}: HTTP {r.status_code} {r.text[:70]}")
                continue
            evs = r.json().get("events", [])
            print(f"  {st}: {len(evs)} events")
            for e in evs[:3]:
                print(f"    event {e.get('event_ticker')}  {str(e.get('title'))[:60]}")
                for m in (e.get("markets") or [])[:8]:
                    keys = {k: m[k] for k in m
                            if any(s in k for s in ("ticker", "sub_title", "subtitle",
                                                    "yes_bid", "yes_ask", "last_price",
                                                    "strike", "cap", "floor", "volume"))}
                    print("      ", json.dumps(keys, default=str)[:200])
        except requests.RequestException as exc:
            print(f"  {st}: {type(exc).__name__}: {exc}")


def probe_kalshi_search():
    line("4. Kalshi: keyword scan of open events (no series filter, 1 page)")
    try:
        r = S.get(f"{KALSHI}/events", timeout=20,
                  params={"status": "open", "limit": 200})
        evs = r.json().get("events", []) if r.ok else []
        print(f"  fetched {len(evs)} events, HTTP {r.status_code}")
        hits = [e for e in evs
                if any(k in (str(e.get("title", "")) + str(e.get("event_ticker", ""))).upper()
                       for k in ("FED", "RATE", "FOMC", "CPI", "INFLATION"))]
        for e in hits[:12]:
            print(f"    {e.get('event_ticker'):<22} {str(e.get('title'))[:60]}")
        if not hits:
            print("    no macro events on page 1 - series filter is mandatory")
    except requests.RequestException as exc:
        print("  failed:", exc)


def probe_expected_bps():
    line("5. Current expected-change calculation")
    from src import patch2
    bps, detail = patch2.fed_expected_bps()
    print("expected bps:", bps)
    for d in detail[:10]:
        print("   ", d)
    print("hike_pressure_from({}):", patch2.hike_pressure_from({}))


def probe_polymarket():
    line("6. Polymarket Fed questions")
    try:
        r = S.get(f"{GAMMA}/markets/keyset", timeout=20,
                  params={"closed": "false", "limit": 100})
        ms = r.json().get("markets", []) if r.ok else []
        print(f"  HTTP {r.status_code}, {len(ms)} markets on page 1")
        for m in ms:
            q = (m.get("question") or "").lower()
            if any(k in q for k in ("fed", "fomc", "rate", "powell")):
                print(f"    {str(m.get('question'))[:70]}  id={m.get('id')}")
    except requests.RequestException as exc:
        print("  failed:", exc)


if __name__ == "__main__":
    clear_cache()
    probe_clock()
    probe_kalshi_series()
    probe_kalshi_events()
    probe_kalshi_search()
    probe_expected_bps()
    probe_polymarket()
    print("\nDone. Paste this whole output back into the chat.")
