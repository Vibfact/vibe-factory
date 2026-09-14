"""src/fmt.py  (v2 — full headlines, clickable links)

Changes vs v1:
  * headlines are no longer truncated; Federal Register titles and Trump posts
    render in full and become hyperlinks when a URL is available
  * total message length is capped at 3,800 characters (Telegram's limit is
    4,096) by dropping the least important trailing lines rather than
    mid-sentence truncation
  * label text spelled out, plus a plain-language read of what the score means

Requires patch9 for untruncated source text.
"""
from __future__ import annotations

import csv
import html
import json
import pathlib

COMP_CSV = pathlib.Path("state/components.csv")
COT_JSON = pathlib.Path("state/cot.json")
GDELT_JSON = pathlib.Path("state/gdelt_state.json")

PTS_PER_W = 37.0
MAX_CHARS = 3800

LABELS = {
    "real_rate": "real 10y", "usd": "dollar", "hike_pressure": "hike odds",
    "inflation_priced": "priced CPI", "macro_heat": "macro heat",
    "speech_tone": "Fed speakers", "geo_risk": "geo risk",
    "stress_priced": "stress mkts", "cot_crowding": "positioning",
    "momentum": "momentum", "media_tone": "media tone", "gsr": "gold/silver",
}
TOPIC_ICON = {"tariff": "🧱", "fed_attack": "🏛", "dollar": "💵",
              "gold": "🥇", "geopol": "🌍", "dealmaking": "🤝"}


def _dot(label: str) -> str:
    return {"Strongly bearish": "🟥", "Bearish": "🟧", "Neutral": "⬜",
            "Bullish": "🟩", "Strongly bullish": "💚"}.get(label, "⬜")


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def link(text: str, url: str | None) -> str:
    t = esc(text)
    return f'<a href="{esc(url)}">{t}</a>' if url else t


def _rows(metal: str) -> list[dict]:
    if not COMP_CSV.exists():
        return []
    try:
        with COMP_CSV.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("metal") == metal]
    except OSError:
        return []
    return rows[-2:]


def _movers(metal: str, top: int = 3) -> list[str]:
    rows = _rows(metal)
    if len(rows) < 2:
        return []
    prev, now = rows
    out = []
    for key, nice in LABELS.items():
        try:
            a, b = prev.get(f"w_{key}", ""), now.get(f"w_{key}", "")
            if a in ("", None) or b in ("", None):
                continue
            d = float(b) - float(a)
        except (TypeError, ValueError):
            continue
        if abs(d) < 0.004:
            continue
        out.append((abs(d), f"{nice} {'+' if d > 0 else '−'}{abs(d) * PTS_PER_W:.0f}"))
    out.sort(reverse=True)
    return [t for _, t in out[:top]]


def _cot_line() -> str | None:
    if not COT_JSON.exists():
        return None
    try:
        data = json.loads(COT_JSON.read_text()).get("data") or {}
    except (OSError, ValueError):
        return None
    bits = []
    for metal in ("XAU", "XAG"):
        d = data.get(metal)
        if not d:
            continue
        hist = [v for v in d.get("hist", []) if v is not None]
        pct = (sum(1 for v in hist if v <= d["latest"]) / len(hist) * 100) if hist else 0
        bits.append(f"{metal} {d['latest'] * 100:+.1f}% OI ({pct:.0f}th pct)")
    return " · ".join(bits) if bits else None


def _tone_line() -> str | None:
    if not GDELT_JSON.exists():
        return None
    try:
        tones = json.loads(GDELT_JSON.read_text()).get("tones") or {}
    except (OSError, ValueError):
        return None
    bits = [f"{k} {v['v']:+.1f}" for k, v in tones.items()
            if isinstance(v, dict) and v.get("v") is not None]
    return " · ".join(bits) if bits else None


