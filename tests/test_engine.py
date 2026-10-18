import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel import audit, metrics, parser, remediation, run  # noqa: E402

CLIENT = json.loads((Path(__file__).resolve().parent.parent / "clients" / "acme.json").read_text())


def test_norm_domain():
    assert parser.norm_domain("https://www.Acme.com/x") == "acme.com"
    assert parser.norm_domain("acme.com") == "acme.com"


def test_mentions_word_boundary():
    assert parser.mentions("Acme is great", "Acme", "acme.com", [])
    assert not parser.mentions("Acmeology is a field", "Acme", "acme.com", [])
    assert parser.mentions("see docs", "Acme", "acme.com", ["https://app.acme.com/x"])


def test_parse_response():
    r = parser.parse_response("Rival is popular. See https://rival.com/a.", [], CLIENT)
    assert r["client_cited"] is False and r["competitors_cited"] == ["Rival"]


def test_metrics():
    runs = [
        {"prompt": "p", "engine": "a", "sample": 0, "client_cited": True, "competitors_cited": [], "urls": ["u1"], "sentiment": "Positive", "ok": True},
        {"prompt": "p", "engine": "a", "sample": 1, "client_cited": False, "competitors_cited": ["Rival"], "urls": ["u2"], "sentiment": "Neutral", "ok": True},
        {"prompt": "p", "engine": "b", "sample": 0, "client_cited": False, "competitors_cited": [], "urls": [], "sentiment": "Neutral", "ok": True},
        {"prompt": "p", "engine": "b", "sample": 1, "ok": False},
    ]
    m = metrics.aggregate(runs)
    assert m["runs"] == 3 and m["failed_runs"] == 1
    assert m["sov"] == round(100 / 3, 2)
    assert m["competitor_overlap"] == round(100 / 3, 2)
    assert m["attribution_map"] == ["u1", "u2"]


def test_audit_chain_detects_tamper():
    with tempfile.TemporaryDirectory() as d:
        log = audit.AuditLog(Path(d) / "a.jsonl")
        log.record(x=1)
        log.record(x=2)
        assert audit.verify(Path(d) / "a.jsonl")
        p = Path(d) / "a.jsonl"
        p.write_text(p.read_text().replace('"x": 1', '"x": 9'))
        assert not audit.verify(p)


def test_drop_detection():
    hist = [{"sov_by_prompt": {"p": v}} for v in (60, 60, 60, 60, 30)]
    assert remediation.detect_drops(hist, {"p": 30}) == ["p"]
    assert remediation.detect_drops(hist, {"p": 55}) == []
    assert remediation.detect_drops(hist[:3], {"p": 30}) == []


def test_draft_requires_facts_and_never_publishes():
    d = remediation.draft_patch({**CLIENT, "approved_facts": []}, "p", "Rival")
    assert d["status"] == "blocked"
    d = remediation.draft_patch(CLIENT, "p", "Rival")
    assert d["status"] == "pending_review" and len(d["bluf_summary"].split(". ")) == 3
    assert "Rival" not in d["bluf_summary"]


def test_end_to_end_mock():
    with tempfile.TemporaryDirectory() as d:
        rec = run.run_cycle(CLIENT, d, samples=3, mock=True)
        # 4 engines x 3 prompts x 3 samples = 36 runs
        assert rec["metrics"]["runs"] == 36
        assert 0 <= rec["metrics"]["sov"] <= 100
        assert audit.verify(Path(d) / "acme_com" / "audit.jsonl")
        assert len((Path(d) / "acme_com" / "audit.jsonl").read_text().splitlines()) == 36
        assert (Path(d) / "acme_com" / "latest_report.md").exists()


