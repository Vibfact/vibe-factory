"""src/sources.py — every data feed. All free. Each function is independent and
raises on failure; the orchestrator isolates failures per source."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re

import feedparser
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

UA = {"User-Agent": "vibe-factory/1.0 (+github actions; contact via repo issues)"}
TIMEOUT = 8
VADER = SentimentIntensityAnalyzer()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=1, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))))
    return s


S = _session()


def _get(url, **kw):
    r = S.get(url, timeout=kw.pop("timeout", TIMEOUT), **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# 1. PRICES — Stooq daily CSV, keyless
# --------------------------------------------------------------------------
def stooq(symbol: str = "xauusd") -> pd.DataFrame:
    txt = _get(f"https://stooq.com/q/d/l/?s={symbol}&i=d", timeout=15).text
    if "Date" not in txt.split("\n")[0]:
        raise ValueError(f"stooq returned non-CSV for {symbol}: {txt[:120]}")
    df = pd.read_csv(io.StringIO(txt), parse_dates=["Date"]).set_index("Date").sort_index()
    return df


def price_regime(df: pd.DataFrame) -> dict:
    c = df["Close"].astype(float)
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
    }


def metals() -> dict:
    xau, xag = stooq("xauusd"), stooq("xagusd")
    out = {"XAU": price_regime(xau), "XAG": price_regime(xag)}
    ratio = (xau["Close"].astype(float) / xag["Close"].astype(float)).dropna()
    out["ratio"] = {"level": float(ratio.iloc[-1]),
                    "hist": [float(v) for v in ratio.tail(504)]}
    out["XAU"]["hist_mom20"] = _hist_mom(xau, 20)
    out["XAG"]["hist_mom20"] = _hist_mom(xag, 20)
    return out


def _hist_mom(df: pd.DataFrame, n: int, win: int = 504) -> list[float]:
    c = df["Close"].astype(float)
    return [float(v) for v in (c / c.shift(n) - 1).dropna().tail(win)]


# --------------------------------------------------------------------------
# 2. FRED — free key, 120 req/min
# --------------------------------------------------------------------------
FRED = "https://api.stlouisfed.org/fred"
FRED_SERIES = {"real10y": "DFII10", "usd": "DTWEXBGS",
               "breakeven": "T10YIE", "effr": "EFFR"}


def fred_series(series_id: str, years: int = 3) -> list[tuple[str, float]]:
    key = os.environ["FRED_API_KEY"]
    start = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    j = _get(f"{FRED}/series/observations", params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "observation_start": start}).json()
    return [(o["date"], float(o["value"])) for o in j["observations"] if o["value"] != "."]


def macro() -> dict:
    out = {}
    for name, sid in FRED_SERIES.items():
        obs = fred_series(sid)
        vals = [v for _, v in obs]
        out[name] = {"level": vals[-1], "chg_5d": vals[-1] - vals[-6] if len(vals) > 6 else 0.0,
                     "hist": vals[-504:]}
    return out


TIER1 = re.compile(r"consumer price index|employment situation|personal income and outlays|"
                   r"producer price|gross domestic product|fomc", re.I)


def fred_releases(days: int = 14) -> list[dict]:
    key = os.environ["FRED_API_KEY"]
    today = dt.date.today()
    j = _get(f"{FRED}/releases/dates", params={
        "api_key": key, "file_type": "json",
        "realtime_start": today.isoformat(),
        "realtime_end": (today + dt.timedelta(days=days)).isoformat(),
        "include_release_dates_with_no_data": "true", "limit": 1000}).json()
    out = []
    for r in j.get("release_dates", []):
        name = r.get("release_name", "")
        d = r["date"]
        if d < today.isoformat():
            continue
        out.append({"date": d, "name": name, "tier1": bool(TIER1.search(name)),
                    "days_out": (dt.date.fromisoformat(d) - today).days})
    return sorted(out, key=lambda x: x["date"])


# --------------------------------------------------------------------------
# 3. FED EVENT CLOCK — RSS + FOMC calendar page, keyless
# --------------------------------------------------------------------------
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
        d = feedparser.parse(url)
        for e in d.entries[:40]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if not ts:
                continue
            when = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc)
            age_h = (now - when).total_seconds() / 3600
            if age_h > days_back * 24 or age_h < -24 * 30:
                continue
            text = f"{e.get('title', '')} {e.get('summary', '')}"
            hawk += len(HAWK.findall(text))
            dove += len(DOVE.findall(text))
            items.append({"kind": kind, "title": e.get("title", ""), "age_h": round(age_h, 1),
                          "chair": bool(CHAIR.search(text)),
                          "when": when.isoformat()})
    tone = (dove - hawk) / max(dove + hawk, 1)      # +1 dovish (bullish metals)
    return {"items": items[:40], "speech_tone": tone, "hawk_hits": hawk, "dove_hits": dove}


def fomc_dates() -> list[str]:
    html = _get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                timeout=15).text
    year = dt.date.today().year
    months = {m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"], 1)}
    found = set()
    for mon, day in re.findall(r"(January|February|March|April|May|June|July|August|"
                               r"September|October|November|December)\s*(\d{1,2})[-–]?\d*",
                               html):
        try:
            found.add(dt.date(year, months[mon.lower()], int(day)).isoformat())
        except ValueError:
            continue
    return sorted(found)


def next_event(dates: list[str]) -> int | None:
    today = dt.date.today().isoformat()
    fut = [d for d in dates if d >= today]
    return (dt.date.fromisoformat(fut[0]) - dt.date.today()).days if fut else None


# --------------------------------------------------------------------------
# 4. KALSHI — public market data, no key
# --------------------------------------------------------------------------
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_KEYWORDS = {
    "fed": ("FED", "FOMC", "INTEREST RATE", "RATE HIKE", "RATE CUT"),
    "cpi": ("CPI", "INFLATION"),
    "shutdown": ("SHUTDOWN",),
    "tariff": ("TARIFF",),
}


def _kalshi_price(m: dict) -> float | None:
    bid, ask = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
    if bid is not None and ask is not None:
        try:
            return (float(bid) + float(ask)) / 2
        except (TypeError, ValueError):
            pass
    bid_c, ask_c = m.get("yes_bid"), m.get("yes_ask")      # legacy cents fields
    if bid_c is None or ask_c is None:
        return None
    return (float(bid_c) + float(ask_c)) / 200


def kalshi_markets(max_pages: int = 3) -> list[dict]:
    out, cursor = [], None
    for _ in range(max_pages):
        p = {"status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        j = _get(f"{KALSHI}/markets", params=p, timeout=12).json()
        out.extend(j.get("markets", []))
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def kalshi_topics() -> dict:
    markets = kalshi_markets()
    topics: dict[str, list[dict]] = {k: [] for k in KALSHI_KEYWORDS}
    for m in markets:
        blob = f"{m.get('title', '')} {m.get('ticker', '')}".upper()
        p = _kalshi_price(m)
        if p is None:
            continue
        for topic, kws in KALSHI_KEYWORDS.items():
            if any(k in blob for k in kws):
                topics[topic].append({
                    "ticker": m.get("ticker"), "title": m.get("title"), "p": round(p, 4),
                    "volume": float(m.get("volume_fp") or m.get("volume") or 0)})
    for t in topics:
        topics[t] = sorted(topics[t], key=lambda x: -x["volume"])[:8]
    return topics


def hike_pressure_from(topics: dict) -> float:
    """+1 = market leaning hawkish (bearish metals), -1 = leaning dovish."""
    hike = cut = 0.0
    for m in topics.get("fed", []):
        t = (m["title"] or "").lower()
        if any(k in t for k in ("hike", "increase", "raise")):
            hike = max(hike, m["p"])
        if any(k in t for k in ("cut", "decrease", "lower")):
            cut = max(cut, m["p"])
    return round(hike - cut, 4)


# --------------------------------------------------------------------------
# 5. POLYMARKET — Gamma discovery + CLOB midpoints, no key
# --------------------------------------------------------------------------
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
        j = _get(f"{GAMMA}/markets/keyset", params=p, timeout=12).json()
        for m in j.get("markets", []):
            q = (m.get("question") or "").lower()
            if any(k in q for k in PM_KEYWORDS):
                matches.append(m)
        cursor = j.get("next_cursor")
        if not cursor:
            break

    token_of, out = {}, []
    for m in matches[:25]:
        ids = m.get("clobTokenIds")
        if not ids:
            try:
                ids = _get(f"{GAMMA}/markets/{m['id']}", timeout=8).json().get("clobTokenIds")
            except requests.RequestException:
                continue
        try:
            yes = json.loads(ids)[0]
        except (TypeError, ValueError, IndexError):
            continue
        token_of[yes] = m.get("question")

    if not token_of:
        return out
    r = S.post(f"{CLOB}/midpoints", json=[{"token_id": t} for t in token_of],
               timeout=TIMEOUT)
    r.raise_for_status()
    mids = r.json()
    for token, question in token_of.items():
        mid = mids.get(token)
        if mid is None:
            continue
        out.append({"question": question, "p": round(float(mid), 4)})
    return out


# --------------------------------------------------------------------------
# 6. POLITICAL — trumpstruth.org RSS + Federal Register, both keyless
# --------------------------------------------------------------------------
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
        raw = f"{e.get('title', '')} {e.get('summary', '')}"
        txt = re.sub(r"<[^>]+>", " ", raw)
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
                         "text": txt.strip()[:160], "when": when.isoformat()})
    return hits


FR_API = "https://www.federalregister.gov/api/v1/documents.json"
FR_HOT = re.compile(r"tariff|section 232|section 301|duties|import|sanction|export control", re.I)


def federal_register(days: int = 5) -> list[dict]:
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    j = _get(FR_API, params={
        "per_page": 60, "order": "newest",
        "conditions[type][]": "PRESDOCU",
        "conditions[publication_date][gte]": since,
        "fields[]": ["title", "signing_date", "publication_date",
                     "presidential_document_type", "html_url"]}, timeout=12).json()
    out = []
    for r in j.get("results", []):
        title = r.get("title") or ""
        out.append({"title": title[:160], "type": r.get("presidential_document_type"),
                    "date": r.get("signing_date") or r.get("publication_date"),
                    "hot": bool(FR_HOT.search(title)), "url": r.get("html_url")})
    return out


# --------------------------------------------------------------------------
# 7. GDELT tone — keyless
# --------------------------------------------------------------------------
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
            j = _get(GDELT, params={"query": q, "mode": "timelinetone",
                                    "timespan": timespan, "format": "json"},
                     timeout=12).json()
            data = j["timeline"][0]["data"]
            out[name] = round(sum(d["value"] for d in data) / max(len(data), 1), 4)
        except (requests.RequestException, KeyError, IndexError, ValueError):
            out[name] = None
    return out
