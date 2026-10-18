"""Track which fixes the client applied and whether Share of Voice on that prompt moved afterwards.

AI answers are noisy and several fixes may land at once, so this reports an association with a noise
threshold, never proof of cause.
"""
import json
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, pstdev

SETTLE_DAYS = 7      # crawlers need time to re-read a changed page
MIN_BEFORE = 3       # weekly cycles needed before the fix
MIN_AFTER = 2        # weekly cycles needed after the settle period
MIN_CHANGE = 10.0    # points; below this we never call it a change


def mark_applied(applied_path, fix: dict, on: str | None = None) -> dict:
    p = Path(applied_path)
    items = json.loads(p.read_text()) if p.exists() else []
    if any(i["id"] == fix["id"] for i in items):
        return next(i for i in items if i["id"] == fix["id"])
    item = {"id": fix["id"], "title": fix["title"], "prompt": fix.get("prompt"),
            "page": fix.get("page"), "date": on or date.today().isoformat()}
    items.append(item)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, indent=2))
    return item


def compute_impact(history: list[dict], applied: list[dict]) -> list[dict]:
    out = []
    for a in applied:
        prompt = a.get("prompt")  # no prompt (e.g. a crawler fix) -> measure overall Share of Voice

        def val(h, prompt=prompt):
            return h["sov_by_prompt"].get(prompt) if prompt else h.get("metrics", {}).get("sov")
        start = date.fromisoformat(a["date"])
        settle = start + timedelta(days=SETTLE_DAYS)
        before = [val(h) for h in history if date.fromisoformat(h["ts"][:10]) < start and val(h) is not None][-4:]
        after = [val(h) for h in history if date.fromisoformat(h["ts"][:10]) >= settle and val(h) is not None]
        if len(before) < MIN_BEFORE:
            out.append({**a, "verdict": "no baseline", "detail": f"Only {len(before)} cycles before the fix."})
            continue
        if len(after) < MIN_AFTER:
            out.append({**a, "verdict": "too early",
                        "detail": f"{len(after)} cycles since {settle.isoformat()}; need {MIN_AFTER}."})
            continue
        b, af = mean(before), mean(after)
        change = af - b
        threshold = max(MIN_CHANGE, 2 * pstdev(before))
        verdict = "improved" if change >= threshold else "declined" if change <= -threshold else "no clear change"
        out.append({**a, "verdict": verdict, "before": round(b, 1), "after": round(af, 1),
                    "change": round(change, 1), "threshold": round(threshold, 1),
                    "detail": ("Overall SoV " if not prompt else "") + f"{b:.1f}% to {af:.1f}% ({change:+.1f} pts; needs {threshold:.1f}+ to count)"})
    return out


def render_impact(results: list[dict]) -> str:
    if not results:
        return ""
    lines = ["## Results of applied fixes",
             "_Association, not proof: AI answers vary and other changes may overlap._", ""]
    for r in results:
        lines.append(f"- `{r['id']}` **{r['title']}** (applied {r['date']}): **{r['verdict']}**. {r['detail']}")
    return "\n".join(lines)
