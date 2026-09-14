"""src/patch9.py — full headlines and clickable links.

The truncation happens upstream, not in the formatter:
  * src/fedreg.py._normalise cuts titles to 160 chars
  * src/sources.py.trump_pressure cuts post text to 160 chars and keeps no URL

This module removes both caps and carries the source URL through, so the
message builder can render a full headline as a hyperlink.

Import AFTER patch8.
"""
from __future__ import annotations

import datetime as dt
import re

import feedparser

from src import fedreg as _fedreg

# ------------------------------------------------- 1. Federal Register titles
def _normalise_full(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        title = (r.get("title") or "").strip()
        kind = r.get("presidential_document_type") or r.get("subtype") or r.get("type")
        out.append({
            "title": title,                          # no truncation
            "type": kind,
            "date": r.get("signing_date") or r.get("publication_date"),
            "hot": bool(_fedreg.FR_HOT.search(title)),
            "url": r.get("html_url"),
        })
    return out


_fedreg._normalise = _normalise_full     # resolved at call time inside fedreg


def federal_register(days: int = 5) -> list[dict]:
    seen, out = set(), []
    for d in _fedreg.federal_register(days):
        k = (d.get("title") or "")[:90].lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


# ------------------------------------------------------- 2. Trump posts
from src.sources import TOPICS, VADER, CAP_PER_TOPIC     # noqa: E402

TRUMP_FEED = "https://www.trumpstruth.org/feed"
MAX_TEXT = 600


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
        txt = re.sub(r"\s+", " ", txt).strip()
        # the archive often repeats the title inside the summary
        half = len(txt) // 2
        if half > 30 and txt[:half].strip() == txt[half:].strip():
            txt = txt[:half].strip()

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
                         "text": txt[:MAX_TEXT],
                         "url": e.get("link"),
                         "when": when.isoformat()})
    return hits


print("[info] patch9 active: full headlines, source URLs")
