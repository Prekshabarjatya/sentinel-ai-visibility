"""Turn crawler checks, page gaps and citation data into one prioritized fix list.

Priority:  P0 = blocks all citations (fix first).  P1 = page changes tied to a losing prompt.
           P2 = off-site sources to earn (done by a person, not automated).
Every item is a recommendation for the client to approve; nothing is published.
Every item has a stable id so applied fixes can be tracked (see impact.py).
"""
import hashlib
from collections import Counter

from . import pagegap, parser, review

LOSING_BELOW = 50.0  # prompt SoV (%) under which we treat a prompt as losing


def losing_prompts(sov_by_prompt: dict) -> list[str]:
    return sorted((p for p, v in sov_by_prompt.items() if v < LOSING_BELOW), key=lambda p: sov_by_prompt[p])


def fix_id(f: dict) -> str:
    key = "|".join(str(f.get(k, "")) for k in ("priority", "title", "prompt", "page"))
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def cited_pages_for(prompt, runs, client):
    """Return (competitor_urls, third_party_urls) cited in answers to this prompt where the client was omitted."""
    own = parser.norm_domain(client["domain"])
    comp = {parser.norm_domain(c["domain"]) for c in client.get("competitors", [])}
    c_urls, t_urls = Counter(), Counter()
    for r in runs:
        if r["prompt"] != prompt or not r.get("ok") or r["client_cited"]:
            continue
        for u in r["urls"]:
            host = parser.norm_domain(u)
            if host == own or host.endswith("." + own):
                continue
            (c_urls if host in comp or any(host.endswith("." + d) for d in comp) else t_urls)[u] += 1
    return [u for u, _ in c_urls.most_common(3)], [u for u, _ in t_urls.most_common(5)]


def build_fix_list(client, runs, sov_by_prompt, robots, fetch, max_pages_per_prompt=3, llm=None):
    fixes = []

    # P0: crawler access
    if robots.get("status") == "error":
        fixes.append({"priority": "P0", "title": "Could not read robots.txt",
                      "why": robots.get("error", ""), "action": "Check the site is reachable, then re-run.",
                      "effort": "Low", "automated": True})
    elif robots.get("blocked_search"):
        bots = ", ".join(robots["blocked_search"])
        fixes.append({"priority": "P0", "title": f"AI search crawlers are blocked: {bots}",
                      "why": "A blocked crawler cannot read your pages, so the engine cannot cite them.",
                      "action": f"Edit robots.txt (and any firewall or CDN bot rules) to allow: {bots}.",
                      "effort": "Low", "automated": True})

    pages = client.get("target_pages", {})
    default_url = client.get("default_page", "https://" + parser.norm_domain(client["domain"]))

    for prompt in losing_prompts(sov_by_prompt):
        c_urls, t_urls = cited_pages_for(prompt, runs, client)
        url = pages.get(prompt, default_url)
        try:
            html = fetch(url)[1]
            mine = pagegap.analyze_html(html)
            mine_text = pagegap.visible_text(html)
        except Exception as e:
            fixes.append({"priority": "P1", "title": f"Could not fetch {url}", "why": str(e)[:120],
                          "action": "Confirm the URL and that the site allows our checker.", "effort": "Low",
                          "prompt": prompt, "automated": True})
            continue

        comps, comp_texts, failed = [], [], []
        for cu in c_urls[:max_pages_per_prompt]:
            try:
                h = fetch(cu)[1]
                comps.append(pagegap.analyze_html(h))
                comp_texts.append(pagegap.visible_text(h))
            except Exception:
                failed.append(cu)
        coverage = f" (compared {len(comps)} of {len(comps) + len(failed)} cited competitor pages)" if failed else ""

        # be explicit about what could not be compared, instead of silently skipping it
        if failed:
            fixes.append({"priority": "P1", "title": "Some cited competitor pages could not be fetched",
                          "why": "Blocked or unreachable: " + ", ".join(failed),
                          "action": "Open these pages by hand and compare them with yours; the automated comparison is incomplete.",
                          "effort": "Low", "prompt": prompt, "page": failed[0], "automated": False})

        for g in pagegap.find_gaps(mine, comps):
            fixes.append({"priority": "P1", "title": g["label"], "why": g["evidence"] + coverage,
                          "action": g["action"], "effort": "Medium", "prompt": prompt, "page": url,
                          "automated": False})

        if llm and comp_texts:
            rv = review.review_page(prompt, mine_text, comp_texts, llm)
            if rv["status"] == "ok" and rv["answers_directly"] <= 3 and rv["answers_directly"] < rv["competitor_avg"]:
                fixes.append({"priority": "P1", "title": "Page does not answer this question directly enough",
                              "why": f"Content review score {rv['answers_directly']}/5 vs {rv['competitor_avg']}/5 for cited pages{coverage}",
                              "action": " ".join(rv["suggestions"]) or "Rewrite the opening to answer the question directly.",
                              "effort": "Medium", "prompt": prompt, "page": url, "automated": False})
            elif rv["status"] in ("unavailable", "unparseable"):
                fixes.append({"priority": "P1", "title": "Content review did not run",
                              "why": rv.get("reason", ""), "action": "Re-run later or review the page by hand.",
                              "effort": "Low", "prompt": prompt, "page": url, "automated": True})

        for tu in t_urls[:3]:
            fixes.append({"priority": "P2", "title": f"Earn a mention on {parser.norm_domain(tu)}",
                          "why": f"Engines cited {tu} for this prompt and you are not on it.",
                          "action": "Check whether it is a review site, directory or article you can be listed in or quoted by.",
                          "effort": "High", "prompt": prompt, "page": tu, "automated": False})

    seen, out = set(), []
    for f in sorted(fixes, key=lambda f: (f["priority"], f.get("prompt", ""))):
        key = (f["priority"], f["title"], f.get("prompt"), f.get("page"))
        if key not in seen:
            seen.add(key)
            f["id"] = fix_id(f)
            out.append(f)
    return out


