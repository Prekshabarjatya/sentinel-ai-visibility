"""AEO checks: is the page shaped so an answer engine can lift a clean answer from it?

Rules of thumb used (from common snippet practice, not guarantees): a question heading followed by a
direct answer of roughly 30-70 words; FAQ and HowTo content marked up with matching schema.
"""
import json
from html.parser import HTMLParser

from . import pagegap

SKIP = {"script", "style", "noscript", "svg"}


class _A(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []            # ("h", text) / ("p", text) in page order
        self.ol_items = []          # item counts of each ordered list
        self.jsonld = []
        self._cur = None; self._buf = []; self._ol = None; self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP: self._skip += 1
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._cur = "jsonld"; self._buf = []
        elif tag in ("h1", "h2", "h3", "p"):
            self._cur = tag; self._buf = []
        elif tag == "ol": self._ol = 0
        elif tag == "li" and self._ol is not None: self._ol += 1

    def handle_endtag(self, tag):
        if tag in SKIP and self._skip: self._skip -= 1
        if self._cur == tag or (self._cur == "jsonld" and tag == "script"):
            s = " ".join("".join(self._buf).split())
            if self._cur == "jsonld" and s:
                try: self.jsonld.append(json.loads(s))
                except ValueError: pass
            elif s: self.blocks.append(("h" if self._cur != "p" else "p", s))
            self._cur = None
        if tag == "ol" and self._ol is not None:
            self.ol_items.append(self._ol); self._ol = None

    def handle_data(self, d):
        if self._cur and not (self._skip and self._cur != "jsonld"): self._buf.append(d)


def _issue(sev, title, why, action, url):
    return {"priority": sev, "title": title, "why": why, "action": action, "effort": "Medium",
            "page": url, "automated": False}


def audit(html: str, url: str) -> list[dict]:
    a = _A(); a.feed(html or "")
    types = set()
    for j in a.jsonld: types |= pagegap._types(j)
    out = []

    q_idx = [i for i, (k, t) in enumerate(a.blocks) if k == "h" and t.endswith("?")]
    if not q_idx:
        out.append(_issue("P1", "No question-style headings", "Answer engines match buyer questions to headings.",
                          "Add headings phrased as buyer questions, each followed by a direct answer.", url))
    else:
        bad = []
        for i in q_idx:
            nxt = next((t for k, t in a.blocks[i + 1:i + 3] if k == "p"), "")
            n = len(nxt.split())
            if not 30 <= n <= 70:
                bad.append(f"'{a.blocks[i][1][:50]}' ({n} words)")
        if bad:
            out.append(_issue("P2", "Answers under question headings are the wrong length",
                              f"{len(bad)} of {len(q_idx)}: " + "; ".join(bad[:3]),
                              "Open each with a self-contained answer of about 30-70 words, then add detail.", url))
        if len(q_idx) >= 3 and "FAQPage" not in types:
            out.append(_issue("P1", "FAQ content has no FAQPage schema", f"{len(q_idx)} question headings found.",
                              "Add FAQPage JSON-LD that matches the visible questions and answers.", url))
    if any(n >= 3 for n in a.ol_items) and "HowTo" not in types:
        out.append(_issue("P2", "Step-by-step list has no HowTo schema", "An ordered list of 3+ steps was found.",
                          "If the page is a how-to, add HowTo JSON-LD; otherwise ignore this.", url))
    first_p = next((t for k, t in a.blocks if k == "p" and len(t.split()) >= 10), "")
    if not 30 <= len(first_p.split()) <= 70:
        out.append(_issue("P2", "Opening paragraph is not a snippet-ready summary",
                          f"First substantial paragraph: {len(first_p.split())} words.",
                          "Start with a 30-70 word plain summary of what this page answers.", url))
    return out