ROBOTS_BLOCK = "User-agent: OAI-SearchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
THIN = "<html><head><title>Acme</title></head><body><h1>Acme</h1><p>Welcome.</p></body></html>"
RICH = """<html><head><title>Rival</title><meta name="description" content="x">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[]}</script>
<script type="application/ld+json">{"@type":"Organization","name":"Rival"}</script></head><body><h1>Rival</h1>
<p>Rival is a project management tool for startups that helps teams plan work, track tasks and ship faster with clear owners and deadlines for everyone.</p>
<h2>How does Rival work?</h2><p>""" + "It tracks 500 tasks and 20 projects. " * 60 + """</p><h2>Is Rival free?</h2><table><tr><td>Plan</td></tr></table></body></html>"""


def fake_fetch(url):
    if url.endswith("/robots.txt"):
        return 200, ROBOTS_BLOCK
    if "rival.com" in url:
        return 200, RICH
    return 200, THIN


def test_robots_blocks_search_bot():
    from sentinel import crawlers
    r = crawlers.check_robots("acme.com", fake_fetch)
    assert r["blocked_search"] == ["OAI-SearchBot"] and r["search"]["ClaudeBot"] is True
    assert crawlers.check_robots("acme.com", lambda u: (404, ""))["status"] == "no_robots"
    assert crawlers.check_robots("acme.com", lambda u: (_ for _ in ()).throw(OSError("x")))["status"] == "error"


def test_page_analysis_and_gaps():
    from sentinel import pagegap
    thin, rich = pagegap.analyze_html(THIN), pagegap.analyze_html(RICH)
    assert not thin["has_faq_schema"] and rich["has_faq_schema"] and rich["has_org_schema"]
    assert rich["direct_answer_early"] and rich["question_headings"] == 2 and rich["has_table"]
    feats = {g["feature"] for g in pagegap.find_gaps(thin, [rich, rich])}
    assert {"has_faq_schema", "direct_answer_early", "word_count", "has_table"} <= feats


def test_fix_list_priorities():
    from sentinel import crawlers, fixes
    runs = [{"prompt": "p1", "engine": "e", "sample": i, "ok": True, "client_cited": False,
             "competitors_cited": ["Rival"], "urls": ["https://rival.com/features", "https://review-site.com/top"],
             "sentiment": "Neutral"} for i in range(3)]
    robots = crawlers.check_robots("acme.com", fake_fetch)
    fl = fixes.build_fix_list(CLIENT, runs, {"p1": 0.0, "p2": 80.0}, robots, fake_fetch)
    assert fl[0]["priority"] == "P0" and "OAI-SearchBot" in fl[0]["title"]
    assert any(f["priority"] == "P1" and f["prompt"] == "p1" for f in fl)
    assert any(f["priority"] == "P2" and "review-site.com" in f["title"] for f in fl)
    assert not any(f.get("prompt") == "p2" for f in fl)  # winning prompt gets no page fixes
    assert "Nothing is changed on your site" in fixes.render_fixes(fl)


def test_optimize_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        rec = run.run_cycle(CLIENT, d, samples=2, mock=True, optimize=True, fetch=fake_fetch)
        assert rec["fixes"][0]["priority"] == "P0"
        assert (Path(d) / "acme_com" / "fixes.json").exists()
        assert "Recommended fixes" in (Path(d) / "acme_com" / "latest_report.md").read_text()
        assert audit.verify(Path(d) / "acme_com" / "audit.jsonl")


RUNS = [{"prompt": "p1", "engine": "e", "sample": i, "ok": True, "client_cited": False, "competitors_cited": ["Rival"],
         "urls": ["https://rival.com/features", "https://other.io/x"], "sentiment": "Neutral"} for i in range(3)]


def test_review_parsing():
    from sentinel import review
    good = lambda p: 'Sure:\n{"client_score": 2, "competitor_scores": [4, 5], "suggestions": ["Lead with the answer.", "a", "b", "c"]}'
    r = review.review_page("q", "client text", ["c1", "c2"], good)
    assert r["status"] == "ok" and r["answers_directly"] == 2 and r["competitor_avg"] == 4.5 and len(r["suggestions"]) == 3
    assert review.review_page("q", "t", ["c"], lambda p: "not json")["status"] == "unparseable"
    assert review.review_page("q", "t", ["c"], lambda p: '{"client_score": 9, "competitor_scores": [3]}')["status"] == "unparseable"
    boom = lambda p: (_ for _ in ()).throw(RuntimeError("down"))
    assert review.review_page("q", "t", ["c"], boom)["status"] == "unavailable"
    assert review.review_page("q", "", ["c"], good)["status"] == "skipped"


