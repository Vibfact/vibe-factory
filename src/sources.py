"""src/sources.py  (patched 2026-09-14)

Changes vs first build:
  * Stooq removed as primary: its CSV endpoint now demands a captcha-issued apikey.
    Primary price source is chartgoldprice.com (no key, no rate limit, XAU+XAG
    daily closes back to 2000; free with attribution). Fallbacks: api.gold-api.com
    (live only, unlimited free) and Stooq if you set STOOQ_APIKEY.
  * Local price history in state/prices.csv so percentiles survive a dead API.
  * Federal Register: literal (unencoded) bracket query string - the percent-encoded
    form returns HTTP 400.
  * FRED release calendar: longer timeout, smaller page.

Attribution: gold and silver closes courtesy of https://www.chartgoldprice.com
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import pathlib
import re

import feedparser
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

UA = {"User-Agent": "vibe-factory/1.1 (github actions; personal research)"}
TIMEOUT = 8
SLOW_TIMEOUT = 25
PRICE_CSV = pathlib.Path("state/prices.csv")
VADER = SentimentIntensityAnalyzer()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=1, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))))
    return s


S = _session()


def _get(url, timeout=TIMEOUT, **kw):
    r = S.get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r


# ==========================================================================
# 1. PRICES
# ==========================================================================
CHART_API = "https://www.chartgoldprice.com/api/data"
GOLD_API = "https://api.gold-api.com/price"


def _walk_series(node, out):
    """Find every list of {date, close|price} pairs anywhere in a JSON blob."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                keys = {kk.lower() for kk in v[0]}
                if "date" in keys and ({"close", "price", "value"} & keys):
                    out.setdefault(k.lower(), v)
            _walk_series(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_series(v, out)
    return out


def _series_to_frame(rows) -> pd.DataFrame:
    recs = []
    for r in rows:
        low = {k.lower(): v for k, v in r.items()}
        val = low.get("close", low.get("price", low.get("value")))
        if val in (None, ""):
            continue
        try:
            recs.append((pd.to_datetime(low["date"]), float(val)))
        except (ValueError, TypeError):
            continue
    df = pd.DataFrame(recs, columns=["Date", "Close"]).dropna()
    return df.drop_duplicates("Date").set_index("Date").sort_index()


def chartgoldprice_history() -> dict[str, pd.DataFrame]:
    j = _get(CHART_API, params={"history": "both"}, timeout=SLOW_TIMEOUT).json()
    found = _walk_series(j, {})
    out = {}
    for metal, needle in (("XAU", "gold"), ("XAG", "silver")):
        key = next((k for k in found if needle in k), None)
        if key:
            df = _series_to_frame(found[key])
            if len(df) > 250:
                out[metal] = df
    if not out:
        raise ValueError(f"no usable history in chartgoldprice payload: {list(found)[:6]}")
    return out


def gold_api_live() -> dict[str, float]:
    out = {}
    for metal, sym in (("XAU", "XAU"), ("XAG", "XAG")):
        j = _get(f"{GOLD_API}/{sym}").json()
        px = j.get("price") or j.get("Price")
        if px:
            out[metal] = float(px)
    if not out:
        raise ValueError("gold-api returned no prices")
    return out


def stooq_history(symbol: str) -> pd.DataFrame:
    """Only usable if you hold a captcha-issued apikey (env STOOQ_APIKEY)."""
    key = os.environ.get("STOOQ_APIKEY")
    if not key:
        raise RuntimeError("STOOQ_APIKEY not set")
    txt = _get(f"https://stooq.com/q/d/l/?s={symbol}&i=d&apikey={key}",
               timeout=SLOW_TIMEOUT).text
    if not txt.lower().startswith("date"):
        raise ValueError(f"stooq non-CSV: {txt[:100]}")
    return pd.read_csv(io.StringIO(txt), parse_dates=["Date"]).set_index("Date").sort_index()


def _load_local() -> dict[str, pd.DataFrame]:
    if not PRICE_CSV.exists():
        return {}
    df = pd.read_csv(PRICE_CSV, parse_dates=["Date"])
    out = {}
    for metal in ("XAU", "XAG"):
        if metal in df.columns:
            sub = df[["Date", metal]].dropna().rename(columns={metal: "Close"})
            out[metal] = sub.drop_duplicates("Date").set_index("Date").sort_index()
    return out


def _save_local(hist: dict[str, pd.DataFrame]) -> None:
    if not hist:
        return
    PRICE_CSV.parent.mkdir(parents=True, exist_ok=True)
    merged = None
    for metal, df in hist.items():
        col = df["Close"].rename(metal)
        merged = col.to_frame() if merged is None else merged.join(col, how="outer")
    merged = merged.tail(3000)
    merged.index.name = "Date"
    merged.to_csv(PRICE_CSV)


def price_history() -> dict[str, pd.DataFrame]:
    hist, errors = {}, []
    for fn in (chartgoldprice_history,
               lambda: {"XAU": stooq_history("xauusd"), "XAG": stooq_history("xagusd")}):
        try:
            hist = fn()
            break
        except Exception as exc:                              # noqa: BLE001
            errors.append(f"{fn}: {type(exc).__name__}: {exc}")

    local = _load_local()
    if not hist:
        hist = local
        try:                                                  # patch today's close on
            live = gold_api_live()
            today = pd.Timestamp(dt.date.today())
            for metal, px in live.items():
                base = hist.get(metal, pd.DataFrame(columns=["Close"]))
                base.loc[today, "Close"] = px
                hist[metal] = base.sort_index()
        except Exception as exc:                              # noqa: BLE001
            errors.append(f"gold_api_live: {exc}")
    else:
        for metal, df in local.items():                       # keep longest history
            if metal in hist and len(df) > len(hist[metal]):
                hist[metal] = df.combine_first(hist[metal]).sort_index()

    for metal in ("XAU", "XAG"):
        if metal not in hist or len(hist[metal]) < 250:
            raise ValueError(f"insufficient {metal} history; tried: {' | '.join(errors)[:300]}")
    _save_local(hist)
    return hist


def price_regime(df: pd.DataFrame) -> dict:
    c = df["Close"].astype(float).dropna()
    ret = c.pct_change()
    return {
        "px": float(c.iloc[-1]),
        "asof": str(c.index[-1].date()),
        "mom_20": float(c.iloc[-1] / c.iloc[-21] - 1),
        "mom_60": float(c.iloc[-1] / c.iloc[-61] - 1),
        "dist_ma200": float(c.iloc[-1] / c.rolling(200).mean().iloc[-1] - 1),
        "rv20_ann": float(ret.tail(20).std() * 252 ** 0.5),
        "rv_ratio": float(ret.tail(20).std() / max(ret.tail(120).std(), 1e-9)),
        "dd_1y": float(c.iloc[-1] / c.tail(252).max() - 1),
        "ret_5d": float(c.iloc[-1] / c.iloc[-6] - 1),
        "hist_mom20": [float(v) for v in (c / c.shift(20) - 1).dropna().tail(504)],
    }


def metals() -> dict:
    hist = price_history()
    out = {m: price_regime(df) for m, df in hist.items()}
    ratio = (hist["XAU"]["Close"].astype(float) /
             hist["XAG"]["Close"].astype(float)).dropna()
    out["ratio"] = {"level": float(ratio.iloc[-1]),
                    "hist": [float(v) for v in ratio.tail(504)]}
    return out


# ==========================================================================
# 2. FRED
# ==========================================================================
FRED = "https://api.stlouisfed.org/fred"
FRED_SERIES = {"real10y": "DFII10", "usd": "DTWEXBGS",
               "breakeven": "T10YIE", "effr": "EFFR"}


def fred_series(series_id: str, years: int = 3) -> list[tuple[str, float]]:
    key = os.environ["FRED_API_KEY"]
    start = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    j = _get(f"{FRED}/series/observations", timeout=SLOW_TIMEOUT, params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "observation_start": start}).json()
    return [(o["date"], float(o["value"])) for o in j["observations"] if o["value"] != "."]


