"""src/run.py  (v3 — rich Telegram output)

Changes vs v2:
  * Telegram messages are built by src/fmt.py and sent with parse_mode=HTML, so
    they carry status emoji, bold figures, a WHY line naming the top three
    movers, and the COT and tone lines that previously only hit the console.
  * If fmt.py is missing or raises, it silently falls back to the plain-text
    renderer and sends without parse_mode. Formatting must never break a run.
  * Console output stays plain text — GitHub Actions logs don't render HTML.

    python -m src.run --track pressure --budget-seconds 42
    python -m src.run --track regime --append state/history.csv
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import pathlib
import re
import time

import requests

from src import schema, score, sources

STATE = pathlib.Path("state")
CACHE_PATH = STATE / "cache.json"
ALERTS_PATH = STATE / "alert_log.json"
INTRADAY = STATE / "intraday.jsonl"

TTL = {"metals": 900, "macro": 21_600, "releases": 21_600, "fomc": 86_400,
       "fed_feed": 1_800, "kalshi": 300, "polymarket": 300, "trump": 300,
       "fedreg": 3_600, "gdelt": 3_600}
PRESSURE_SOURCES = ("metals", "kalshi", "polymarket", "trump",
                    "macro", "releases", "fomc", "fedreg", "fed_feed")
ALL_SOURCES = tuple(TTL)

FETCH = {
    "metals": sources.metals,
    "macro": sources.macro,
    "releases": sources.fred_releases,
    "fomc": sources.fomc_dates,
    "fed_feed": sources.fed_feed,
    "kalshi": sources.kalshi_topics,
    "polymarket": sources.polymarket_odds,
    "trump": sources.trump_pressure,
    "fedreg": sources.federal_register,
    "gdelt": sources.gdelt_tone,
}


class Cache:
    def __init__(self, path: pathlib.Path = CACHE_PATH):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except ValueError:
                self.data = {}

    def stale(self, key: str, ttl: int) -> bool:
        e = self.data.get(key)
        return not e or (time.time() - e.get("ts", 0)) > ttl

    def get(self, key: str):
        e = self.data.get(key)
        return e.get("value") if e else None

    def put(self, key: str, value) -> None:
        self.data[key] = {"ts": time.time(), "value": value}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))


def collect(cache: Cache, wanted: tuple[str, ...], budget_s: float) -> tuple[dict, list[str]]:
    todo = [k for k in wanted if cache.stale(k, TTL[k])]
    failed: list[str] = []
    if todo:
        with cf.ThreadPoolExecutor(max_workers=len(todo)) as ex:
            futures = {ex.submit(FETCH[k]): k for k in todo}
            try:
                for fut in cf.as_completed(futures, timeout=max(budget_s, 1)):
                    k = futures[fut]
                    try:
                        cache.put(k, fut.result())
                    except Exception as exc:              # noqa: BLE001
                        failed.append(k)
                        print(f"[warn] {k}: {type(exc).__name__}: {exc}")
            except TimeoutError:
                for fut, k in futures.items():
                    if not fut.done():
                        fut.cancel()
                        failed.append(k)
                        print(f"[warn] {k}: timed out after {budget_s:.0f}s (budget)")
            ex.shutdown(wait=False, cancel_futures=True)

    data = {k: cache.get(k) for k in wanted}
    for k, v in data.items():
        if v is None and k not in failed:
            failed.append(k)
    return data, failed


def build_components(data: dict, metal: str) -> tuple[dict, float, float, dict]:
    macro, met = data.get("macro") or {}, data.get("metals") or {}
    kalshi, gdelt = data.get("kalshi") or {}, data.get("gdelt") or {}
    m = met.get(metal) or {}

    hike = sources.hike_pressure_from(kalshi) if kalshi is not None else None
    comp = {
        "real_rate": score.rank((macro.get("real10y") or {}).get("hist"),
                                (macro.get("real10y") or {}).get("level")),
        "usd": score.rank((macro.get("usd") or {}).get("hist"),
                          (macro.get("usd") or {}).get("level")),
        "hike_pressure": hike,
        "cot_crowding": None,
        "momentum": score.rank(m.get("hist_mom20"), m.get("mom_20")),
        "gsr": score.rank((met.get("ratio") or {}).get("hist"),
                          (met.get("ratio") or {}).get("level")) if metal == "XAG" else None,
        "media_tone": None,
    }
    tone = gdelt.get("gold" if metal == "XAU" else "silver")
    if tone is not None:
        comp["media_tone"] = max(-1.0, min(1.0, tone / 5.0))

    events = list(data.get("trump") or [])
    press = score.pressure(events)

    releases = data.get("releases") or []
    tier1 = [r for r in releases if r.get("tier1")]
    days_out = min([r["days_out"] for r in tier1], default=None)
    fomc_days = sources.next_event(data.get("fomc") or [])
    if fomc_days is not None:
        days_out = fomc_days if days_out is None else min(days_out, fomc_days)
    skew = score.event_skew(days_out, hike or 0.0)

    clock = {"next_tier1_days": days_out, "next_fomc_days": fomc_days,
             "upcoming": [f"{r['name'][:38]} {r['days_out']}d" for r in tier1[:4]]}
    return comp, press, skew, {"clock": clock, "events": events, "hike": hike}


def render(results: list[dict], meta: dict, data: dict, prev: dict) -> str:
    """Plain-text fallback, also used for console output."""
    lines = []
    for r in results:
        p = prev.get(r["metal"], {}).get("vibe")
        delta = "" if p is None else f"  {'^' if r['vibe'] > p else 'v'}{abs(r['vibe'] - p):.0f}"
        lines.append(f"{r['metal']}  {r['vibe']:+.0f}  {r['label']}{delta}   conf {r['confidence']}")
    lines.append(f"regime {results[0]['regime']:+.0f} | pressure {results[0]['pressure']:+.0f}")
    if meta.get("hike") is not None:
        lines.append(f"PRICED  hawkish lean {meta['hike']:+.2f}")
    c = meta["clock"]
    if c["upcoming"]:
        lines.append("CLOCK   " + " | ".join(c["upcoming"]))
    if c["next_fomc_days"] is not None:
        lines.append(f"FOMC    in {c['next_fomc_days']}d")
    for e in [e for e in meta["events"] if abs(e["impact"]) > 0.25][:3]:
        lines.append(f"POLITIC {e['topic']} {e['impact']:+.2f} ({e['age_min']:.0f}m) "
                     f"{e['text'][:70]}")
    for d in [d for d in (data.get("fedreg") or []) if d.get("hot")][:2]:
        lines.append(f"POLICY  {d['title'][:80]}")
    met = (data.get("metals") or {}).get("XAU") or {}
    xag = ((data.get("metals") or {}).get("XAG") or {}).get("px")
    if met.get("px") and xag:
        lines.append(f"PX      XAU {met['px']:.2f} | XAG {xag:.2f} (asof {met.get('asof')})")
    if results[0]["missing"]:
        lines.append("STALE   " + ",".join(results[0]["missing"]))
    return "\n".join(lines)


def send(text: str, parse_mode: str | None = None) -> None:
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if os.environ.get("DRY_RUN", "").lower() == "true":
        print("[dry-run]\n" + text)
        return
    if not token or not chat:
        print("[info] no telegram secrets; printing only\n" + text)
        return
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json=payload, timeout=10)
    if not r.ok and parse_mode:
        # Bad HTML must never cost you the alert: retry once as plain text.
        print(f"[warn] telegram {r.status_code} with {parse_mode}: {r.text[:160]}")
        plain = re.sub(r"<[^>]+>", "", text)
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": plain}, timeout=10)
    if not r.ok:
        print(f"[warn] telegram {r.status_code}: {r.text[:200]}")


def should_alert(results: list[dict], prev: dict, log: dict, force: bool) -> bool:
    if force:
        return True
    if time.time() - log.get("last_ts", 0) < 1200:
        return False
    if log.get("count_today", 0) >= 12:
        return False
    for r in results:
        p = prev.get(r["metal"], {})
        if not p or p.get("label") != r["label"] or abs(r["vibe"] - p.get("vibe", 0)) >= 12:
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", choices=("pressure", "regime"), default="pressure")
    ap.add_argument("--budget-seconds", type=float, default=42.0)
    ap.add_argument("--append", default=None)
    args = ap.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    cache = Cache()
    wanted = ALL_SOURCES if args.track == "regime" else PRESSURE_SOURCES
    budget = args.budget_seconds if args.track == "pressure" else max(args.budget_seconds, 120)
    data, failed = collect(cache, wanted, budget)

    results, meta = [], {}
    for metal in ("XAU", "XAG"):
        comp, press, skew, meta = build_components(data, metal)
        results.append(score.blend(comp, press, skew, metal))

    log = {}
    if ALERTS_PATH.exists():
        try:
            log = json.loads(ALERTS_PATH.read_text())
        except ValueError:
            log = {}
    prev = log.get("last_results", {})
    today = dt.date.today().isoformat()
    if log.get("day") != today:
        log = {"day": today, "count_today": 0, "last_results": prev}

    plain = render(results, meta, data, prev)
    print(plain)

    if should_alert(results, prev, log, force=(args.track == "regime")):
        try:
            from src import fmt
            send(fmt.build(results, meta, data, prev), parse_mode="HTML")
        except Exception as exc:                               # noqa: BLE001
            print(f"[warn] rich format failed ({type(exc).__name__}: {exc}); plain fallback")
            send(plain)
        log["last_ts"] = time.time()
        log["count_today"] = log.get("count_today", 0) + 1
    else:
        print("[info] no alert trigger")

    log["last_results"] = {r["metal"]: {"vibe": r["vibe"], "label": r["label"]} for r in results}
    ALERTS_PATH.write_text(json.dumps(log))

    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with INTRADAY.open("a") as f:
        f.write(json.dumps({"ts": stamp, "results": results, "clock": meta["clock"],
                            "hike": meta.get("hike"), "failed": failed}) + "\n")
    cache.put("last_run_utc", stamp)
    cache.save()

    if args.append:
        schema.append_row(args.append, stamp, results, data, meta)
        print(f"[info] appended to {args.append}")

    if failed:
        print(f"[info] degraded sources: {', '.join(sorted(set(failed)))}")


if __name__ == "__main__":
    main()