def test_prompt_forbids_invented_facts():
    from sentinel import review
    assert "client to supply" in review._build_prompt("q", "a", ["b"])


def test_unfetchable_competitor_pages_are_reported():
    from sentinel import crawlers, fixes

    def partial(url):
        if "other.io" in url:
            raise OSError("403 blocked")
        return fake_fetch(url)
    fl = fixes.build_fix_list(CLIENT, RUNS, {"p1": 0.0}, crawlers.check_robots("acme.com", partial), partial)
    assert any("could not be fetched" in f["title"] and "other.io" in f["why"] for f in fl)
    assert any("compared 1 of 2" in f["why"] for f in fl if f["title"] == "No FAQ structured data")


def test_llm_review_adds_content_fix_and_ids_are_stable():
    from sentinel import crawlers, fixes
    llm = lambda p: '{"client_score": 1, "competitor_scores": [4], "suggestions": ["Open with a direct answer."]}'
    rb = crawlers.check_robots("acme.com", fake_fetch)
    fl = fixes.build_fix_list(CLIENT, RUNS, {"p1": 0.0}, rb, fake_fetch, llm=llm)
    item = [f for f in fl if "directly enough" in f["title"]]
    assert item and "1/5" in item[0]["why"] and "direct answer" in item[0]["action"]
    ids = [f["id"] for f in fl]
    assert len(set(ids)) == len(ids)
    assert ids == [f["id"] for f in fixes.build_fix_list(CLIENT, RUNS, {"p1": 0.0}, rb, fake_fetch, llm=llm)]


def test_impact_verdicts():
    from sentinel import impact
    mk = lambda d, v: {"ts": d + "T00:00:00Z", "sov_by_prompt": {"p": v}}
    hist = [mk("2026-09-06", 20), mk("2026-09-13", 22), mk("2026-09-20", 18), mk("2026-09-27", 20),
            mk("2026-10-11", 55), mk("2026-10-18", 60)]
    a = {"id": "x", "title": "t", "prompt": "p", "date": "2026-10-02"}
    r = impact.compute_impact(hist, [a])[0]
    assert r["verdict"] == "improved" and r["change"] > 30
    assert impact.compute_impact(hist[:5], [a])[0]["verdict"] == "too early"
    assert impact.compute_impact(hist, [{**a, "date": "2026-09-10"}])[0]["verdict"] == "no baseline"
    flat = [mk("2026-09-06", 20), mk("2026-09-13", 22), mk("2026-09-20", 18), mk("2026-09-27", 20),
            mk("2026-10-11", 24), mk("2026-10-18", 21)]
    assert impact.compute_impact(flat, [a])[0]["verdict"] == "no clear change"
    overall = [{"ts": h["ts"], "sov_by_prompt": h["sov_by_prompt"], "metrics": {"sov": h["sov_by_prompt"]["p"]}} for h in hist]
    r2 = impact.compute_impact(overall, [{**a, "prompt": None}])[0]
    assert r2["verdict"] == "improved" and r2["detail"].startswith("Overall SoV")


def test_mark_applied_and_report_section():
    from sentinel import impact
    with tempfile.TemporaryDirectory() as d:
        fx = {"id": "abc12345", "title": "No FAQ structured data", "prompt": "p", "page": "https://acme.com"}
        impact.mark_applied(Path(d) / "applied.json", fx, "2026-10-02")
        impact.mark_applied(Path(d) / "applied.json", fx, "2026-10-03")  # duplicate ignored
        items = json.loads((Path(d) / "applied.json").read_text())
        assert len(items) == 1 and items[0]["date"] == "2026-10-02"
        assert "Association, not proof" in impact.render_impact(impact.compute_impact([], items))


