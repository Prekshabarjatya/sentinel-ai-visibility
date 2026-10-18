"""Backlink profile from FREE exports: Ahrefs Webmaster Tools and Bing Webmaster Tools both give a site
owner their own inbound links as CSV. No competitor backlink data is available without paid tools.

Column names are matched loosely, so either export (or both together) can be passed.
"""
import csv
import io
import re
from collections import Counter
from urllib.parse import urlparse

from . import parser

GENERIC = {"click here", "here", "read more", "more", "website", "link", "this", "visit", "site", "source", "learn more"}


def _find(keys, *needles):
    return next((k for k in keys if any(n in k.lower() for n in needles)), None)


def parse_csv(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return []
    keys = list(rows[0])
    src = _find(keys, "source url", "referring page", "from url", "source page")
    dom = _find(keys, "referring domain", "source domain")
    tgt = _find(keys, "target url", "target page", "to url", "target")
    anc = _find(keys, "anchor")
    nof = _find(keys, "nofollow", "rel")
    if not (src or dom):
        raise ValueError("Need a source URL or referring domain column. Export inbound links from Ahrefs Webmaster Tools or Bing Webmaster Tools.")
    out = []
    for r in rows:
        s = (r.get(src) or "").strip() if src else ""
        d = parser.norm_domain((r.get(dom) or "").strip()) if dom and (r.get(dom) or "").strip() else (
            parser.norm_domain(s) if s else "")
        if not d:
            continue
        flag = (r.get(nof) or "").strip().lower() if nof else ""
        out.append({"source": s, "domain": d, "target": (r.get(tgt) or "").strip() if tgt else "",
                    "anchor": (r.get(anc) or "").strip() if anc else "",
                    "nofollow": flag in ("true", "1", "yes", "nofollow") or "nofollow" in flag})
    return out


def merge(*row_lists) -> list[dict]:
    seen, out = set(), []
    for rows in row_lists:
        for r in rows:
            k = (r["source"] or r["domain"], r["target"])
            if k not in seen:
                seen.add(k)
                out.append(r)
    return out


NAKED = re.compile(r"^(https?://|www\.)\S+$|^[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", re.I)


def anchor_type(anchor: str, brand: str, domain: str) -> str:
    a = anchor.strip().lower()
    if not a:
        return "empty or image"
    if NAKED.match(a):
        return "naked URL"
    if brand.lower() in a or parser.norm_domain(domain) in a:
        return "brand"
    if a in GENERIC:
        return "generic"
    return "keyword or other"


def analyze(rows: list[dict], client: dict, cited_domains=()) -> dict:
    own = parser.norm_domain(client["domain"])
    rows = [r for r in rows if r["domain"] != own and not r["domain"].endswith("." + own)]
    n = len(rows)
    by_dom = Counter(r["domain"] for r in rows)
    anchors = Counter(anchor_type(r["anchor"], client["brand"], client["domain"]) for r in rows)
    targets = Counter(r["target"] for r in rows if r["target"])
    nofollow = sum(1 for r in rows if r["nofollow"])
    flags = []
    if n and len(by_dom) < 10:
        flags.append(f"Only {len(by_dom)} referring domains. Authority is thin.")
    if n >= 20 and by_dom.most_common(1)[0][1] / n > 0.4:
        d, c = by_dom.most_common(1)[0]
        flags.append(f"{d} supplies {100 * c // n}% of all links. A single source dominates.")
    if n >= 20 and anchors["keyword or other"] / n > 0.3:
        flags.append(f"{100 * anchors['keyword or other'] // n}% of anchors are keyword-style. Heavy keyword anchors can look manipulative.")
    if n >= 20 and nofollow / n > 0.8:
        flags.append(f"{100 * nofollow // n}% of links are nofollow, which pass little ranking value.")
    linking = set(by_dom)
    missing = [d for d in cited_domains if d not in linking and d != own]
    return {"links": n, "referring_domains": len(by_dom), "top_domains": by_dom.most_common(5),
            "anchor_mix": dict(anchors.most_common()), "top_targets": targets.most_common(5),
            "nofollow_pct": round(100 * nofollow / n, 1) if n else 0.0, "flags": flags,
            "cited_but_not_linking": missing[:10]}


def render(a: dict) -> str:
    L = ["## Backlink profile",
         f"**{a['links']:,} links from {a['referring_domains']:,} referring domains** ({a['nofollow_pct']}% nofollow). "
         "Counts come from the owner's free exports and may be incomplete."]
    if a["anchor_mix"]:
        L += ["", "Anchor mix: " + ", ".join(f"{k} {v}" for k, v in a["anchor_mix"].items())]
    if a["top_domains"]:
        L += ["", "Top referring domains: " + ", ".join(f"{d} ({c})" for d, c in a["top_domains"])]
    for f in a["flags"]:
        L.append(f"- **Watch:** {f}")
    if a["cited_but_not_linking"]:
        L += ["", "**Sites AI engines cite for your prompts that do not link to you (outreach targets):** "
              + ", ".join(a["cited_but_not_linking"])]
    return "\n".join(L)