def cited_third_party_domains(runs, client, top=10) -> list[str]:
    """Sites (not the client, not tracked competitors) that engines cite, most frequent first."""
    own = parser.norm_domain(client["domain"])
    comp = {parser.norm_domain(c["domain"]) for c in client.get("competitors", [])}
    c = Counter()
    for r in runs:
        if r.get("ok"):
            for u in r["urls"]:
                h = parser.norm_domain(u)
                if h != own and not h.endswith("." + own) and h not in comp:
                    c[h] += 1
    return [d for d, _ in c.most_common(top)]


def merge(*lists) -> list[dict]:
    """Combine fix lists: de-duplicate, assign ids, order by priority."""
    seen, out = set(), []
    for f in sorted((x for l in lists for x in l), key=lambda f: (f["priority"], f.get("prompt", ""), f.get("page", ""))):
        key = (f["priority"], f["title"], f.get("prompt"), f.get("page"))
        if key not in seen:
            seen.add(key)
            f["id"] = fix_id(f)
            out.append(f)
    return out


def render_fixes(fixes) -> str:
    if not fixes:
        return "## Recommended fixes\nNo fixes identified this cycle."
    lines = ["## Recommended fixes", "_Recommendations for your approval. Nothing is changed on your site by us._", ""]
    for pr, label in (("P0", "Fix first: blocks all citations"), ("P1", "Page changes for losing prompts"),
                      ("P2", "Off-site sources to earn")):
        grp = [f for f in fixes if f["priority"] == pr]
        if not grp:
            continue
        lines.append(f"**{pr} · {label}**")
        for f in grp:
            tag = f" (prompt: {f['prompt']})" if f.get("prompt") else ""
            pg = f" [{f['page']}]" if f.get("page") and not f["title"].startswith("Earn a mention") else ""
            lines.append(f"- `{f['id']}` **{f['title']}**{tag}{pg}. {f['action']} _Why:_ {f['why']} _Effort:_ {f['effort']}")
        lines.append("")
    return "\n".join(lines)