BAD_PAGE = ("<html><head><meta name='robots' content='noindex, follow'><title>Hi</title></head>"
            "<body><h1>A</h1><h1>B</h1><h3>Skipped</h3><img src='a.png'><img src='b.png' alt='ok'><p>Short.</p></body></html>")
ANSWER = "Acme is a project management tool for startups that lets teams plan work, assign owners, track deadlines and report progress in one place without spreadsheets or long meetings."
GOOD_PAGE = ("<html lang='en'><head><title>Acme project management for startup teams</title>"
             "<meta name='description' content='Acme helps startup teams plan work, assign owners and track deadlines in one simple place.'>"
             "<meta name='viewport' content='width=device-width'><link rel='canonical' href='https://acme.com/'>"
             "<script type='application/ld+json'>{\"@type\":\"FAQPage\"}</script></head><body><h1>Acme</h1>"
             f"<p>{ANSWER}</p><h2>What is Acme?</h2><p>{ANSWER}</p><h2>How much does it cost?</h2><p>{ANSWER}</p>"
             f"<h2>Is it secure?</h2><p>{ANSWER}</p><a href='/a'>a</a><a href='/b'>b</a><a href='/c'>c</a></body></html>")


def ffull(pages, sitemap_pages=("https://acme.com/",), robots="User-agent: *\nAllow: /\nSitemap: https://acme.com/sitemap.xml\n"):
    def f(url):
        if url.endswith("/robots.txt"):
            return {"status": 200, "text": robots, "headers": {}, "final_url": url, "redirects": 0}
        if url.endswith("/sitemap.xml"):
            body = "".join(f"<url><loc>{u}</loc></url>" for u in sitemap_pages)
            return {"status": 200, "text": f"<urlset>{body}</urlset>", "headers": {}, "final_url": url, "redirects": 0}
        if url in pages:
            return {"status": 200, "text": pages[url], "headers": {}, "final_url": url, "redirects": 0}
        return {"status": 404, "text": "", "headers": {}, "final_url": url, "redirects": 0}
    return f


def test_seo_flags_problems_and_passes_good_page():
    from sentinel import seo
    bad = seo.audit_response("https://acme.com/", {"status": 200, "text": BAD_PAGE, "headers": {}, "final_url": "https://acme.com/", "redirects": 0})
    titles = {i["title"] for i in bad}
    assert {"Page is set to noindex", "No canonical URL", "More than one H1", "Heading levels skip",
            "No mobile viewport tag", "Many images lack alt text"} <= titles
    assert next(i for i in bad if "noindex" in i["title"])["priority"] == "P0"
    good = seo.audit_response("https://acme.com/", {"status": 200, "text": GOOD_PAGE, "headers": {}, "final_url": "https://acme.com/", "redirects": 0})
    assert not any(i["priority"] == "P0" for i in good)
    hdr = seo.audit_response("https://acme.com/", {"status": 200, "text": GOOD_PAGE, "headers": {"x-robots-tag": "noindex"}, "final_url": "https://acme.com/", "redirects": 0})
    assert any("noindex" in i["title"] for i in hdr)
    assert seo.audit_response("https://acme.com/x", {"status": 404, "text": "", "headers": {}, "final_url": "x", "redirects": 0})[0]["priority"] == "P0"


def test_sitemap_checks():
    from sentinel import seo
    ok = seo.audit_sitemap("acme.com", ["https://acme.com/"], ffull({}))
    assert ok == []
    miss = seo.audit_sitemap("acme.com", ["https://acme.com/", "https://acme.com/new"], ffull({}))
    assert [i["page"] for i in miss] == ["https://acme.com/new"]
    none = seo.audit_sitemap("acme.com", [], lambda u: {"status": 404, "text": "", "headers": {}, "final_url": u, "redirects": 0})
    assert none[0]["title"] == "No sitemap found"


