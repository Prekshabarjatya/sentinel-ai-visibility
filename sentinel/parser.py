"""Response parsing: brand/domain detection, citation extraction, sentiment."""
import re
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s)\]>\"'<,]+", re.I)
POSITIVE = {"best", "leading", "recommended", "top", "excellent", "trusted", "popular", "strong"}
NEGATIVE = {"avoid", "poor", "worst", "unreliable", "lacking", "limited", "complaints", "outdated"}


def norm_domain(value: str) -> str:
    """'https://www.Acme.com/x' or 'acme.com' -> 'acme.com'."""
    value = value.strip().lower()
    if "//" not in value:
        value = "//" + value
    host = urlparse(value).netloc or value
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host


def extract_urls(text: str, extra_urls=()) -> list[str]:
    seen, out = set(), []
    for u in list(URL_RE.findall(text or "")) + list(extra_urls or []):
        u = u.rstrip(".,;")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def domain_in_urls(domain: str, urls) -> bool:
    d = norm_domain(domain)
    for u in urls:
        h = norm_domain(u)
        if h == d or h.endswith("." + d):
            return True
    return False


def mentions(text: str, brand: str, domain: str, urls) -> bool:
    """True if the brand name appears as a word, or the domain appears in text or cited URLs."""
    text = text or ""
    if brand and re.search(r"(?<!\w)" + re.escape(brand) + r"(?!\w)", text, re.I):
        return True
    if domain and norm_domain(domain) in text.lower():
        return True
    return domain_in_urls(domain, urls) if domain else False


def sentiment(text: str, brand: str) -> str:
    """Crude tone flag from sentences that mention the brand: Positive / Neutral / Negative."""
    pos = neg = 0
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        if brand and brand.lower() in sent.lower():
            words = set(re.findall(r"[a-z]+", sent.lower()))
            pos += len(words & POSITIVE)
            neg += len(words & NEGATIVE)
    return "Positive" if pos > neg else "Negative" if neg > pos else "Neutral"


def parse_response(text: str, cited_urls, client: dict) -> dict:
    urls = extract_urls(text, cited_urls)
    client_hit = mentions(text, client["brand"], client["domain"], urls)
    comp_hits = [c["brand"] for c in client.get("competitors", [])
                 if mentions(text, c["brand"], c["domain"], urls)]
    return {
        "client_cited": client_hit,
        "competitors_cited": comp_hits,
        "urls": urls,
        "sentiment": sentiment(text, client["brand"]) if client_hit else "Neutral",
    }
