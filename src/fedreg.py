"""src/fedreg.py — Federal Register presidential documents, self-healing.

Why this file exists: the previous call sent fields[]=presidential_document_type,
which is not a valid field name on documents.json (it is only valid as a
*condition*, conditions[presidential_document_type]). The API answers 400 for an
unknown field. Working examples of this endpoint request type + subtype instead.

Rather than betting on one exact spelling, this module tries a ladder of query
variants and keeps the first that returns HTTP 200, then remembers which one
worked. Total failure returns [] instead of raising: this feed is contextual
colour, not a scored component, so it must never break a run.
"""
from __future__ import annotations

import datetime as dt
import re

import requests

API = "https://www.federalregister.gov/api/v1/documents.json"
PI_API = "https://www.federalregister.gov/api/v1/public-inspection-documents.json"
UA = {"User-Agent": "vibe-factory/1.1 (personal research; contact via repo issues)"}
TIMEOUT = 12

FR_HOT = re.compile(r"tariff|section 232|section 301|duties|import|sanction|"
                    r"export control|emergency|proclamation", re.I)

_WORKING_VARIANT: int | None = None


def _variants(since: str) -> list[dict]:
    base = {"per_page": 40, "order": "newest"}
    safe_fields = ["title", "type", "subtype", "signing_date",
                   "publication_date", "html_url"]
    return [
        # 0: safe field list + presidential documents only
        {**base, "conditions[type][]": "PRESDOCU",
         "conditions[publication_date][gte]": since, "fields[]": safe_fields},
        # 1: same, no explicit fields (API returns its defaults)
        {**base, "conditions[type][]": "PRESDOCU",
         "conditions[publication_date][gte]": since},
        # 2: filter by document subtype instead of type
        {**base, "conditions[presidential_document_type][]": "executive_order",
         "conditions[publication_date][gte]": since, "fields[]": safe_fields},
        # 3: no date condition at all, just newest presidential documents
        {**base, "conditions[type][]": "PRESDOCU", "fields[]": safe_fields},
        # 4: bare minimum
        {**base},
    ]


def _normalise(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        title = r.get("title") or ""
        kind = r.get("presidential_document_type") or r.get("subtype") or r.get("type")
        out.append({
            "title": title[:160],
            "type": kind,
            "date": r.get("signing_date") or r.get("publication_date"),
            "hot": bool(FR_HOT.search(title)),
            "url": r.get("html_url"),
        })
    return out


def federal_register(days: int = 5) -> list[dict]:
    global _WORKING_VARIANT
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    variants = _variants(since)
    order = ([_WORKING_VARIANT] if _WORKING_VARIANT is not None else []) + \
            [i for i in range(len(variants)) if i != _WORKING_VARIANT]

    last_err = ""
    for i in order:
        try:
            r = requests.get(API, params=variants[i], headers=UA, timeout=TIMEOUT)
            if r.status_code == 400:
                last_err = f"variant {i}: 400 {r.text[:120]}"
                continue
            r.raise_for_status()
            _WORKING_VARIANT = i
            return _normalise(r.json().get("results", []))
        except requests.RequestException as exc:
            last_err = f"variant {i}: {type(exc).__name__}: {exc}"

    # Last resort: documents on public inspection (filed but not yet published).
    try:
        r = requests.get(PI_API, params={"per_page": 40}, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        docs = [d for d in r.json().get("results", [])
                if (d.get("type") == "PRESDOCU") or FR_HOT.search(d.get("title") or "")]
        return _normalise(docs)
    except requests.RequestException as exc:
        last_err += f" | public-inspection: {exc}"

    print(f"[warn] fedreg unavailable, continuing without it ({last_err[:200]})")
    return []
