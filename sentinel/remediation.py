"""Drift detection and remediation DRAFTS.

Nothing here publishes anything. Drafts go to a review queue (JSON file); a named human
approves them in the queue, and only then may a separate, explicit publish step run (not
included in Phase 1). Draft text is built only from client-supplied facts.
"""
import json
import time
from pathlib import Path

DROP_POINTS = 20.0   # drop vs 4-week average
CONSECUTIVE = 2      # cycles in a row


def detect_drops(history: list[dict], current_by_prompt: dict) -> list[str]:
    """history: previous cycle records [{'sov_by_prompt': {...}}, ...] oldest first (needs >= 4 cycles).
    Returns prompts whose SoV fell >= DROP_POINTS below the 4-cycle average for CONSECUTIVE cycles."""
    if len(history) < 4 + (CONSECUTIVE - 1):
        return []
    flagged = []
    for prompt, now in current_by_prompt.items():
        recent = [h["sov_by_prompt"].get(prompt) for h in history]
        if any(v is None for v in recent):
            continue
        base_window = recent[-(4 + CONSECUTIVE - 1):-(CONSECUTIVE - 1)] if CONSECUTIVE > 1 else recent[-4:]
        base = sum(base_window) / len(base_window)
        prior_low = all(v <= base - DROP_POINTS for v in recent[-(CONSECUTIVE - 1):]) if CONSECUTIVE > 1 else True
        if prior_low and now <= base - DROP_POINTS:
            flagged.append(prompt)
    return flagged


def draft_patch(client: dict, prompt: str, competitor: str | None) -> dict:
    facts = client.get("approved_facts", [])
    if len(facts) < 2:
        return {"prompt": prompt, "status": "blocked",
                "reason": "Need at least 2 client-approved facts in config to draft a factual summary."}
    s1 = f"{client['brand']} {facts[0].rstrip('.')}."
    s2 = f"{facts[1].rstrip('.')}."
    s2 = s2[0].upper() + s2[1:]
    s3 = (f"Teams comparing options for \"{prompt}\" can see {client['brand']}'s details at "
          f"https://{client['domain']}.")
    return {
        "prompt": prompt,
        "competitor_cited": competitor,
        "bluf_summary": " ".join([s1, s2, s3]),
        "json_ld": {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": client["brand"],
            "url": f"https://{client['domain']}",
            "description": facts[0].rstrip("."),
        },
        "status": "pending_review",
        "approver": client.get("approver"),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Draft only. Contains no comparative claims about competitors. Not published.",
    }


def enqueue(queue_path, drafts: list[dict]):
    p = Path(queue_path)
    existing = json.loads(p.read_text()) if p.exists() else []
    existing.extend(drafts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(existing, indent=2))
