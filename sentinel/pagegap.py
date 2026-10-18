"""Compare the client's page with pages AI engines actually cited, and list what the client lacks."""
import json
import re
from html.parser import HTMLParser
from statistics import median

SKIP = {"script", "style", "noscript", "svg"}


class _P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.text, self.title, self.meta_desc = [], [], "", ""
        self.h1 = self.h2 = self.tables = self.lists = 0
        self.q_headings = 0
        self.paras, self.jsonld = [], []
        self._cur, self._buf = None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.stack.append(tag)
        if tag == "meta" and (a.get("name") or "").lower() == "description":
            self.meta_desc = a.get("content") or ""
        if tag == "h1": self.h1 += 1
        if tag in ("h2", "h3"): self.h2 += 1
        if tag == "table": self.tables += 1
        if tag in ("ul", "ol"): self.lists += 1
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._cur = "jsonld"; self._buf = []
        elif tag in ("title", "p", "h2", "h3"):
            self._cur = tag; self._buf = []

    def handle_endtag(self, tag):
        if self._cur == tag or (self._cur == "jsonld" and tag == "script"):
            s = "".join(self._buf).strip()
            if self._cur == "title": self.title = s
            elif self._cur == "p" and s: self.paras.append(s)
            elif self._cur in ("h2", "h3") and s.endswith("?"): self.q_headings += 1
            elif self._cur == "jsonld" and s:
                try: self.jsonld.append(json.loads(s))
                except ValueError: pass
            self._cur = None
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self._cur: self._buf.append(data)
        if not (set(self.stack) & SKIP): self.text.append(data)


def _types(obj):
    out = set()
    if isinstance(obj, list):
        for o in obj: out |= _types(o)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if isinstance(t, str): out.add(t)
        elif isinstance(t, list): out |= {x for x in t if isinstance(x, str)}
        for v in obj.values():
            if isinstance(v, (list, dict)): out |= _types(v)
    return out


def analyze_html(html: str) -> dict:
    p = _P()
    p.feed(html or "")
    words = len(re.findall(r"\w+", " ".join(p.text)))
    first = next((x for x in p.paras if len(x.split()) >= 15), "")
    types = set()
    for j in p.jsonld: types |= _types(j)
    return {
        "has_title": bool(p.title), "has_meta_description": bool(p.meta_desc), "has_h1": p.h1 > 0,
        "word_count": words,
        "direct_answer_early": 20 <= len(first.split()) <= 70,
        "has_table": p.tables > 0, "has_list": p.lists > 0,
        "question_headings": p.q_headings,
        "has_jsonld": bool(types), "schema_types": sorted(types),
        "has_faq_schema": "FAQPage" in types,
        "has_org_schema": bool(types & {"Organization", "LocalBusiness", "Corporation"}),
        "numbers": len(re.findall(r"\b\d[\d,.%]*\b", " ".join(p.paras))),
    }


def visible_text(html: str, max_words: int = 700) -> str:
    """Visible page text, trimmed, for content review."""
    p = _P()
    p.feed(html or "")
    return " ".join(" ".join(p.text).split()[:max_words])


# feature -> (human label, fix text)
BOOL_GAPS = {
    "direct_answer_early": ("No short direct answer near the top",
                            "Add a 2-3 sentence plain-language summary of what you offer and who it is for, right under the H1."),
    "has_faq_schema": ("No FAQ structured data",
                       "Add a FAQ section answering the buyer question in plain language, with FAQPage JSON-LD."),
    "has_org_schema": ("No Organization structured data",
                       "Add Organization JSON-LD (name, URL, description, sameAs profiles)."),
    "has_table": ("No comparison or spec table", "Add a table of key facts or a comparison buyers can scan."),
    "has_meta_description": ("No meta description", "Write a specific meta description stating what the page covers."),
}


def find_gaps(client_page: dict, competitor_pages: list[dict]) -> list[dict]:
    gaps = []
    if not competitor_pages:
        return [{"feature": k, "label": v[0], "action": v[1], "evidence": "Not compared (no competitor pages fetched)"}
                for k, v in BOOL_GAPS.items() if not client_page.get(k)]
    n = len(competitor_pages)
    for k, (label, action) in BOOL_GAPS.items():
        have = sum(1 for c in competitor_pages if c.get(k))
        if not client_page.get(k) and have >= max(1, (n + 1) // 2):
            gaps.append({"feature": k, "label": label, "action": action,
                         "evidence": f"{have} of {n} cited pages have it; yours does not"})
    med = median(c["word_count"] for c in competitor_pages)
    if med and client_page["word_count"] < 0.6 * med:
        gaps.append({"feature": "word_count", "label": "Thinner than cited pages",
                     "action": "Expand with specific facts, use cases, numbers and examples that answer the buyer question.",
                     "evidence": f"Your page: {client_page['word_count']} words; cited pages median: {int(med)}"})
    cq = median(c["question_headings"] for c in competitor_pages)
    if cq >= 2 and client_page["question_headings"] < cq / 2:
        gaps.append({"feature": "question_headings", "label": "Few question-style headings",
                     "action": "Add headings phrased as the questions buyers ask, each followed by a direct answer.",
                     "evidence": f"Cited pages median {int(cq)} question headings; yours {client_page['question_headings']}"})
    return gaps