def test_aeo_checks():
    from sentinel import aeo
    assert not any(i["priority"] == "P1" for i in aeo.audit(GOOD_PAGE, "u"))
    bad = {i["title"] for i in aeo.audit(BAD_PAGE, "u")}
    assert "No question-style headings" in bad and "Opening paragraph is not a snippet-ready summary" in bad
    nofaq = GOOD_PAGE.replace('<script type=\'application/ld+json\'>{"@type":"FAQPage"}</script>', "")
    assert "FAQ content has no FAQPage schema" in {i["title"] for i in aeo.audit(nofaq, "u")}
    steps = "<html><body><h2>How to start?</h2><p>short</p><ol><li>a</li><li>b</li><li>c</li></ol></body></html>"
    t = {i["title"] for i in aeo.audit(steps, "u")}
    assert "Step-by-step list has no HowTo schema" in t and "Answers under question headings are the wrong length" in t


GA4_CSV = """# ----------
# Traffic acquisition: Session source / medium
# Start date: 20260901
Session source / medium,Landing page,Sessions,Key events
google / organic,/,"1,200",30
chatgpt.com / referral,/pricing,40,5
perplexity.ai / referral,/blog/a,25,2
(direct) / (none),/,300,10
gemini.google.com / referral,/,10,0
"""


def test_ga4_ai_referrals():
    from sentinel import ga4
    s = ga4.summarize(ga4.parse_csv(GA4_CSV))
    assert s["total"] == 1575 and s["ai"] == 75 and s["share"] == round(100 * 75 / 1575, 2)
    assert s["by_source"] == {"ChatGPT": 40, "Perplexity": 25, "Gemini": 10} and s["key_events"] == 7
    assert "ChatGPT" in ga4.render(s)
    try:
        ga4.parse_csv("a,b\n1,2")
        assert False
    except ValueError:
        pass


GSC_CSV = """Top queries,Clicks,Impressions,CTR,Position
how to track project tasks,5,900,0.6%,8.2
best project management software,40,1500,2.7%,4.1
acme login,300,400,75%,1.0
what is a kanban board,2,300,0.7%,6.5
project planning tips,1,20,5%,12
"""


def test_gsc_opportunities():
    from sentinel import gsc
    a = gsc.analyze(gsc.parse_csv(GSC_CSV), tracked_prompts=["best project management software"])
    assert [r["query"] for r in a["striking"]] == ["how to track project tasks", "what is a kanban board"]
    assert a["low_ctr"][0]["query"] == "how to track project tasks"
    assert "best project management software" not in a["suggested_prompts"]
    assert a["suggested_prompts"][0] == "how to track project tasks"
    assert "add these as tracked prompts" in gsc.render(a)


def test_seo_end_to_end_with_exports():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "ga4.csv").write_text(GA4_CSV)
        (Path(d) / "gsc.csv").write_text(GSC_CSV)
        pages = {"https://acme.com": BAD_PAGE}
        c = {**CLIENT, "default_page": "https://acme.com", "target_pages": {}}
        rec = run.run_cycle(c, d, samples=1, mock=True, seo_audit=True, fetch_full=ffull(pages),
                            ga4_csv=Path(d) / "ga4.csv", gsc_csv=Path(d) / "gsc.csv")
        assert rec["fixes"][0]["priority"] == "P0" and "noindex" in rec["fixes"][0]["title"]
        assert all("id" in f for f in rec["fixes"])
        rep = (Path(d) / "acme_com" / "latest_report.md").read_text()
        assert "AI referral traffic" in rep and "Search Console opportunities" in rep and "Recommended fixes" in rep
        assert audit.verify(Path(d) / "acme_com" / "audit.jsonl")


AHREFS_CSV = """Referring page URL,Target URL,Link anchor,Nofollow
https://blog.example.org/post,https://acme.com/,Acme,false
https://blog.example.org/post2,https://acme.com/pricing,best project software,false
https://news.site.com/a,https://acme.com/,click here,true
https://acme.com/self,https://acme.com/,self link,false
"""
BING_LINKS_CSV = """Source URL,Target URL,Anchor Text
https://blog.example.org/post,https://acme.com/,Acme
https://directory.io/acme,https://acme.com/,https://acme.com
"""
BING_QUERIES_CSV = """Query,Clicks,Impressions,CTR,Avg. Position
how to track project tasks,3,100,3%,6.0
new bing only question?,1,80,1.2%,9.0
"""