def macro() -> dict:
    out = {}
    for name, sid in FRED_SERIES.items():
        vals = [v for _, v in fred_series(sid)]
        if not vals:
            continue
        out[name] = {"level": vals[-1],
                     "chg_5d": vals[-1] - vals[-6] if len(vals) > 6 else 0.0,
                     "hist": vals[-504:]}
    return out


TIER1 = re.compile(r"consumer price index|employment situation|personal income and outlays|"
                   r"producer price|gross domestic product|fomc", re.I)


def fred_releases(days: int = 14) -> list[dict]:
    key = os.environ["FRED_API_KEY"]
    today = dt.date.today()
    j = _get(f"{FRED}/releases/dates", timeout=SLOW_TIMEOUT, params={
        "api_key": key, "file_type": "json",
        "realtime_start": today.isoformat(),
        "realtime_end": (today + dt.timedelta(days=days)).isoformat(),
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc", "limit": 400}).json()
    out = []
    for r in j.get("release_dates", []):
        d, name = r["date"], r.get("release_name", "")
        if d < today.isoformat():
            continue
        out.append({"date": d, "name": name, "tier1": bool(TIER1.search(name)),
                    "days_out": (dt.date.fromisoformat(d) - today).days})
    return sorted(out, key=lambda x: x["date"])


# ==========================================================================
# 3. FED EVENT CLOCK
# ==========================================================================
FED_FEEDS = {
    "speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
    "press": "https://www.federalreserve.gov/feeds/press_all.xml",
    "calendar": "https://www.federalreserve.gov/feeds/calendar.xml",
}
CHAIR = re.compile(r"powell|chair\b", re.I)
HAWK = re.compile(r"restrictive|tighten|vigilan|persistent inflation|higher for longer|"
                  r"upside risk to inflation", re.I)
DOVE = re.compile(r"accommodat|cut|easing|softening labor|downside risk to employment|"
                  r"disinflation", re.I)


def fed_feed(days_back: int = 5) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    items, hawk, dove = [], 0, 0
    for kind, url in FED_FEEDS.items():
        try:
            d = feedparser.parse(url)
        except Exception:                                     # noqa: BLE001
            continue
        for e in d.entries[:40]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if not ts:
                continue
            when = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc)
            age_h = (now - when).total_seconds() / 3600
            if age_h > days_back * 24:
                continue
            text = f"{e.get('title', '')} {e.get('summary', '')}"
            hawk += len(HAWK.findall(text))
            dove += len(DOVE.findall(text))
            items.append({"kind": kind, "title": e.get("title", "")[:120],
                          "age_h": round(age_h, 1), "chair": bool(CHAIR.search(text))})
    return {"items": items[:40], "speech_tone": (dove - hawk) / max(dove + hawk, 1),
            "hawk_hits": hawk, "dove_hits": dove}


def fomc_dates() -> list[str]:
    html = _get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                timeout=SLOW_TIMEOUT).text
    year = dt.date.today().year
    months = {m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"], 1)}
    found = set()
    for mon, day in re.findall(
            r"(January|February|March|April|May|June|July|August|September|October|"
            r"November|December)\s*(\d{1,2})", html):
        try:
            found.add(dt.date(year, months[mon.lower()], int(day)).isoformat())
        except ValueError:
            continue
    return sorted(found)


def next_event(dates: list[str]) -> int | None:
    today = dt.date.today()
    fut = [d for d in dates if d >= today.isoformat()]
    return (dt.date.fromisoformat(fut[0]) - today).days if fut else None


# ==========================================================================
# 4. KALSHI
# ==========================================================================
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_KEYWORDS = {
    "fed": ("FED", "FOMC", "INTEREST RATE", "RATE HIKE", "RATE CUT"),
    "cpi": ("CPI", "INFLATION"),
    "shutdown": ("SHUTDOWN",),
    "tariff": ("TARIFF",),
}


def _kalshi_price(m: dict) -> float | None:
    bid, ask = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
    try:
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2
    except (TypeError, ValueError):
        pass
    bid_c, ask_c = m.get("yes_bid"), m.get("yes_ask")
    if bid_c is None or ask_c is None:
        return None
    return (float(bid_c) + float(ask_c)) / 200


def kalshi_topics(max_pages: int = 3) -> dict:
    markets, cursor = [], None
    for _ in range(max_pages):
        p = {"status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        j = _get(f"{KALSHI}/markets", timeout=12, params=p).json()
        markets.extend(j.get("markets", []))
        cursor = j.get("cursor")
        if not cursor:
            break
    topics: dict[str, list[dict]] = {k: [] for k in KALSHI_KEYWORDS}
    for m in markets:
        blob = f"{m.get('title', '')} {m.get('ticker', '')}".upper()
        p = _kalshi_price(m)
        if p is None:
            continue
        for topic, kws in KALSHI_KEYWORDS.items():
            if any(k in blob for k in kws):
                topics[topic].append({"ticker": m.get("ticker"), "title": m.get("title"),
                                      "p": round(p, 4),
                                      "volume": float(m.get("volume_fp")
                                                      or m.get("volume") or 0)})
    return {t: sorted(v, key=lambda x: -x["volume"])[:8] for t, v in topics.items()}


def hike_pressure_from(topics: dict) -> float:
    hike = cut = 0.0
    for m in topics.get("fed", []):
        t = (m.get("title") or "").lower()
        if any(k in t for k in ("hike", "increase", "raise")):
            hike = max(hike, m["p"])
        if any(k in t for k in ("cut", "decrease", "lower")):
            cut = max(cut, m["p"])
    return round(hike - cut, 4)


# ==========================================================================
# 5. POLYMARKET
# ==========================================================================
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PM_KEYWORDS = ("fed", "fomc", "interest rate", "rate hike", "rate cut", "powell",
               "tariff", "recession", "cpi", "inflation")


def polymarket_odds(max_pages: int = 3) -> list[dict]:
    matches, cursor = [], None
    for _ in range(max_pages):
        p = {"closed": "false", "limit": 100}
        if cursor:
            p["after_cursor"] = cursor
        j = _get(f"{GAMMA}/markets/keyset", timeout=12, params=p).json()
        for m in j.get("markets", []):
            if any(k in (m.get("question") or "").lower() for k in PM_KEYWORDS):
                matches.append(m)
        cursor = j.get("next_cursor")
        if not cursor:
            break

    token_of = {}
    for m in matches[:25]:
        ids = m.get("clobTokenIds")
        if not ids:
            try:
                ids = _get(f"{GAMMA}/markets/{m['id']}").json().get("clobTokenIds")
            except requests.RequestException:
                continue
        try:
            token_of[json.loads(ids)[0]] = m.get("question")
        except (TypeError, ValueError, IndexError):
            continue
    if not token_of:
        return []
    r = S.post(f"{CLOB}/midpoints", json=[{"token_id": t} for t in token_of], timeout=TIMEOUT)
    r.raise_for_status()
    mids = r.json()
    return [{"question": q, "p": round(float(mids[t]), 4)}
            for t, q in token_of.items() if mids.get(t) is not None]


# ==========================================================================
# 6. POLITICAL
# ==========================================================================
TRUMP_FEED = "https://www.trumpstruth.org/feed"
TOPICS = {
    "tariff": (r"tariff|trade deal|trade war|reciprocal|section 232|import tax", +1, 0.65),
    "fed_attack": (r"powell|too late|fed should|rates are too|federal reserve", +1, 0.60),
    "dollar": (r"strong dollar|weak dollar|devalu|dollar dominance", +1, 0.35),
    "gold": (r"\bgold\b|bullion|fort knox", +1, 0.30),
    "geopol": (r"iran|russia|ukraine|taiwan|venezuela|strike|missile|military", +1, 0.55),
    "dealmaking": (r"great deal|agreement signed|historic deal|peace deal|ceasefire", -1, 0.45),
}
CAP_PER_TOPIC = 1.0


def trump_pressure(hours: int = 48) -> list[dict]:
    d = feedparser.parse(TRUMP_FEED)
    now = dt.datetime.now(dt.timezone.utc)
    hits, budget = [], {k: 0.0 for k in TOPICS}
    for e in d.entries[:120]:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if not ts:
            continue
        when = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc)
        age = (now - when).total_seconds() / 60
        if age > hours * 60 or age < -60:
            continue
        txt = re.sub(r"<[^>]+>", " ", f"{e.get('title', '')} {e.get('summary', '')}")
        low = txt.lower()
        caps = sum(1 for w in txt.split() if w.isupper() and len(w) > 3)
        for topic, (rx, sign, mag) in TOPICS.items():
            if not re.search(rx, low):
                continue
            intensity = (0.5 + 0.5 * abs(VADER.polarity_scores(txt)["compound"])) * \
                        (1 + min(caps, 10) / 20)
            impact = sign * mag * intensity
            if budget[topic] + abs(impact) > CAP_PER_TOPIC:
                continue
            budget[topic] += abs(impact)
            hits.append({"topic": topic, "impact": round(impact, 3),
                         "age_min": round(age, 1), "hl": 180,
                         "text": txt.strip()[:160]})
    return hits


FR_HOT = re.compile(r"tariff|section 232|section 301|duties|import|sanction|export control", re.I)


def federal_register(days: int = 5) -> list[dict]:
    """Brackets must stay literal: percent-encoded conditions[...] returns HTTP 400."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    fields = ["title", "signing_date", "publication_date",
              "presidential_document_type", "html_url"]
    url = ("https://www.federalregister.gov/api/v1/documents.json"
           f"?per_page=40&order=newest&conditions[type][]=PRESDOCU"
           f"&conditions[publication_date][gte]={since}"
           + "".join(f"&fields[]={f}" for f in fields))
    j = _get(url, timeout=12).json()
    out = []
    for r in j.get("results", []):
        title = r.get("title") or ""
        out.append({"title": title[:160], "type": r.get("presidential_document_type"),
                    "date": r.get("signing_date") or r.get("publication_date"),
                    "hot": bool(FR_HOT.search(title)), "url": r.get("html_url")})
    return out


# ==========================================================================
# 7. GDELT
# ==========================================================================
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERIES = {
    "gold": '(gold price OR bullion OR "safe haven") sourcelang:english',
    "silver": '(silver price OR "silver squeeze") sourcelang:english',
    "fed": '("federal reserve" OR "rate hike" OR "rate cut") sourcelang:english',
    "tariff": '(tariff OR "trade war") sourcelang:english',
}


def gdelt_tone(timespan: str = "3d") -> dict:
    out = {}
    for name, q in GDELT_QUERIES.items():
        try:
            j = _get(GDELT, timeout=12, params={"query": q, "mode": "timelinetone",
                                                "timespan": timespan,
                                                "format": "json"}).json()
            data = j["timeline"][0]["data"]
            out[name] = round(sum(d["value"] for d in data) / max(len(data), 1), 4)
        except (requests.RequestException, KeyError, IndexError, ValueError):
            out[name] = None
    return out


# Override the built-in federal_register with the self-healing version.
from src.fedreg import federal_register  # noqa: E402,F401


from src.patch2 import (fomc_dates, fred_releases, hike_pressure_from,
                        federal_register)  # noqa: E402,F401


from src.patch3 import hike_pressure_from, polymarket_odds  # noqa: E402,F401


from src.patch4 import hike_pressure_from, gdelt_tone  # noqa: E402,F401


from src.patch5 import gdelt_tone, fred_releases  # noqa: E402,F401


from src import patch6  # noqa: E402,F401  (fills cot_crowding, patches blend)


from src import patch7  # noqa: E402,F401  (macro surprise, inflation, geo, typed skew)


from src import patch8  # noqa: E402,F401  (stress tickers, component log, health)


from src.patch9 import federal_register, trump_pressure  # noqa: E402,F401


from src import patch10  # noqa: E402,F401  (live broker price)
