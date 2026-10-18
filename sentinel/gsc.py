"""Search Console CSV export analysis (Performance > Queries, exported as CSV).

Finds: striking-distance queries (position 5-20), low-CTR queries on page one, and buyer-style
questions worth adding as tracked prompts. A live Search Console API link needs OAuth credentials;
this works from the exported file today.
"""
import csv
import io

QUESTION_WORDS = {"how", "what", "why", "when", "where", "who", "which", "can", "does", "is", "are",
                  "should", "best", "top", "compare", "alternatives", "alternative"}


def _num(x) -> float:
    try:
        return float(str(x).replace(",", "").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_csv(text: str) -> list[dict]:
    """Search Console or Bing Webmaster 'Queries' export (header names are matched loosely)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        return []
    keys = list(rows[0])

    def find(*needles):
        return next((k for k in keys if any(n in k.lower() for n in needles)), None)

    q, imp, pos = find("quer") or keys[0], find("impression"), find("position")
    clk, ctr = find("click"), find("ctr")
    if not imp or not pos:
        raise ValueError("Need 'Impressions' and 'Position' columns. Export the Queries table from Search Console or Bing Webmaster Tools.")
    return [{"query": r[q].strip(), "clicks": _num(r.get(clk, 0)) if clk else 0.0,
             "impressions": _num(r[imp]), "ctr": _num(r.get(ctr, 0)) if ctr else 0.0,
             "position": _num(r[pos])} for r in rows if (r.get(q) or "").strip()]


def merge_rows(*row_lists) -> list[dict]:
    """Combine several exports (for example Google and Bing): sum clicks and impressions,
    impression-weighted position, CTR recomputed."""
    acc = {}
    for rows in row_lists:
        for r in rows:
            a = acc.setdefault(r["query"].lower(), {"query": r["query"], "clicks": 0.0, "impressions": 0.0, "pw": 0.0})
            a["clicks"] += r["clicks"]; a["impressions"] += r["impressions"]; a["pw"] += r["position"] * r["impressions"]
    return [{"query": a["query"], "clicks": a["clicks"], "impressions": a["impressions"],
             "ctr": 100 * a["clicks"] / a["impressions"] if a["impressions"] else 0.0,
             "position": a["pw"] / a["impressions"] if a["impressions"] else 0.0} for a in acc.values()]


def is_question(q: str) -> bool:
    w = q.lower().split()
    return bool(w) and (w[0] in QUESTION_WORDS or q.endswith("?") or " vs " in q.lower() or "alternative" in q.lower())


def analyze(rows: list[dict], tracked_prompts=(), min_impr=50) -> dict:
    tracked = {p.lower() for p in tracked_prompts}
    big = [r for r in rows if r["impressions"] >= min_impr]
    striking = sorted((r for r in big if 5 <= r["position"] <= 20), key=lambda r: -r["impressions"])[:10]
    low_ctr = sorted((r for r in big if r["position"] <= 10 and r["ctr"] < 1.5), key=lambda r: -r["impressions"])[:10]
    questions = sorted((r for r in rows if is_question(r["query"]) and r["impressions"] >= min_impr // 2
                        and r["query"].lower() not in tracked), key=lambda r: -r["impressions"])[:10]
    return {"queries": len(rows), "impressions": int(sum(r["impressions"] for r in rows)),
            "clicks": int(sum(r["clicks"] for r in rows)), "striking": striking, "low_ctr": low_ctr,
            "suggested_prompts": [r["query"] for r in questions]}


def render(a: dict) -> str:
    L = ["## Search Console opportunities",
         f"{a['queries']} queries, {a['impressions']:,} impressions, {a['clicks']:,} clicks."]
    if a["striking"]:
        L += ["", "**Striking distance (position 5-20): small improvements can reach page one**"]
        L += [f"- {r['query']}: position {r['position']:.1f}, {int(r['impressions']):,} impressions" for r in a["striking"][:5]]
    if a["low_ctr"]:
        L += ["", "**Page one but low click rate: rewrite the title and meta description**"]
        L += [f"- {r['query']}: position {r['position']:.1f}, CTR {r['ctr']:.1f}%" for r in a["low_ctr"][:5]]
    if a["suggested_prompts"]:
        L += ["", "**Real questions people search: add these as tracked prompts**"]
        L += [f"- {q}" for q in a["suggested_prompts"][:8]]
    return "\n".join(L)