def _reading(vibe: float, conf: float) -> str:
    """One line of plain language, so the number never stands alone."""
    a = abs(vibe)
    if a < 20:
        s = "no directional edge"
    elif a < 40:
        s = "mild tilt, tradeable only with other confirmation"
    elif a < 60:
        s = "clear tilt across most components"
    elif a < 80:
        s = "strong, broad agreement"
    else:
        s = "extreme — check for a data error before acting"
    if conf < 0.6:
        s += " · low confidence, treat as noise"
    return s


def build(results: list[dict], meta: dict, data: dict, prev: dict) -> str:
    head, body, tail = [], [], []

    for r in results:
        p = prev.get(r["metal"], {}).get("vibe")
        if p is None:
            delta = ""
        else:
            d = r["vibe"] - p
            arrow = "▲" if d > 0 else ("▼" if d < 0 else "▬")
            delta = f" {arrow}{abs(d):.0f}"
        head.append(f"{_dot(r['label'])} <b>{r['metal']} {r['vibe']:+.0f}</b> "
                    f"{esc(r['label'])}{delta} · <i>conf {r['confidence'] * 100:.0f}%</i>")
    head.append(f"<i>{esc(_reading(results[0]['vibe'], results[0]['confidence']))}</i>")
    head.append("─────────────────")

    body.append(f"📊 regime <b>{results[0]['regime']:+.0f}</b> · "
                f"pressure <b>{results[0]['pressure']:+.0f}</b>")

    hike = meta.get("hike")
    if hike is not None:
        extra = ""
        try:
            from src.patch3 import LAST_DETAIL as D
            if D.get("p_hike") is not None:
                extra = (f" — E[{D['expected_bps']:+.0f}bps], "
                         f"hike {D['p_hike'] * 100:.0f}% / hold {D['p_hold'] * 100:.0f}%")
        except Exception:                                      # noqa: BLE001
            pass
        body.append(f"🏦 hike lean <b>{hike:+.2f}</b>{esc(extra)}")

    clock = meta.get("clock") or {}
    if clock.get("upcoming"):
        body.append(f"⏰ {esc(clock['upcoming'][0])}")
    elif clock.get("next_fomc_days") is not None:
        body.append(f"⏰ FOMC in <b>{clock['next_fomc_days']}d</b>")

    if movers := _movers(results[0]["metal"]):
        body.append("🧭 <b>why</b> " + esc(" · ".join(movers)))
    if why := results[0].get("skew_why"):
        body.append(f"🎯 skew <code>{esc(why)}</code>")
    if cot := _cot_line():
        body.append(f"📈 COT {esc(cot)}")
    if tone := _tone_line():
        body.append(f"🗣 tone {esc(tone)}")

    for e in sorted(meta.get("events") or [], key=lambda x: -abs(x["impact"]))[:2]:
        if abs(e["impact"]) < 0.2:
            continue
        icon = TOPIC_ICON.get(e["topic"], "📰")
        age = f"{e['age_min'] / 60:.0f}h" if e["age_min"] > 90 else f"{e['age_min']:.0f}m"
        body.append(f"{icon} <b>{esc(e['topic'])}</b> {e['impact']:+.2f} <i>({age})</i>\n"
                    f"   {link(e.get('text', ''), e.get('url'))}")

    for d in [d for d in (data.get("fedreg") or []) if d.get("hot")][:3]:
        body.append(f"📜 {link(d.get('title', ''), d.get('url'))}")

    met = data.get("metals") or {}
    xau, xag = (met.get("XAU") or {}).get("px"), (met.get("XAG") or {}).get("px")
    if xau and xag:
        tail.append(f"💵 XAU <b>{xau:,.2f}</b> · XAG <b>{xag:,.2f}</b> · ratio {xau / xag:.1f}")
    if results[0].get("missing"):
        tail.append(f"⚠️ <i>stale: {esc(', '.join(results[0]['missing']))}</i>")

    lines = head + body + tail
    while sum(len(x) + 1 for x in lines) > MAX_CHARS and len(body) > 4:
        body.pop(-1)                                # drop least important context first
        lines = head + body + tail
    return "\n".join(lines)
