"""src/patch10.py — use the live broker feed for the price line and intraday
momentum, while leaving the historical series untouched.

Design rule that matters: the daily-close series from chartgoldprice is what all
percentile components are ranked against (momentum, gold/silver ratio). Broker
quotes include spreads and premiums, so mixing them into that history would
corrupt three years of consistent closes. Therefore:

  * broker feed  -> displayed price, today's momentum, gold/silver ratio
  * daily closes -> the 504-observation history the percentiles rank against
  * broker quotes are logged separately to state/broker_prices.csv, which is
    also a free record of your own premium over spot

Falls back silently: argentor -> gold-api -> daily close. Import AFTER patch9.
"""
from __future__ import annotations

import csv
import datetime as dt
import pathlib

from src import sources as _s

BROKER_CSV = pathlib.Path("state/broker_prices.csv")
_orig_metals = _s.metals


def _log(rows: dict, spot: dict) -> None:
    BROKER_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not BROKER_CSV.exists()
    row = {"ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    for m in ("XAU", "XAG"):
        d = rows.get(m) or {}
        row[f"{m.lower()}_mid"] = d.get("usd_oz", "")
        row[f"{m.lower()}_bid"] = d.get("bid", "")
        row[f"{m.lower()}_ask"] = d.get("ask", "")
        close = (spot.get(m) or {}).get("px")
        row[f"{m.lower()}_close"] = close or ""
        row[f"{m.lower()}_prem_pct"] = (
            round((d["usd_oz"] / close - 1) * 100, 4)
            if d.get("usd_oz") and close else "")
    try:
        with BROKER_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError as exc:
        print(f"[warn] broker log failed: {exc}")


def _live() -> dict:
    try:
        from src import argentor
        return argentor.rates()
    except Exception as exc:                                   # noqa: BLE001
        print(f"[info] argentor unavailable ({type(exc).__name__}: {exc}); trying gold-api")
    try:
        live = _s.gold_api_live()
        return {m: {"usd_oz": px, "bid": None, "ask": None, "source": "gold-api"}
                for m, px in live.items()}
    except Exception as exc:                                   # noqa: BLE001
        print(f"[info] gold-api unavailable ({exc}); using daily close")
        return {}


def metals() -> dict:
    out = _orig_metals()                       # daily closes + full history
    live = _live()
    if not live:
        return out

    for metal in ("XAU", "XAG"):
        d, sub = live.get(metal), out.get(metal)
        if not d or not sub or not d.get("usd_oz"):
            continue
        close = sub.get("px")
        px = d["usd_oz"]
        sub["px_close"] = close
        sub["px"] = px
        sub["px_source"] = d.get("source")
        sub["bid"], sub["ask"] = d.get("bid"), d.get("ask")
        sub["asof"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if close:
            sub["chg_vs_close"] = round((px / close - 1) * 100, 3)
        # re-anchor the fast price stats on the live print; hist_mom20 is the
        # ranking history and deliberately stays on daily closes
        if sub.get("mom_20") is not None and close:
            scale = px / close
            sub["mom_20"] = round((1 + sub["mom_20"]) * scale - 1, 6)
            if sub.get("ret_5d") is not None:
                sub["ret_5d"] = round((1 + sub["ret_5d"]) * scale - 1, 6)
            if sub.get("dd_1y") is not None:
                sub["dd_1y"] = round(min(0.0, (1 + sub["dd_1y"]) * scale - 1), 6)

    if out.get("XAU", {}).get("px") and out.get("XAG", {}).get("px"):
        ratio = out["XAU"]["px"] / out["XAG"]["px"]
        out.setdefault("ratio", {})["level"] = round(ratio, 4)

    _log(live, {m: {"px": (out.get(m) or {}).get("px_close")} for m in ("XAU", "XAG")})
    return out


_s.metals = metals
print("[info] patch10 active: live broker price for display and intraday stats")