def test_backlinks_parse_merge_analyze():
    from sentinel import backlinks
    a_rows = backlinks.parse_csv(AHREFS_CSV)
    b_rows = backlinks.parse_csv(BING_LINKS_CSV)
    assert a_rows[2]["nofollow"] is True and a_rows[0]["domain"] == "blog.example.org"
    merged = backlinks.merge(a_rows, b_rows)
    assert len(merged) == 6 - 1  # the same blog.example.org/post -> acme.com/ link appears in both exports
    an = backlinks.analyze(merged, CLIENT, cited_domains=["review-site.com", "blog.example.org", "acme.com"])
    assert an["links"] == 4 and an["referring_domains"] == 3  # self link excluded
    assert an["cited_but_not_linking"] == ["review-site.com"]
    assert an["anchor_mix"]["brand"] == 1 and an["anchor_mix"]["generic"] == 1 and an["anchor_mix"]["naked URL"] == 1
    assert any("Only 3 referring domains" in f for f in an["flags"])
    assert "outreach targets" in backlinks.render(an)
    assert backlinks.anchor_type("", "Acme", "acme.com") == "empty or image"
    try:
        backlinks.parse_csv("a,b\n1,2")
        assert False
    except ValueError:
        pass


def test_bing_queries_merge_with_google():
    from sentinel import gsc
    g = gsc.parse_csv(GSC_CSV)
    b = gsc.parse_csv(BING_QUERIES_CSV)
    assert b[0]["position"] == 6.0 and b[1]["query"] == "new bing only question?"
    m = {r["query"]: r for r in gsc.merge_rows(g, b)}
    r = m["how to track project tasks"]
    assert r["impressions"] == 1000 and r["clicks"] == 8
    assert abs(r["position"] - (8.2 * 900 + 6.0 * 100) / 1000) < 1e-9
    assert "new bing only question?" in m


def test_backlinks_end_to_end():
    from sentinel import fixes
    runs = [{"prompt": "p", "ok": True, "urls": ["https://review-site.com/x", "https://acme.com/", "https://rival.com/"]}]
    assert fixes.cited_third_party_domains(runs, CLIENT) == ["review-site.com"]
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.csv").write_text(AHREFS_CSV)
        (Path(d) / "b.csv").write_text(BING_LINKS_CSV)
        (Path(d) / "q1.csv").write_text(GSC_CSV)
        (Path(d) / "q2.csv").write_text(BING_QUERIES_CSV)
        rec = run.run_cycle(CLIENT, d, samples=1, mock=True, backlinks_csv=[Path(d) / "a.csv", Path(d) / "b.csv"],
                            gsc_csv=[Path(d) / "q1.csv", Path(d) / "q2.csv"])
        assert rec["backlinks"]["referring_domains"] == 3
        assert "new bing only question?" in rec["gsc"]["suggested_prompts"]
        assert "Backlink profile" in (Path(d) / "acme_com" / "latest_report.md").read_text()



def test_twin_summary_counts_overlap():
    from sentinel import twin
    rows = {"original": [{"prompt": "a", "named": False, "competitors": ["Dettol"]},
                         {"prompt": "a", "named": True, "competitors": ["Dettol"]}],
            "fixed": [{"prompt": "a", "named": True, "competitors": []}, {"prompt": "a", "error": "HTTP 429"}]}
    s = twin.summarize(rows)
    assert s["original"]["sov"] == 50.0 and s["original"]["lost_to_competitor"] == 50.0
    assert s["original"]["competitors_named"] == {"Dettol": 2}
    assert s["fixed"]["sov"] == 100.0 and s["fixed"]["failed"] == 1


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                print("FAIL", name)
                traceback.print_exc()
    sys.exit(1 if fails else 0)
