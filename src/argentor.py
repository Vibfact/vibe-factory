"""src/argentor.py  (v2 | exact pair matching)

v1 guessed units from magnitude and averaged EUR with USD quotes, producing
XAU 4008.84 from a EUR bid and a USD ask. The feed needs no guessing: it
returns explicit pairs with BID and ASK.

Observed payload (2026-09-14):
    XAUUSD 4296.44/4297.41      XAGUSD 63.472/63.554
    XAUEUR 3720.24/3722.40      XAGEUR 54.983/55.045
    EURUSD 1.15432/1.15497
    XAUEURGMS 119.63/119.65     XAGEURGMS 1.768/1.770      (EUR per gram)
    XAUEURKG4X9 119630.62/...   XAGEURKG4X9 1767.608/...   (EUR per kg, 4N9)
    XPTUSD, XPTEUR, XPDUSD, XPDEUR

Auth: GET with header `TokenID`, value from env ARGENTOR_TOKEN.
Probe with:  python -m src.argentor
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

BASE = "https://trading.argentoressayeurs.be/ntpconnect/"
DEFAULT_PATH = "v1/GetAllRates"
TIMEOUT = 12
CACHE_TTL = 60
_CACHE: dict = {"ts": 0.0, "value": None}

S = requests.Session()
S.headers.update({"User-Agent": "vibe-factory/1.9"})

# pair -> (metal, currency, unit)
PAIRS = {
    "XAUUSD": ("XAU", "USD", "oz"), "XAGUSD": ("XAG", "USD", "oz"),
    "XAUEUR": ("XAU", "EUR", "oz"), "XAGEUR": ("XAG", "EUR", "oz"),
    "XPTUSD": ("XPT", "USD", "oz"), "XPDUSD": ("XPD", "USD", "oz"),
    "XPTEUR": ("XPT", "EUR", "oz"), "XPDEUR": ("XPD", "EUR", "oz"),
    "XAUEURGMS": ("XAU", "EUR", "g"), "XAGEURGMS": ("XAG", "EUR", "g"),
    "XAUEURKG4X9": ("XAU", "EUR", "kg"), "XAGEURKG4X9": ("XAG", "EUR", "kg"),
}
SANITY = {"XAU": (500, 20000), "XAG": (5, 500), "XPT": (200, 10000), "XPD": (200, 10000)}


def _token() -> str:
    t = os.environ.get("ARGENTOR_TOKEN")
    if not t:
        raise RuntimeError("ARGENTOR_TOKEN not set")
    return t.strip()


def fetch_raw(path: str | None = None):
    url = BASE + (path or os.environ.get("ARGENTOR_PATH") or DEFAULT_PATH)
    r = S.get(url, headers={"TokenID": _token()}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _quotes(raw) -> dict[str, dict[str, float]]:
    """Flatten to {'XAUUSD': {'bid': x, 'ask': y}, ...} whatever the nesting."""
    found: dict[str, dict[str, float]] = {}

    def visit(node):
        if isinstance(node, dict):
            low = {str(k).strip().upper(): v for k, v in node.items()}
            pair = low.get("PAIR") or low.get("SYMBOL") or low.get("NAME")
            bid, ask = low.get("BID"), low.get("ASK")
            if pair and (bid is not None or ask is not None):
                try:
                    entry = {}
                    if bid is not None:
                        entry["bid"] = float(bid)
                    if ask is not None:
                        entry["ask"] = float(ask)
                    found[re.sub(r"[^A-Z0-9]", "", str(pair).upper())] = entry
                except (TypeError, ValueError):
                    pass
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(raw)
    return found


def rates() -> dict:
    if _CACHE["value"] is not None and (time.time() - _CACHE["ts"]) < CACHE_TTL:
        return _CACHE["value"]

    q = _quotes(fetch_raw())
    if not q:
        raise ValueError("no PAIR/BID/ASK rows found in payload")

    def mid(pair: str) -> float | None:
        e = q.get(pair)
        if not e:
            return None
        if "bid" in e and "ask" in e:
            return (e["bid"] + e["ask"]) / 2
        return e.get("bid") or e.get("ask")

    eurusd = mid("EURUSD")
    out: dict = {}

    for metal, usd_pair, eur_pair in (("XAU", "XAUUSD", "XAUEUR"),
                                      ("XAG", "XAGUSD", "XAGEUR"),
                                      ("XPT", "XPTUSD", "XPTEUR"),
                                      ("XPD", "XPDUSD", "XPDEUR")):
        usd = mid(usd_pair)
        if usd is None and (eur := mid(eur_pair)) and eurusd:
            usd = eur * eurusd                              # derive if USD pair absent
        if usd is None:
            continue
        lo, hi = SANITY[metal]
        if not (lo <= usd <= hi):
            print(f"[warn] {usd_pair} {usd} outside sanity band, skipped")
            continue
        e = q.get(usd_pair, {})
        out[metal] = {"usd_oz": round(usd, 3),
                      "bid": e.get("bid"), "ask": e.get("ask"),
                      "eur_oz": round(mid(eur_pair), 3) if mid(eur_pair) else None,
                      "source": "argentor"}

    if not out:
        raise ValueError(f"no usable metal pairs; saw {sorted(q)[:12]}")

    out["extras"] = {
        "eurusd": round(eurusd, 5) if eurusd else None,
        "xau_eur_gram": mid("XAUEURGMS"),
        "xag_eur_gram": mid("XAGEURGMS"),
        "xau_eur_kg_4n9": mid("XAUEURKG4X9"),
        "xag_eur_kg_4n9": mid("XAGEURKG4X9"),
        "pairs_seen": len(q),
    }

    # internal consistency: XAUEUR * EURUSD should equal XAUUSD
    for metal, eur_pair in (("XAU", "XAUEUR"), ("XAG", "XAGEUR")):
        e, u = mid(eur_pair), (out.get(metal) or {}).get("usd_oz")
        if e and u and eurusd:
            drift = abs(e * eurusd / u - 1)
            if drift > 0.005:
                print(f"[warn] {metal} EUR/USD cross drift {drift:.2%}")

    _CACHE.update({"ts": time.time(), "value": out})
    print("[info] argentor " + " | ".join(
        f"{m} {d['usd_oz']:.2f} USD/oz" for m, d in out.items() if m != "extras")
        + f" | EURUSD {out['extras']['eurusd']}")
    return out


if __name__ == "__main__":
    raw = fetch_raw()
    print("=" * 70, "\nPAIRS SEEN\n", "=" * 70)
    for pair, e in sorted(_quotes(raw).items()):
        tag = " ".join(str(x) for x in PAIRS.get(pair, ("?",)))
        print(f"  {pair:<14} bid {e.get('bid', '-'):>12} ask {e.get('ask', '-'):>12}   {tag}")
    print("=" * 70, "\nPARSED\n", "=" * 70)
    print(json.dumps(rates(), indent=2))
