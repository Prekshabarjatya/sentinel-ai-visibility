"""Weekly cycle: query all engines x prompts x samples in parallel, parse, aggregate, log, draft.

Usage:
    python -m sentinel.run clients/acme.json --mock            # no API keys, no cost
    python -m sentinel.run clients/acme.json --samples 3       # real engines (set API keys in env)
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import audit, backlinks, crawlers, fixes, ga4, gsc, impact, metrics, parser, providers, remediation, review, seo

ENGINES = ["openai", "anthropic", "gemini", "perplexity"]


def _one(provider, prompt, sample, client, log):
    region = client.get("region", "us")
    lang = client.get("language", "en")
    try:
        text, cited = provider.ask(prompt, region, lang)
        parsed = parser.parse_response(text, cited, client)
        row = {"prompt": prompt, "engine": provider.name, "sample": sample, "ok": True, **parsed}
        log.record(client=client["domain"], engine=provider.name, prompt=prompt, sample=sample,
                   status="ok", client_cited=parsed["client_cited"], urls=parsed["urls"])
    except Exception as e:  # never let one failure stop the cycle
        row = {"prompt": prompt, "engine": provider.name, "sample": sample, "ok": False,
               "error": str(e)[:200]}
        log.record(client=client["domain"], engine=provider.name, prompt=prompt, sample=sample,
                   status="error", error=str(e)[:200])
    return row


def run_cycle(client: dict, data_dir: Path, samples=3, mock=False, workers=8, engines=None,
              optimize=False, fetch=None, llm=None, seo_audit=False, fetch_full=None, ga4_csv=None, gsc_csv=None, backlinks_csv=None) -> dict:
    data_dir = Path(data_dir)
    slug = parser.norm_domain(client["domain"]).replace(".", "_")
    log = audit.AuditLog(data_dir / slug / "audit.jsonl")
    provs = providers.build(engines or client.get("engines", ENGINES), mock=mock, client=client)

    jobs = [(p, pr, s) for p in provs for pr in client["prompts"] for s in range(samples)]
    runs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, p, pr, s, client, log) for p, pr, s in jobs]
        for f in as_completed(futs):
            runs.append(f.result())

    agg = metrics.aggregate(runs)
    by_prompt = metrics.sov_by_prompt(runs)

    hist_path = data_dir / slug / "history.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    flagged = remediation.detect_drops(history, by_prompt)

    drafts = []
    if client.get("remediation_opt_in") and flagged:
        for pr in flagged:
            comps = [r["competitors_cited"][0] for r in runs
                     if r["prompt"] == pr and r.get("ok") and r["competitors_cited"] and not r["client_cited"]]
            drafts.append(remediation.draft_patch(client, pr, comps[0] if comps else None))
        remediation.enqueue(data_dir / slug / "review_queue.json", drafts)

    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "samples": samples,
              "metrics": agg, "sov_by_prompt": by_prompt, "flagged": flagged,
              "drafts_queued": len(drafts)}

    if optimize:
        fetch = fetch or crawlers.default_fetch
        robots = crawlers.check_robots(client["domain"], fetch)
        fixes_list = fixes.build_fix_list(client, runs, by_prompt, robots, fetch, llm=llm)
        record["robots"] = robots
        record["fixes"] = fixes_list
        (data_dir / slug).mkdir(parents=True, exist_ok=True)
        (data_dir / slug / "fixes.json").write_text(json.dumps(fixes_list, indent=2))
        log.record(client=client["domain"], status="optimize", fixes=len(fixes_list),
                   blocked_bots=robots.get("blocked_search", []))
    if seo_audit:
        extra = seo.audit_client(client, fetch_full or seo.fetch_full)
        record["fixes"] = fixes.merge(record.get("fixes", []), extra)
        (data_dir / slug).mkdir(parents=True, exist_ok=True)
        (data_dir / slug / "fixes.json").write_text(json.dumps(record["fixes"], indent=2))
        log.record(client=client["domain"], status="seo_audit", issues=len(extra))
    if ga4_csv:
        record["ga4"] = ga4.summarize(ga4.parse_csv(Path(ga4_csv).read_text(encoding="utf-8-sig")))
    if gsc_csv:
        paths = gsc_csv if isinstance(gsc_csv, (list, tuple)) else [gsc_csv]
        rows = gsc.merge_rows(*[gsc.parse_csv(Path(x).read_text(encoding="utf-8-sig")) for x in paths])
        record["gsc"] = gsc.analyze(rows, client["prompts"])
    if backlinks_csv:
        paths = backlinks_csv if isinstance(backlinks_csv, (list, tuple)) else [backlinks_csv]
        rows = backlinks.merge(*[backlinks.parse_csv(Path(x).read_text(encoding="utf-8-sig")) for x in paths])
        record["backlinks"] = backlinks.analyze(rows, client, fixes.cited_third_party_domains(runs, client))

    applied_path = data_dir / slug / "applied.json"
    if applied_path.exists():
        record["impact"] = impact.compute_impact(history + [record], json.loads(applied_path.read_text()))
    history.append(record)
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist_path.write_text(json.dumps(history, indent=2))
    (data_dir / slug / "latest_report.md").write_text(render_report(client, record))
    return record


def render_report(client, rec) -> str:
    m = rec["metrics"]
    lines = [
        f"# AI Citation Report: {client['brand']}",
        f"Generated {rec['ts']} | {m['runs']} successful runs ({m['failed_runs']} failed) | "
        f"{rec['samples']} samples per prompt per engine",
        "",
        f"**Share of Voice:** {m['sov']}% (spread between samples: +/-{m['sov_spread']} points)",
        f"**Competitor overlap (competitor cited, you omitted):** {m['competitor_overlap']}%",
        f"**Tone when cited:** {m['sentiment']}",
        "",
        "| Engine | SoV % |", "| --- | --- |",
        *[f"| {e} | {v} |" for e, v in sorted(m["sov_by_engine"].items())],
        "",
        f"**Sources cited across runs:** {len(m['attribution_map'])}",
        f"**Prompts flagged for review:** {len(rec['flagged'])} | **Drafts awaiting approval:** {rec['drafts_queued']}",
        "",
        "_Results are measurements of AI answers at the time of each run; answers change without notice._",
    ]
    if "robots" in rec:
        rb = rec["robots"]
        if rb["status"] == "error":
            lines += ["", f"**AI crawler access:** could not check ({rb.get('error', '')})"]
        else:
            ok = [b for b, v in rb["search"].items() if v]
            lines += ["", f"**AI crawler access:** allowed: {', '.join(ok) or 'none'}"
                          + (f" | **blocked: {', '.join(rb['blocked_search'])}**" if rb["blocked_search"] else "")]
    if "fixes" in rec:
        lines += ["", fixes.render_fixes(rec["fixes"])]
    if rec.get("ga4"):
        lines += ["", ga4.render(rec["ga4"])]
    if rec.get("gsc"):
        lines += ["", gsc.render(rec["gsc"])]
    if rec.get("backlinks"):
        lines += ["", backlinks.render(rec["backlinks"])]
    if rec.get("impact"):
        lines += ["", impact.render_impact(rec["impact"])]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("client_config")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--optimize", action="store_true",
                    help="also check AI crawler access, compare pages and build the fix list (needs internet)")
    ap.add_argument("--seo", action="store_true", help="technical SEO + AEO audit of key pages (needs internet)")
    ap.add_argument("--ga4", metavar="CSV", help="GA4 Traffic acquisition CSV export: report AI-referral traffic")
    ap.add_argument("--gsc", metavar="CSV", action="append",
                    help="Search Console or Bing Webmaster Queries CSV (repeat the flag to combine both)")
    ap.add_argument("--backlinks", metavar="CSV", action="append",
                    help="Inbound-links CSV from Ahrefs Webmaster Tools or Bing Webmaster Tools (repeat to combine)")
    ap.add_argument("--llm-review", action="store_true",
                    help="with --optimize: ask a model whether your page answers each losing prompt (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--mark-applied", metavar="FIX_ID", help="record that the client applied this fix, then exit")
    ap.add_argument("--applied-on", metavar="YYYY-MM-DD", help="date the fix went live (default: today)")
    a = ap.parse_args()
    client = json.loads(Path(a.client_config).read_text())
    if a.mark_applied:
        base = Path(a.data_dir) / parser.norm_domain(client["domain"]).replace(".", "_")
        fl = json.loads((base / "fixes.json").read_text())
        fx = next((f for f in fl if f["id"] == a.mark_applied), None)
        if not fx:
            raise SystemExit(f"No fix with id {a.mark_applied} in {base / 'fixes.json'}")
        print("Recorded:", impact.mark_applied(base / "applied.json", fx, a.applied_on))
        return
    llm = review.default_llm() if (a.optimize and a.llm_review) else None
    rec = run_cycle(client, a.data_dir, samples=a.samples, mock=a.mock, optimize=a.optimize, llm=llm,
                    seo_audit=a.seo, ga4_csv=a.ga4, gsc_csv=a.gsc, backlinks_csv=a.backlinks)
    print(render_report(client, rec))


if __name__ == "__main__":
    main()
