"""Technical SEO audit for a client's key pages. Free: needs only the pages themselves.

Out of scope here (needs paid data): keyword rank tracking, backlinks, search volume.
Issues use the same shape as fixes.py items so they merge into one prioritized list.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from . import aeo, parser

UA = "SentinelBot/1.0 (+AI visibility monitoring)"
SEV = {"P0": "P0", "P1": "P1", "P2": "P2"}


def fetch_full(url: str, timeout=15) -> dict:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": UA}, allow_redirects=True)
    return {"status": r.status_code, "text": r.text, "headers": {k.lower(): v for k, v in r.headers.items()},
            "final_url": r.url, "redirects": len(r.history)}


class _S(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""; self._in_title = False
        self.meta_robots = ""; self.desc = ""; self.canonical = ""; self.viewport = False; self.lang = ""
        self.h_levels = []; self.imgs = 0; self.imgs_no_alt = 0; self.links = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html": self.lang = a.get("lang", "")
        elif tag == "title": self._in_title = True
        elif tag == "meta":
            n = a.get("name", "").lower()
            if n == "robots": self.meta_robots = a.get("content", "").lower()
            elif n == "description": self.desc = a.get("content", "").strip()
            elif n == "viewport": self.viewport = True
        elif tag == "link" and a.get("rel", "").lower() == "canonical": self.canonical = a.get("href", "").strip()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"): self.h_levels.append(int(tag[1]))
        elif tag == "img":
            self.imgs += 1
            if not a.get("alt", "").strip(): self.imgs_no_alt += 1
        elif tag == "a" and a.get("href"): self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "title": self._in_title = False

    def handle_data(self, d):
        if self._in_title: self.title += d


def _norm(u: str) -> str:
    p = urlparse(u)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def _issue(sev, title, why, action, url, effort="Low"):
    return {"priority": sev, "title": title, "why": why, "action": action, "effort": effort,
            "page": url, "automated": False}


def audit_response(url: str, resp: dict) -> list[dict]:
    out = []
    if resp["status"] >= 400:
        return [_issue("P0", f"Page returns HTTP {resp['status']}", "Crawlers cannot read an error page.",
                       "Fix the page or redirect it to the right URL.", url)]
    final = resp.get("final_url", url)
    h = resp.get("headers", {})
    s = _S(); s.feed(resp["text"])

    if "noindex" in s.meta_robots or "noindex" in h.get("x-robots-tag", "").lower():
        out.append(_issue("P0", "Page is set to noindex", "Search and AI engines are told not to index it.",
                          "Remove the noindex directive (meta robots or X-Robots-Tag header) if this page should rank.", url))
    if not final.startswith("https://"):
        out.append(_issue("P1", "Page is not served over HTTPS", f"Final URL: {final}",
                          "Redirect all HTTP traffic to HTTPS.", url))
    if resp.get("redirects", 0) >= 3:
        out.append(_issue("P1", "Long redirect chain", f"{resp['redirects']} redirects before the page loads.",
                          "Point links and redirects straight at the final URL.", url))
    if not s.canonical:
        out.append(_issue("P1", "No canonical URL", "Duplicate versions of the page can split ranking signals.",
                          'Add <link rel="canonical"> pointing to the preferred URL.', url))
    elif _norm(urljoin(final, s.canonical)) != _norm(final):
        out.append(_issue("P1", "Canonical points to a different URL", f"Canonical: {s.canonical}",
                          "Confirm this is intended; otherwise point the canonical at this page.", url))
    t = s.title.strip()
    if not t:
        out.append(_issue("P1", "Missing title tag", "The title is the strongest on-page signal.", "Add a specific title of 30-60 characters.", url))
    elif not 30 <= len(t) <= 60:
        out.append(_issue("P2", "Title length outside 30-60 characters", f"Now {len(t)} characters.", "Rewrite to 30-60 characters with the main topic first.", url))
    if not s.desc:
        out.append(_issue("P1", "Missing meta description", "Search results show a generated snippet instead.", "Write a 70-160 character description.", url))
    elif not 70 <= len(s.desc) <= 160:
        out.append(_issue("P2", "Meta description length outside 70-160 characters", f"Now {len(s.desc)} characters.", "Rewrite to 70-160 characters.", url))
    h1 = s.h_levels.count(1)
    if h1 == 0:
        out.append(_issue("P1", "No H1 heading", "The page has no main heading.", "Add one H1 that names the page topic.", url))
    elif h1 > 1:
        out.append(_issue("P2", "More than one H1", f"{h1} H1 headings.", "Keep one H1; use H2/H3 for sections.", url))
    if any(b - a > 1 for a, b in zip(s.h_levels, s.h_levels[1:])):
        out.append(_issue("P2", "Heading levels skip", "For example H1 followed directly by H3.", "Use headings in order (H1, H2, H3).", url))
    if s.imgs and s.imgs_no_alt / s.imgs > 0.2:
        out.append(_issue("P2", "Many images lack alt text", f"{s.imgs_no_alt} of {s.imgs} images.", "Add descriptive alt text.", url))
    if not s.viewport:
        out.append(_issue("P1", "No mobile viewport tag", "The page may render poorly on phones.", 'Add <meta name="viewport" content="width=device-width, initial-scale=1">.', url))
    if not s.lang:
        out.append(_issue("P2", "No language set on <html>", "Engines guess the language.", 'Add lang="en" (or the right code) to <html>.', url))
    host = urlparse(final).netloc.removeprefix("www.")
    internal = [l for l in s.links if l.startswith("/") or host in l]
    if len(internal) < 3:
        out.append(_issue("P2", "Few internal links", f"{len(internal)} internal links found.", "Link to related key pages so crawlers can reach them.", url))
    return out


def audit_sitemap(domain: str, pages: list[str], fetch=fetch_full) -> list[dict]:
    base = "https://" + parser.norm_domain(domain)
    sitemaps = []
    try:
        r = fetch(base + "/robots.txt")
        if r["status"] == 200:
            sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r["text"])
    except Exception:
        pass
    sm_url = sitemaps[0] if sitemaps else base + "/sitemap.xml"
    try:
        r = fetch(sm_url)
    except Exception as e:
        return [_issue("P1", "Sitemap could not be read", str(e)[:100], "Check the sitemap URL is reachable.", sm_url)]
    if r["status"] != 200:
        return [_issue("P1", "No sitemap found", f"{sm_url} returned HTTP {r['status']}.",
                       "Publish sitemap.xml and list it in robots.txt.", sm_url, "Medium")]
    locs = {_norm(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r["text"])}
    out = []
    if not sitemaps:
        out.append(_issue("P2", "Sitemap not listed in robots.txt", "Crawlers find it faster when listed.",
                          f"Add 'Sitemap: {sm_url}' to robots.txt.", sm_url))
    if "<sitemapindex" not in r["text"]:  # an index points to other files; do not judge pages against it
        for p in pages:
            if _norm(p) not in locs:
                out.append(_issue("P2", "Page missing from sitemap", "Key page not listed.", "Add it to sitemap.xml.", p))
    return out


def audit_client(client: dict, fetch=fetch_full, max_pages=10) -> list[dict]:
    default = client.get("default_page", "https://" + parser.norm_domain(client["domain"]))
    pages = client.get("pages_to_audit") or [default, *client.get("target_pages", {}).values()]
    pages = list(dict.fromkeys(pages))[:max_pages]
    out = []
    for url in pages:
        try:
            resp = fetch(url)
        except Exception as e:
            out.append(_issue("P0", "Page unreachable", str(e)[:100], "Check the page is online and not blocking our checker.", url))
            continue
        out += audit_response(url, resp)
        if resp["status"] < 400:
            out += aeo.audit(resp["text"], url)
    out += audit_sitemap(client["domain"], pages, fetch)
    return out
