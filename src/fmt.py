"""src/fmt.py — rich Telegram message builder.

Adds three things the plain renderer never had:
  1. WHY: top-3 movers, by diffing this run's weighted contributions against the
     previous run for the same metal, read out of state/components.csv (written
     by patch8 before render is ever called).
  2. COT and TONE lines, which previously only printed to the console.
  3. Telegram HTML formatting with colour-coded status emoji.

HTML is used rather than MarkdownV2 because only &, < and > need escaping, so
Trump post text and Federal Register titles can be inserted safely.

Points shown next to each mover are a linear approximation: the score passes
through tanh, so a weighted-contribution delta of dw is worth roughly
dw * 0.55 * 100 / 1.5 ~= dw * 37 points near the middle of the range. Direction
and ranking are exact; the magnitude is indicative.
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

LABELS = {
    "real_rate": "real 10y",
    "usd": "dollar",
    "hike_pressure": "hike odds",
    "inflation_priced": "priced CPI",
    "macro_heat": "macro heat",
    "speech_tone": "Fed speakers",
    "geo_risk": "geo risk",
    "stress_priced": "stress mkts",
    "cot_crowding": "positioning",
    "momentum": "momentum",
    "media_tone": "media tone",
    "gsr": "gold/silver",
}
TOPIC_ICON = {
    "tariff": "🧱", "fed_attack": "🏛", "dollar": "💵",
    "gold": "🥇", "geopol": "🌍", "dealmaking": "🤝",
}


def _dot(label: str) -> str:
    return {"Strongly bearish": "🟥", "Bearish": "🟧", "Neutral": "⬜",
            "Bullish": "🟩", "Strongly bullish": "💚"}.get(label, "⬜")


def esc(s) -> str:
    return html.escape(str(s), quote=False)


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
    prev, now = rows[0], rows[1]
    out = []
    for key, nice in LABELS.items():
        col = f"w_{key}"
        try:
            a, b = prev.get(col, ""), now.get(col, "")
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
        bits.append(f"{metal} {d['latest'] * 100:+.1f}% OI ({pct:.0f}th)")
    return " · ".join(bits) if bits else None


def _tone_line() -> str | None:
    if not GDELT_JSON.exists():
        return None
    try:
        tones = (json.loads(GDELT_JSON.read_text()).get("tones") or {})
    except (OSError, ValueError):
        return None
    bits = [f"{k} {v['v']:+.1f}" for k, v in tones.items()
            if isinstance(v, dict) and v.get("v") is not None]
    return " · ".join(bits) if bits else None


def build(results: list[dict], meta: dict, data: dict, prev: dict) -> str:
    L = []

    for r in results:
        p = prev.get(r["metal"], {}).get("vibe")
        if p is None:
            delta = ""
        else:
            d = r["vibe"] - p
            arrow = "▲" if d > 0 else ("▼" if d < 0 else "▬")
            delta = f" {arrow}{abs(d):.0f}"
        L.append(f"{_dot(r['label'])} <b>{r['metal']} {r['vibe']:+.0f}</b> "
                 f"{esc(r['label'])}{delta} · <i>conf {r['confidence'] * 100:.0f}%</i>")

    L.append("─────────────────")
    L.append(f"📊 regime <b>{results[0]['regime']:+.0f}</b> · "
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
        L.append(f"🏦 hike lean <b>{hike:+.2f}</b>{esc(extra)}")

    clock = meta.get("clock") or {}
    if clock.get("upcoming"):
        L.append(f"⏰ {esc(clock['upcoming'][0])}")
    elif clock.get("next_fomc_days") is not None:
        L.append(f"⏰ FOMC in <b>{clock['next_fomc_days']}d</b>")

    movers = _movers(results[0]["metal"])
    if movers:
        L.append("🧭 <b>why</b> " + esc(" · ".join(movers)))
    if skew_why := results[0].get("skew_why"):
        L.append(f"🎯 skew <code>{esc(skew_why)}</code>")

    if cot := _cot_line():
        L.append(f"📈 COT {esc(cot)}")
    if tone := _tone_line():
        L.append(f"🗣 tone {esc(tone)}")

    for e in sorted(meta.get("events") or [], key=lambda x: -abs(x["impact"]))[:2]:
        if abs(e["impact"]) < 0.2:
            continue
        icon = TOPIC_ICON.get(e["topic"], "📰")
        age = f"{e['age_min'] / 60:.0f}h" if e["age_min"] > 90 else f"{e['age_min']:.0f}m"
        L.append(f"{icon} <b>{esc(e['topic'])}</b> {e['impact']:+.2f} "
                 f"<i>({age})</i>\n   <i>{esc(e['text'][:110])}…</i>")

    for d in [d for d in (data.get("fedreg") or []) if d.get("hot")][:2]:
        L.append(f"📜 {esc(d['title'][:90])}")

    met = (data.get("metals") or {})
    xau, xag = (met.get("XAU") or {}).get("px"), (met.get("XAG") or {}).get("px")
    if xau and xag:
        L.append(f"💵 XAU <b>{xau:,.2f}</b> · XAG <b>{xag:,.2f}</b> "
                 f"· ratio {xau / xag:.1f}")

    if results[0].get("missing"):
        L.append(f"⚠️ <i>stale: {esc(', '.join(results[0]['missing']))}</i>")

    return "\n".join(L)
