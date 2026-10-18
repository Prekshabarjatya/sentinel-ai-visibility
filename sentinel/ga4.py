"""AI-referral traffic from a Google Analytics 4 CSV export (Reports > Acquisition > Traffic acquisition,
dimension 'Session source' or 'Session source / medium'; optional 'Landing page' and key-event columns).

Live GA4 API access needs OAuth credentials; this works from the exported file today.
"""
import csv
import io
from collections import Counter

AI_SOURCES = {
    "chatgpt.com": "ChatGPT", "chat.openai.com": "ChatGPT", "perplexity": "Perplexity",
    "gemini.google.com": "Gemini", "copilot.microsoft.com": "Copilot", "claude.ai": "Claude",
    "you.com": "You.com", "phind.com": "Phind", "poe.com": "Poe", "meta.ai": "Meta AI",
}


def classify(source: str) -> str | None:
    s = (source or "").lower()
    for key, name in AI_SOURCES.items():
        if key in s:
            return name
    return None


def _num(x) -> float:
    try:
        return float(str(x).replace(",", "").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_csv(text: str) -> list[dict]:
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    start = next((i for i, l in enumerate(lines) if "sessions" in l.lower() and "," in l), None)
    if start is None:
        raise ValueError("No header row with a 'Sessions' column found. Export 'Traffic acquisition' from GA4 as CSV.")
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    return rows


def _col(row, *needles):
    for k in row:
        if all(n in k.lower() for n in needles):
            return k
    return None


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"total": 0, "ai": 0, "share": 0.0, "by_source": {}, "top_landing": [], "key_events": None}
    src = _col(rows[0], "source")
    ses = _col(rows[0], "sessions")
    land = _col(rows[0], "landing")
    ev = _col(rows[0], "key event") or _col(rows[0], "conversions")
    if not src or not ses:
        raise ValueError("Need a 'source' column and a 'Sessions' column.")
    total = ai = 0.0
    by, landing, events = Counter(), Counter(), 0.0
    for r in rows:
        n = _num(r[ses]); total += n
        name = classify(r[src])
        if name:
            ai += n; by[name] += n
            if land: landing[r[land]] += n
            if ev: events += _num(r[ev])
    return {"total": int(total), "ai": int(ai), "share": round(100 * ai / total, 2) if total else 0.0,
            "by_source": dict(by.most_common()), "top_landing": landing.most_common(5),
            "key_events": int(events) if ev else None}


def render(s: dict) -> str:
    lines = ["## AI referral traffic (GA4)",
             f"**{s['ai']:,} of {s['total']:,} sessions ({s['share']}%) came from AI assistants.**"]
    if s["by_source"]:
        lines += ["", "| AI source | Sessions |", "| --- | --- |", *[f"| {k} | {int(v):,} |" for k, v in s["by_source"].items()]]
    if s["top_landing"]:
        lines += ["", "Top landing pages from AI: " + "; ".join(f"{p} ({int(n)})" for p, n in s["top_landing"])]
    if s["key_events"] is not None:
        lines += ["", f"Key events from AI sessions: {s['key_events']}"]
    if not s["by_source"]:
        lines += ["", "No AI referrers found in this export. Note: some AI tools send no referrer, so this undercounts."]
    return "\n".join(lines)
