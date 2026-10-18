"""Metric aggregation. Each 'run' is one parsed response (prompt x engine x sample)."""
from collections import Counter, defaultdict
from statistics import pstdev


def aggregate(runs: list[dict]) -> dict:
    """runs: dicts with prompt, engine, sample, client_cited, competitors_cited, urls, sentiment, ok."""
    ok = [r for r in runs if r.get("ok", True)]
    n = len(ok)
    if n == 0:
        return {"runs": 0, "sov": 0.0, "sov_by_engine": {}, "sov_spread": 0.0,
                "competitor_overlap": 0.0, "attribution_map": [], "sentiment": "Neutral",
                "failed_runs": len(runs)}

    sov = 100.0 * sum(r["client_cited"] for r in ok) / n
    overlap = 100.0 * sum(1 for r in ok if r["competitors_cited"] and not r["client_cited"]) / n

    by_engine = defaultdict(list)
    for r in ok:
        by_engine[r["engine"]].append(r["client_cited"])
    sov_by_engine = {e: round(100.0 * sum(v) / len(v), 2) for e, v in by_engine.items()}

    # spread: std-dev of per-sample SoV (sample index = repeat number), so clients see run-to-run noise
    by_sample = defaultdict(list)
    for r in ok:
        by_sample[r["sample"]].append(r["client_cited"])
    per_sample = [100.0 * sum(v) / len(v) for v in by_sample.values()]
    spread = pstdev(per_sample) if len(per_sample) > 1 else 0.0

    urls = sorted({u for r in ok for u in r["urls"]})
    tones = Counter(r["sentiment"] for r in ok if r["client_cited"])
    tone = tones.most_common(1)[0][0] if tones else "Neutral"

    return {
        "runs": n,
        "failed_runs": len(runs) - n,
        "sov": round(sov, 2),
        "sov_spread": round(spread, 2),
        "sov_by_engine": sov_by_engine,
        "competitor_overlap": round(overlap, 2),
        "attribution_map": urls,
        "sentiment": tone,
    }


def sov_by_prompt(runs: list[dict]) -> dict:
    d = defaultdict(list)
    for r in runs:
        if r.get("ok", True):
            d[r["prompt"]].append(r["client_cited"])
    return {p: round(100.0 * sum(v) / len(v), 2) for p, v in d.items()}
