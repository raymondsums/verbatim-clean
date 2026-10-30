"""Tests for the job watcher's fit tiers. Run: python3 tests/test_watch.py

Nothing here calls the model or a job board. The database and the
preferences file are pointed at a temp dir, so the real ones are never
touched, and the model call is a stub that counts how often it is asked.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs import common  # noqa: E402
from jobs import fit  # noqa: E402
from jobs import track_record  # noqa: E402
from jobs import watch  # noqa: E402

ON = {"skip_defense": True}  # the defense filter is off unless asked for


MASTER = """## Contact

- Sam Example, sam@example.test

## Positioning

- Senior product designer, 8 years, consumer fintech and logistics apps

## Settled decisions

- Remote or Bay Area only
"""


class store:
    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        self.old = (watch.DB_PATH, fit.PREFS_PATH, fit.MASTER_PATH, fit._run_cli,
                    track_record.RECORD_PATH, fit.PROFILE_PATH, common.CONFIG_PATH)
        watch.DB_PATH = os.path.join(self.dir, "jobs.sqlite")
        fit.PREFS_PATH = os.path.join(self.dir, "prefs.json")
        fit.MASTER_PATH = os.path.join(self.dir, "master.md")
        with open(fit.MASTER_PATH, "w", encoding="utf-8") as f:
            f.write(MASTER)
        track_record.RECORD_PATH = os.path.join(self.dir, "record.json")
        fit.PROFILE_PATH = os.path.join(self.dir, "profile.json")
        common.CONFIG_PATH = os.path.join(self.dir, "config.json")
        self.calls = []
        return self

    def __exit__(self, *exc):
        (watch.DB_PATH, fit.PREFS_PATH, fit.MASTER_PATH, fit._run_cli,
         track_record.RECORD_PATH, fit.PROFILE_PATH, common.CONFIG_PATH) = self.old

    def stub(self, tiers):
        """The model answers with these tiers, by job id."""
        def run(prompt):
            self.calls.append(prompt)
            return {"jobs": [{"id": k, "tier": t, "reason": "Reason for %s" % k,
                              "gaps": ["healthcare"], "about": "Something",
                              "tags": ["ai tools"]} for k, t in tiers.items()
                             if "id: %s\n" % k in prompt]}
        fit._run_cli = run


def add(con, jid, title="Senior Product Designer", company="Acme", **extra):
    row = {"id": jid, "ats": "greenhouse", "slug": "acme", "employer": "acme",
           "company": company, "title": title, "url": "https://x.test/" + jid,
           "location": "San Francisco", "seniority": "senior", "posted": "",
           "first_seen": watch._now(), "last_seen": watch._now(),
           "status": "seen", "baseline": 1, "text": "A posting."}
    row.update(extra)
    con.execute("INSERT INTO jobs(%s) VALUES(%s)" % (",".join(row), ",".join("?" * len(row))),
                list(row.values()))
    con.commit()


def job(tier, tags, title="Senior Product Designer", company="Acme", **extra):
    j = {"tier": tier, "tags": json.dumps(tags), "title": title,
         "company": company, "about": "", "status": "seen"}
    j.update(extra)
    return j


# ── Tier storage

def test_a_job_is_scored_once_and_keeps_its_tier():
    with store() as s:
        con = watch.db()
        add(con, "a")
        add(con, "b")
        s.stub({"a": "strong", "b": "stretch"})
        assert watch.score_pending(con, log=lambda *_: None) == 2
        assert len(s.calls) == 1, "both jobs go in one batch"
        # The model would now say something else. It must not be asked.
        s.stub({"a": "stretch", "b": "strong"})
        assert watch.score_pending(con, log=lambda *_: None) == 0
        assert len(s.calls) == 1, s.calls
        rows = {j["id"]: j for j in watch.list_jobs("all")}
        assert rows["a"]["tier"] == "strong" and rows["b"]["tier"] == "stretch", rows
        assert rows["a"]["reason"] == "Reason for a"
        assert rows["a"]["gaps"] == ["healthcare"]
        con.close()


def test_a_job_missing_from_the_reply_is_retried_then_left():
    with store() as s:
        con = watch.db()
        add(con, "a")
        s.stub({})
        watch.score_pending(con, log=lambda *_: None)
        watch.score_pending(con, log=lambda *_: None)
        watch.score_pending(con, log=lambda *_: None)
        assert len(s.calls) == 2, "two tries, then it stops asking"
        assert watch.list_jobs("all")[0]["tier"] is None
        con.close()


def test_dismissed_and_closed_jobs_are_not_scored():
    with store() as s:
        con = watch.db()
        add(con, "a", status="dismissed")
        add(con, "b", closed=watch._now())
        s.stub({"a": "strong", "b": "strong"})
        assert watch.score_pending(con, log=lambda *_: None) == 0
        assert s.calls == []
        con.close()


def test_list_is_best_first():
    with store() as s:
        con = watch.db()
        add(con, "a")
        add(con, "b")
        add(con, "c")
        s.stub({"a": "stretch", "b": "strong", "c": "look"})
        watch.score_pending(con, log=lambda *_: None)
        assert [j["id"] for j in watch.list_jobs("all")] == ["b", "c", "a"]
        con.close()


# ── Interest adjustment

def test_a_preference_moves_a_job_one_step_and_says_why():
    tier, note = fit.adjust(job("look", ["ai tools"]), ["more AI tools"])
    assert tier == "strong", tier
    assert note == "Up from Worth a look: you asked for more AI tools.", note
    tier, note = fit.adjust(job("strong", ["ad tech", "enterprise"]), ["less ad tech"])
    assert tier == "look" and note.startswith("Down from Strong match"), (tier, note)


def test_a_preference_that_does_not_match_changes_nothing():
    assert fit.adjust(job("look", ["fintech"]), ["more AI tools", "less ad tech"]) == ("look", "")


def test_a_stretch_never_becomes_a_strong_match():
    tier, _ = fit.adjust(job("stretch", ["ai tools"]), ["more AI tools"])
    assert tier == "look", tier


def test_preferences_that_pull_both_ways_cancel():
    tier, note = fit.adjust(job("look", ["ai tools", "ad tech"]),
                            ["more AI tools", "less ad tech"])
    assert (tier, note) == ("look", "")


def test_learned_interest_needs_enough_evidence():
    acted = [job("look", ["crypto"], status="dismissed"),
             job("look", ["crypto"], status="dismissed")]
    learned = fit.learn(acted)
    # Two passes are not a pattern yet.
    assert fit.adjust(job("strong", ["crypto"]), [], learned) == ("strong", "")
    acted.append(job("look", ["crypto"], status="dismissed"))
    tier, note = fit.adjust(job("strong", ["crypto"]), [], fit.learn(acted))
    assert tier == "look", tier
    assert "you passed on 3 crypto jobs" in note, note


def test_learned_interest_lifts_one_step_from_what_he_acts_on():
    acted = [job("look", ["automotive"], status="applied"),
             job("look", ["automotive"], starred=1),
             job("look", ["automotive"], opened="2026-10-16T09:00:00")]
    tier, note = fit.adjust(job("look", ["automotive"]), [], fit.learn(acted))
    assert tier == "strong", tier
    assert "interest in 3 automotive jobs" in note, note


def test_own_own_preference_beats_what_was_learned():
    acted = [job("look", ["ai tools"], status="dismissed") for _ in range(4)]
    tier, note = fit.adjust(job("look", ["ai tools"]), ["more AI tools"], fit.learn(acted))
    assert tier == "strong" and "you asked for" in note, (tier, note)


def test_preferences_are_stored_cleaned():
    with store():
        saved = fit.save_prefs(["  more AI tools ", "", "More AI tools", "less ad tech"])
        assert saved == ["more AI tools", "less ad tech"], saved
        assert fit.load_prefs() == saved


def test_preferences_count_on_the_list_from_day_one():
    with store() as s:
        con = watch.db()
        add(con, "a")
        s.stub({"a": "look"})
        watch.score_pending(con, log=lambda *_: None)
        assert watch.list_jobs("all")[0]["tier"] == "look"
        fit.save_prefs(["more AI tools"])
        j = watch.list_jobs("all")[0]
        assert j["tier"] == "strong" and j["base_tier"] == "look", j
        assert j["note"], j
        con.close()


# ── Scores

def test_a_score_stays_inside_its_tier_and_moves_with_it():
    assert fit.in_band(97, "look") == 79 and fit.in_band(10, "stretch") == 30
    assert fit.in_band(None, "strong") is None and fit.in_band(85, None) is None
    assert fit.moved_score(70, "look", "look") == 70
    up = fit.moved_score(79, "look", "strong")
    down = fit.moved_score(60, "look", "stretch")
    assert up == 99 and down == 30, (up, down)


def test_old_jobs_get_a_score_without_their_tier_changing():
    with store():
        con = watch.db()
        add(con, "a", tier="strong", reason="Close fit")
        add(con, "b", tier="look", reason="Level off")
        con.commit()
        old = fit.cli
        fit.cli = lambda prompt, system, schema: {"jobs": [
            {"id": "a", "score": 91}, {"id": "b", "score": 95}]}
        try:
            assert watch.score_backfill(con, log=lambda s: None) == 2
        finally:
            fit.cli = old
        rows = {r["id"]: r for r in watch.list_jobs("all")}
        con.close()
    assert (rows["a"]["tier"], rows["a"]["score"]) == ("strong", 91), rows["a"]
    assert (rows["b"]["tier"], rows["b"]["score"]) == ("look", 79), rows["b"]


# ── Pay

def test_pay_is_read_from_the_posting():
    assert watch.salary("Pay: $150,000 \u2014 $200,000 base") == "$150K - $200K"
    assert watch.salary("$180K - $220K") == "$180K - $220K"
    assert watch.salary("$157,300.00 - $203,500.00 in SF") == "$157.3K - $203.5K"
    assert watch.salary("$45.00 - $65.00 hourly, or $100,000 - $125,000") == "$100K - $125K"
    assert watch.salary("$67.69 - $81.44 per hour") == "$67.69 - $81.44/hr"
    assert watch.salary("Competitive pay and $5,000 - $10,000 stipend") == ""
    assert watch.salary("no pay listed") == ""


def test_shortlist_is_new_strong_and_look_topped_up_with_earlier_strong():
    with store() as s:
        con = watch.db()
        old = "2026-01-01T08:00:00"
        recent = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3 * 86400))
        add(con, "n1", baseline=0, text="x" * 500)
        add(con, "n2", baseline=0, ats="workday")
        add(con, "n3", baseline=0)
        add(con, "e1", baseline=0, first_seen=recent, company="Earlier Co")
        add(con, "e2", baseline=0, first_seen=recent)
        add(con, "b1", first_seen=old)          # baseline, never on the list
        s.stub({"n1": "strong", "n2": "look", "n3": "stretch",
                "e1": "strong", "e2": "look", "b1": "strong"})
        watch.score_pending(con, log=lambda *_: None)
        con.close()
        got = watch.shortlist()["jobs"]
        assert [j["id"] for j in got] == ["n1", "n2", "e1"], got
        assert [j["earlier"] for j in got] == [False, False, True]
        assert got[0]["can_tailor"] and not got[1]["can_tailor"]
        assert got[0]["has_questions"] and not got[1]["has_questions"]
        assert all(j["warm_path"] is None and j["tier"] != "stretch" for j in got)
        assert set(got[0]) >= {"company", "role", "score", "tier", "link", "tailored"}


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
    print("%d passed" % n)


def test_freshness_lowers_old_postings():
    from jobs import fit
    assert fit.age_penalty(10) == 0 and fit.age_penalty(14) == 0
    assert 3.5 < fit.age_penalty(42) < 4.5 and 15.5 < fit.age_penalty(90) < 16.5
    assert fit.age_penalty(3650) <= fit.MAX_PTS
    # smooth: no jump where the pieces meet
    for d in (fit.GENTLE_DAYS, fit.STEEP_DAYS):
        assert abs(fit.age_penalty(d - 0.01) - fit.age_penalty(d + 0.01)) < 0.1
    assert fit.freshen(96, "strong", 7) == (96, "strong", "")
    score, tier, note = fit.freshen(96, "strong", 330)
    assert tier == "look" and score < 80 and note.startswith("Older posting")
    assert fit.freshen(96, "strong", 90)[1] == "strong"
    assert fit.freshen(62, "look", 400)[1] == "stretch"
    assert fit.freshen(None, "strong", 200)[1] == "look"
    # never raises a tier
    assert fit.freshen(50, "stretch", 400)[1] == "stretch"


def test_contract_roles_are_spotted():
    from jobs.common import is_contract
    assert is_contract("Product Designer (Contract)")
    assert is_contract("Freelance Visual Designer")
    assert is_contract("Product Designer", emptype="Contractor")
    assert is_contract("Product Designer", "This is a 6-month contract role with a chance to convert.")
    assert is_contract("UX Designer", "Temp-to-hire, onsite in SF.")
    assert not is_contract("Product Designer", emptype="Full time")
    assert not is_contract("Product Designer", "We sign contracts with carriers. This is not a contract role.")
    assert not is_contract("Product Designer, Contracts Platform", "Benefits: 401k.")


def test_contract_as_a_product_area_is_not_a_contract_role():
    from jobs.common import is_contract
    assert not is_contract("Product Designer, Contract Orchestration")
    assert is_contract("Senior Product Designer, Contract")
    assert is_contract("Contract Product Designer")
    assert is_contract("Senior Product Designer (Webflow) Temp to Perm")


def _config(tmp_path, monkeypatch, **cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))


def test_lower_tags_sit_a_tier_lower(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, lower_tags=["developer tools"])
    j = {"tier": "strong", "tags": '["developer tools", "ai tools"]', "title": "Product Designer",
         "company": "X", "text": "", "about": "", "reason": ""}
    tier, note = fit.adjust(j)
    assert tier == "look" and "lower list" in note
    assert fit.adjust(j, ["more developer tools"])[0] == "strong"


def test_nothing_is_lowered_without_lower_tags(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    j = {"tier": "strong", "tags": '["developer tools"]', "title": "Product Designer",
         "company": "X", "text": "", "about": "", "reason": ""}
    assert fit.adjust(j)[0] == "strong"


def test_full_time_or_contract_stays():
    from jobs.common import is_contract
    assert not is_contract("Product Designer", emptype="Full-time / Contract")
    assert is_contract("UX/UI Designer", emptype="1099 CONTRACT")


def test_lower_tags_at_listed_labs_keep_their_tier(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, lower_tags=["developer tools"])
    labs = tmp_path / "labs.txt"
    labs.write_text("# example\nExample Labs\nBig Corp: Research Lab, Applied AI\n")
    monkeypatch.setattr(fit, "AI_LABS_PATH", str(labs))
    j = {"tier": "strong", "tags": '["developer tools"]', "title": "Product Designer, Developer Platform",
         "company": "Example Labs", "text": "", "about": "", "reason": ""}
    assert fit.adjust(j)[0] == "strong"
    big = dict(j, company="Big Corp", title="Product Designer, Developer Tools", text="")
    assert fit.adjust(big)[0] == "look"
    assert fit.adjust(dict(big, text="Join the Research Lab to build tools for researchers."))[0] == "strong"


def test_429_rests_a_board_and_backs_off():
    cool = {}
    assert watch._cool_after(cool, "workday:x", "HTTPError: 429 Client Error")
    first = cool["workday:x"]["wait"]
    assert first == watch.COOL_FIRST and cool["workday:x"]["until"] > 0
    watch._cool_after(cool, "workday:x", "HTTPError: 429 Client Error")
    assert cool["workday:x"]["wait"] == min(watch.COOL_MAX, first * 2)
    watch._cool_after(cool, "workday:x", "HTTPError: 429")
    assert cool["workday:x"]["wait"] == watch.COOL_MAX
    assert not watch._cool_after(cool, "workday:y", "HTTPError: 500")
    assert watch._cool_after(cool, "workday:x", None) and "workday:x" not in cool


def test_us_and_canada_postings_count_as_the_users_area():
    cfg = {"cities": ["San Francisco"], "allow_us_remote": True, "count_bare_united_states": True}
    assert watch.location_ok("US / Canada", False, cfg)
    assert watch.location_ok("United States & Canada", False, cfg)
    assert not watch.location_ok("Canada", False, cfg)


def test_remote_in_the_title_counts_as_remote():
    cfg = {"cities": ["San Francisco"], "allow_us_remote": True, "count_bare_united_states": True}
    title = "VP, User Experience and Design Innovation (Remote Eligible)"
    assert watch.location_ok("Bellevue, WA, USA", False, cfg, title)
    assert not watch.location_ok("Bellevue, WA, USA", False, cfg, "Staff Product Designer")
    assert not watch.location_ok("Toronto", False, cfg, "Product Designer (Remote, Canada)")
    assert not watch.location_ok("Anywhere", False, cfg,
                                 "Senior Product Designer (Fully Remote - anywhere in South America)")


def test_an_engineering_director_with_ux_in_the_title_is_not_design():
    from jobs.common import is_design_title
    assert not is_design_title("Sr. Director Eng, Seller User Experience")
    assert is_design_title("Sr. Staff Experience Designer - Payments")


def test_a_feed_job_already_open_elsewhere_is_a_dupe():
    with store():
        con = watch.db()
        con.execute("INSERT INTO jobs(id, ats, slug, employer, company, title, closed) "
                    "VALUES('greenhouse:umbrella:1','greenhouse','umbrella','umbrella','Umbrella, Inc.','Senior Product Designer', NULL)")
        p = {"title": "Senior Product Designer", "company": "Umbrella", "url": "https://himalayas.app/x"}
        assert watch._dupe(con, "himalayas", p, set())
        p2 = {"title": "Senior Product Designer", "company": "Wayneco", "url": "https://himalayas.app/y"}
        assert not watch._dupe(con, "himalayas", p2, set())
        p3 = {"title": "Staff Designer", "company": "Acme", "url": "",
              "links": ["https://jobs.ashbyhq.com/acme/123"]}
        assert watch._dupe(con, "serpapi", p3, {("ashby", "acme")})


def test_serpapi_counts_on_its_meter_and_stays_inside_its_caps(tmp_path, monkeypatch):
    from jobs import feeds
    cfg = {"serpapi_jobs": True, "roles": ["nurse"], "cities": ["Denver"],
           "serpapi_monthly_limit": 90}
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.setattr(feeds, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(feeds, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(feeds, "USAGE_PATH", str(tmp_path / "usage.json"))
    monkeypatch.setattr(feeds, "BUDGET_PATH", str(tmp_path / "serpapi_budget.json"))
    assert feeds.boards(cfg) == []
    assert feeds.fetch_serpapi("", cfg) == []
    (tmp_path / ".env").write_text("OTHER=1\nSERPAPI_KEY=k\n")
    assert feeds.serpapi_key(cfg) == "k"
    assert ("nurse", "Denver") in feeds.serp_queries(cfg)
    cycle = feeds._cycle_start(feeds.time.localtime())
    (tmp_path / "serpapi_budget.json").write_text(
        json.dumps({"cycle": cycle, "used": 32, "synced_at": "x"}))
    sent = []

    class R:
        status_code = 200
        def json(self):
            return {"jobs_results": []}
    monkeypatch.setattr(feeds.requests, "get", lambda *a, **k: sent.append(1) or R())
    for _ in range(6):
        feeds.fetch_serpapi("", cfg)
    assert len(sent) == feeds.PER_DAY
    state = json.loads((tmp_path / "serpapi_budget.json").read_text())
    assert state == {"cycle": cycle, "used": 32 + feeds.PER_DAY, "synced_at": "x"}
    # Past the monthly limit no search is made, whatever the day's count.
    (tmp_path / "usage.json").unlink()
    (tmp_path / "serpapi_budget.json").write_text(json.dumps({"cycle": cycle, "used": 90}))
    feeds.fetch_serpapi("", cfg)
    assert len(sent) == feeds.PER_DAY


def test_dribbble_titles_are_split_into_company_role_and_place(monkeypatch):
    from jobs import feeds
    rss = ("<rss><channel><item><title>Hooli is hiring for a position of Senior Product Designer in "
           "San Francisco, CA</title><dc:creator>Hooli</dc:creator><pubDate>Mon, 19 Oct 2026 13:33:31 -0400"
           "</pubDate><link>https://dribbble.com/jobs/311198-Senior?source=feed</link></item></channel></rss>")

    class R:
        text = rss
        def raise_for_status(self):
            pass
    monkeypatch.setattr(feeds.requests, "get", lambda *a, **k: R())
    (j,) = feeds.fetch_dribbble()
    assert (j["company"], j["title"], j["location"], j["pid"]) == (
        "Hooli", "Senior Product Designer", "San Francisco, CA", "311198")


def test_new_defense_names_are_blocked():
    from jobs.common import is_defense
    for name in ("Echodyne", "Armada", "Nominal", "Onebrief", "Primer", "Shield AI", "Helsing", "Code Metal",
                 "Vannevar", "Vannevar Labs, Inc.", "ICEYE", "Senra Systems", "Primer Technologies",
                 "shieldai", "anduril"):
        assert is_defense(name, "Product Designer", cfg=ON)


def test_a_yc_page_job_closes_once_its_own_board_has_it():
    with store():
        con = watch.db()
        con.execute("INSERT INTO jobs(id, ats, slug, employer, company, title, closed) "
                    "VALUES('ashby:padlet:1','ashby','padlet','padlet','Padlet','Product Designer', NULL)")
        assert watch._dupe(con, "yc", {"title": "Product Designer", "company": "Padlet", "url": ""}, set())


def test_wwr_titles_are_split_into_company_and_role(monkeypatch):
    from jobs import feeds
    rss = ("<rss><channel><item><title>Workiva: Senior Product Designer</title>"
           "<region>Anywhere in the World</region><type>Full-Time</type>"
           "<pubDate>Mon, 19 Oct 2026 13:33:31 +0000</pubDate>"
           "<link>https://weworkremotely.com/remote-jobs/workiva-senior-product-designer</link>"
           "</item></channel></rss>")

    class R:
        text = rss
        def raise_for_status(self):
            pass
    monkeypatch.setattr(feeds.requests, "get", lambda *a, **k: R())
    (j,) = feeds.fetch_wwr()
    assert (j["company"], j["title"], j["location"]) == ("Workiva", "Senior Product Designer",
                                                         "Remote, Anywhere in the World")


def test_yc_slug_needs_the_company_domain(monkeypatch):
    from jobs import discover
    monkeypatch.setattr(discover, "_texts", lambda ats, slug: ["Join us at padlet.com"] if ats == "ashby" else [])
    assert discover.yc_board({"name": "Padlet", "slug": "padlet", "website": "https://padlet.com"}) == ("ashby", "padlet")
    monkeypatch.setattr(discover, "_texts", lambda ats, slug: ["We are Speak Inc, a law firm"])
    assert discover.yc_board({"name": "Speak", "slug": "speak", "website": "https://usespeak.com"}) is None


def test_chip_and_plant_design_titles_are_not_design_roles():
    from jobs.common import is_design_title
    for t in ("Senior NVM Designer", "Senior ASIC Designer", "RTL Design Engineer",
              "Physical Design Lead", "Senior Circuit Designer", "Plant Design Manager"):
        assert not is_design_title(t), t
    assert is_design_title("Staff Product Designer, MarianaOS")


def test_a_name_guess_also_tries_the_domain_stem(monkeypatch):
    from jobs import discover
    tried = []
    monkeypatch.setattr(discover, "_texts", lambda ats, slug: tried.append(slug) or (
        ["Apply at withpulse.com"] if slug == "withpulse" else []))
    assert discover.yc_board({"name": "Pulse Health", "website": "https://www.withpulse.com"}) == ("ashby", "withpulse")


def test_portfolio_reads_stop_on_a_block_and_obey_robots(monkeypatch):
    import pytest
    from jobs import discover

    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text
        def raise_for_status(self):
            pass
    pages = {"https://a.test/robots.txt": R(200, "User-agent: *\nDisallow: /private/\n"),
             "https://a.test/ok": R(200, "fine"), "https://a.test/no": R(403)}
    monkeypatch.setattr(discover, "PAUSE", 0)
    monkeypatch.setattr(discover, "_robots", {})
    monkeypatch.setattr(discover.requests, "get", lambda url, **k: pages[url])
    assert discover._polite("https://a.test/ok") == "fine"
    with pytest.raises(discover.Blocked):
        discover._polite("https://a.test/private/x")
    with pytest.raises(discover.Blocked):
        discover._polite("https://a.test/no")


def test_spacex_and_xai_hide_only_their_defense_and_government_roles():
    from jobs.common import is_defense
    itar = "ITAR REQUIREMENTS: To conform to U.S. Government export regulations, applicant must be a U.S. citizen."
    assert not is_defense("SpaceX", "Product Designer, Starlink App", "Design the app. " + itar, cfg=ON)
    assert is_defense("SpaceX", "Product Designer, Starshield", itar, cfg=ON)
    assert is_defense("SpaceX", "UX Designer", "Work on classified programs. " + itar, cfg=ON)
    assert is_defense("SpaceX", "UX Designer", "Requires an active security clearance.", cfg=ON)
    assert is_defense("xAI", "Product Designer", "Grok for Government customers.", cfg=ON)
    assert not is_defense("xAI", "Product Designer", "Design Grok.", cfg=ON)
    assert not is_defense("Primerica", "Product Designer", cfg=ON)


def test_a_company_site_that_blocks_is_stopped_for_good(tmp_path, monkeypatch):
    import pytest
    from jobs import enterprise

    class R:
        def __init__(self, code, text="", ctype="text/html"):
            self.status_code, self.text, self.headers = code, text, {"content-type": ctype}
        def raise_for_status(self):
            pass
        def json(self):
            return {}
    calls = []
    monkeypatch.setattr(enterprise, "PAUSE", 0)
    monkeypatch.setattr(enterprise, "_robots", {})
    monkeypatch.setattr(enterprise, "BLOCKED_PATH", str(tmp_path / "blocked.json"))
    monkeypatch.setattr(enterprise.requests, "get", lambda url, **k: R(200, "User-agent: *\nAllow: /\n"))
    monkeypatch.setattr(enterprise.requests, "request", lambda m, url, **k: calls.append(url) or R(403))
    with pytest.raises(enterprise.Blocked):
        enterprise._req("GET", "https://jobs.example.com/x")
    with pytest.raises(enterprise.Blocked):
        enterprise._req("GET", "https://jobs.example.com/y")
    assert calls == ["https://jobs.example.com/x"]  # the second one never went out


def test_a_privacy_notice_about_national_security_authorities_is_not_defense():
    from jobs.common import is_defense
    notice = "this data may be accessible to the courts, law enforcement and national security authorities of such jurisdictions."
    assert not is_defense("Soylent", "Staff Product Designer", notice, cfg=ON)
    assert is_defense("Soylent", "Staff Product Designer", "Support our national security customers.", cfg=ON)
    assert not is_defense("SpaceX", "Product Designer", notice, cfg=ON)


def test_hardware_design_titles_are_noise_unless_they_say_product_or_ux():
    from jobs.common import is_design_title
    for t in ("Airframe Design Lead, Fuselage", "Senior CAD Designer", "Sr. Tooling Designer, Investment Casting",
              "Mechanical Design Lead", "Structural Design Engineer", "Packaging Engineer",
              "Senior Industrial Designer", "CMF Designer - Color, Material, and Finish (Starlink)"):
        assert not is_design_title(t), t
    for t in ("Industrial Designer, Product Experience", "Senior UX Industrial Designer", "Staff Product Designer"):
        assert is_design_title(t), t


def test_roles_and_exclude_from_config_decide_the_titles():
    from jobs.common import is_wanted_title
    cfg = {"roles": ["nurse", "registered nurse"], "exclude": ["travel"]}
    assert is_wanted_title("Registered Nurse, ICU", cfg)
    assert not is_wanted_title("Travel Nurse", cfg)
    assert not is_wanted_title("Senior Product Designer", cfg)
    # No roles: the example design test runs, and exclude still applies.
    assert is_wanted_title("Senior Product Designer", {})
    assert not is_wanted_title("Senior Product Designer", {"exclude": ["senior"]})


def test_remote_from_another_country():
    cfg = {"cities": ["Berlin"], "allow_remote": True, "remote_country": "Germany"}
    assert watch.location_ok("Berlin, Germany", False, cfg)
    assert watch.location_ok("Remote, Germany", True, cfg)
    assert watch.location_ok("Remote", True, cfg)
    assert watch.location_ok("Remote, worldwide", True, cfg)
    assert not watch.location_ok("Remote, United States", True, cfg)
    assert not watch.location_ok("Remote, Germany", True, dict(cfg, allow_remote=False))
