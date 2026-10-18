"""Check whether the client's robots.txt lets AI search crawlers in (BRD 6.3)."""
from urllib import robotparser
from urllib.parse import urlparse

import requests

# Search/answer crawlers matter most for citations; training crawlers are reported separately.
SEARCH_BOTS = ["OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "PerplexityBot", "Googlebot"]
TRAINING_BOTS = ["GPTBot", "Google-Extended", "anthropic-ai", "CCBot"]
UA = "SentinelBot/1.0 (+AI visibility monitoring)"


def default_fetch(url: str, timeout=15) -> tuple[int, str]:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
    return r.status_code, r.text


def check_robots(domain: str, fetch=default_fetch) -> dict:
    """Returns {'status': 'ok'|'no_robots'|'error', 'search': {bot: bool}, 'training': {bot: bool}, 'blocked_search': [...]}."""
    host = domain if "//" in domain else "https://" + domain
    url = f"{urlparse(host).scheme}://{urlparse(host).netloc}/robots.txt"
    try:
        code, text = fetch(url)
    except Exception as e:
        return {"status": "error", "error": str(e)[:150], "search": {}, "training": {}, "blocked_search": []}
    if code == 404:
        allowed = {b: True for b in SEARCH_BOTS + TRAINING_BOTS}
        return {"status": "no_robots", "search": {b: True for b in SEARCH_BOTS},
                "training": {b: True for b in TRAINING_BOTS}, "blocked_search": [], "url": url}
    if code >= 400:
        return {"status": "error", "error": f"HTTP {code}", "search": {}, "training": {}, "blocked_search": []}
    rp = robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    root = f"{urlparse(host).scheme}://{urlparse(host).netloc}/"
    search = {b: rp.can_fetch(b, root) for b in SEARCH_BOTS}
    training = {b: rp.can_fetch(b, root) for b in TRAINING_BOTS}
    return {"status": "ok", "search": search, "training": training,
            "blocked_search": [b for b, ok in search.items() if not ok], "url": url}
