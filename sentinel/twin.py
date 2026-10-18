"""Digital twin: ask the same prompts with the client's page as it is vs with the fixes applied.

The engine cannot browse, so the page text is handed to it as one search result among many.
This measures whether the fixed page is more quotable, not real search ranking.

    python -m sentinel.twin clients/acme.json --samples 2
"""
import argparse
import html
import json
import os
import re
import time
from pathlib import Path

import requests

from . import parser, providers

FRAME = ("You are an AI assistant answering a shopper in region '{region}'. A web search returned the results below. "
         "Use them only where they actually help answer the question, otherwise rely on what you know. "
         "Name specific brands.\n\n{results}\n\nQuestion: {q}")


def serp(q, client, cache):
    """Top Google organic results for q via SerpApi (needs SERPAPI_API_KEY), cached per prompt to save searches."""
    if q not in cache:
        key = os.environ.get("SERPAPI_API_KEY")
        if not key:
            return []
        r = requests.get("https://serpapi.com/search.json", timeout=30, params={
            "q": q, "api_key": key, "gl": client.get("region", "us"), "hl": client.get("language", "en"),
            **({"google_domain": "google.co.in"} if client.get("region") == "in" else {})})  # "num" garbled results
        r.raise_for_status()
        cache[q] = [{"title": o.get("title", ""), "link": o.get("link", ""), "snippet": o.get("snippet", "")}
                    for o in r.json().get("organic_results", [])[:5]]
    return cache[q]


def results_block(page, organic, slot=2):
    """Real search results with the client page inserted at a fixed rank, so both twins compete on equal terms."""
    own = {"title": "Client page", "link": "", "snippet": page}
    items = organic[:slot] + [own] + organic[slot:]
    return "\n\n".join(f"[Result {i}] {o['title']} {o['link']}\n{o['snippet']}" for i, o in enumerate(items, 1))


def page_text(raw_html: str, limit=3000) -> str:
    """Visible text of a page, keeping title and headings."""
    t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw_html)
    t = re.sub(r"(?i)<(h[1-6]|title)[^>]*>", "\n# ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()[:limit]


def apply_fixes(text: str, fixes: dict) -> str:
    """Fixed version: new title + snippet summary on top, FAQ (question headings + answers) appended."""
    faq = "\n".join(f"## {q}\n{a}" for q, a in fixes["faq"].items())
    return f"# {fixes['title']}\n{fixes['summary']}\n{text}\n# Frequently asked questions\n{faq}"


def run(client, original, fixed, samples, cache, engine_name=None):
    engine = providers.build([engine_name or client["engines"][0]])[0]
    out = {}
    for label, page in (("original", original), ("fixed", fixed)):
        rows = []
        for q in client["prompts"]:
            for _ in range(samples):
                time.sleep(float(os.environ.get("TWIN_PACE", 4)))  # seconds between calls; raise for free-tier rate limits
                try:
                    text, cited = engine.ask(FRAME.format(results=results_block(page, serp(q, client, cache)), q=q, region=client.get("region", "us")))
                    r = parser.parse_response(text, cited, client)
                    rows.append({"prompt": q, "named": r["client_cited"], "competitors": r["competitors_cited"]})
                except providers.ProviderError as e:
                    rows.append({"prompt": q, "error": str(e)})
        out[label] = rows
    return out


def summarize(results):
    s = {}
    for label, rows in results.items():
        ok = [r for r in rows if "named" in r]
        per = {}
        for r in ok:
            per.setdefault(r["prompt"], []).append(r["named"])
        s[label] = {"sov": round(100 * sum(r["named"] for r in ok) / len(ok), 1) if ok else None,
                    "lost_to_competitor": round(100 * sum(1 for r in ok if r["competitors"] and not r["named"]) / len(ok), 1) if ok else None,
                    "competitors_named": {c: sum(c in r["competitors"] for r in ok) for c in sorted({c for r in ok for c in r["competitors"]})},
                    "runs": len(ok), "failed": len(rows) - len(ok),
                    "by_prompt": {p: round(100 * sum(v) / len(v)) for p, v in per.items()}}
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("client")
    ap.add_argument("--fixes", help="JSON with title, summary, faq {question: answer}")
    ap.add_argument("--samples", type=int, default=2)
    ap.add_argument("--engine", help="override the client's first engine, e.g. groq")
    a = ap.parse_args()
    client = json.loads(Path(a.client).read_text())
    fixes = json.loads(Path(a.fixes or Path(a.client).with_name(Path(a.client).stem + "_twin_fixes.json")).read_text())
    raw = requests.get(client["default_page"], timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
    original = page_text(raw)
    fixed = apply_fixes(original, fixes)
    out = Path("data") / client["domain"].replace(".", "_") / "twin.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cache_file = out.with_name("serp.json")
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    for q in client["prompts"]:  # fetch once up front so a failed AI run never re-spends searches
        serp(q, client, cache)
    cache_file.write_text(json.dumps(cache, indent=2))
    summary = summarize(run(client, original, fixed, a.samples, cache, a.engine))
    summary["grounded_in_search"] = bool(cache)
    out.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
