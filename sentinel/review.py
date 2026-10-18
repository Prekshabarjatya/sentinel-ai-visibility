"""LLM content review: does the client's page actually answer the buyer question, compared with cited pages?

The structural checks in pagegap.py cannot judge quality. This adds one LLM call per losing prompt.
`llm` is any function str -> str, so it can be a real model or a fake in tests.
The model is told to use only facts present on the client's page, so suggestions cannot invent claims.
"""
import json
import re


def default_llm():
    """Uses the Anthropic provider (needs ANTHROPIC_API_KEY). Swap in any other callable."""
    from .providers import AnthropicProvider
    prov = AnthropicProvider()
    return lambda prompt: prov.ask(prompt)[0]


def _build_prompt(question, client_text, competitor_texts):
    comps = "\n\n".join(f"[COMPETITOR PAGE {i + 1}]\n{t}" for i, t in enumerate(competitor_texts))
    return (
        "You are reviewing web pages for how well they answer a buyer question, so AI answer engines can cite them.\n"
        f"Buyer question: {question}\n\n[CLIENT PAGE]\n{client_text}\n\n{comps}\n\n"
        "Score each page 1-5 for how directly and specifically it answers the question "
        "(1 = does not, 5 = clear, specific, complete). Then give up to 3 concrete suggestions to improve the CLIENT page. "
        "Rules: use only facts that appear on the client page; if a suggestion needs a fact that is not there, "
        "write '[client to supply: <what>]' instead of inventing it. No claims about competitors.\n"
        "Reply with JSON only: "
        '{"client_score": int, "competitor_scores": [int, ...], "suggestions": [str, ...]}'
    )


def review_page(question: str, client_text: str, competitor_texts: list[str], llm) -> dict:
    if not client_text.strip() or not competitor_texts:
        return {"status": "skipped", "reason": "no page text to compare"}
    try:
        raw = llm(_build_prompt(question, client_text, competitor_texts))
    except Exception as e:
        return {"status": "unavailable", "reason": str(e)[:120]}
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
        score = int(d["client_score"])
        comp = [int(x) for x in d["competitor_scores"]]
        sugg = [str(s)[:300] for s in d.get("suggestions", [])][:3]
        if not (1 <= score <= 5) or not comp or any(not 1 <= c <= 5 for c in comp):
            raise ValueError("score out of range")
    except Exception:
        return {"status": "unparseable", "reason": "model reply was not valid JSON"}
    return {"status": "ok", "answers_directly": score, "competitor_avg": round(sum(comp) / len(comp), 1),
            "suggestions": sugg}
