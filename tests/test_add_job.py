"""Adding a job by hand: off-board postings, board postings, and the poller
keeping a hand-added job open even when the filters would skip it. No
network and no model: the board feed, the page fetch and the extractor are
stubs."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import jd_fetch  # noqa: E402
from jobs import boards, common, watch  # noqa: E402
from test_watch import store  # noqa: E402

POSTING = "About the role. " + "We design tools for teams. " * 20
META = lambda text, title, url: {"company": "Northwind", "role": "Staff Designer",
                                 "location": "Austin, TX"}


def test_off_board_posting_is_added_once(monkeypatch):
    monkeypatch.setattr(jd_fetch, "fetch", lambda url: {"text": POSTING, "title": "Staff Designer | Northwind"})
    monkeypatch.setattr(boards, "add_extra", lambda c, u: False)
    with store():
        url = "https://careers.northwind.test/jobs/staff-designer"
        jid, existed = watch.add_job(url, meta=META)
        assert jid.startswith("manual:careers.northwind.test:") and not existed
        j = watch.view_one(jid)
        assert j["company"] == "Northwind" and j["title"] == "Staff Designer"
        assert j["added"] and j["status"] == "new"
        # Austin is outside the user's cities, but the user picked it, so it is listed.
        assert any(x["id"] == jid for x in watch.list_jobs("all"))
        assert watch.add_job(url, meta=META) == (jid, True)


def test_pasted_text_needs_no_link(monkeypatch):
    with store():
        jid, _ = watch.add_job("", POSTING, meta=META)
        assert jid.startswith("manual:pasted:")


def test_bad_input_is_refused():
    with store():
        for url, text in (("", "too short"), ("not a link", "")):
            try:
                watch.add_job(url, text, meta=META)
            except watch.AddError:
                continue
            raise AssertionError("accepted %r" % url)


def test_defense_role_is_refused_when_the_filter_is_on(monkeypatch):
    with store():
        with open(common.CONFIG_PATH, "w") as f:
            f.write('{"skip_defense": true}')
        try:
            watch.add_job("", "Hold an active security clearance. " + POSTING,
                          meta=lambda *a: {"company": "Acme", "role": "Designer"})
        except watch.AddError as e:
            assert "defense" in str(e)
        else:
            raise AssertionError("defense role was added")


def test_board_posting_survives_the_filters(monkeypatch):
    feed = [{"pid": "7012345", "title": "Brand Marketing Lead", "url": "https://job-boards.greenhouse.io/acme/jobs/7012345",
             "location": "London", "remote": False, "posted": "", "updated": "", "text": POSTING}]
    monkeypatch.setitem(watch.FETCH, "greenhouse", lambda slug: feed)
    monkeypatch.setattr(boards, "add_extra", lambda c, u: False)
    monkeypatch.setattr(boards, "all_boards", lambda: [{"ats": "greenhouse", "slug": "acme", "name": "Acme Inc",
                                                        "employer": "acme", "verified": True}])
    monkeypatch.setattr(watch, "config", lambda: {"cities": ["San Francisco"], "allow_us_remote": True})
    with store():
        jid, _ = watch.add_job("https://job-boards.greenhouse.io/acme/jobs/7012345?gh_src=x")
        assert jid == "greenhouse:acme:7012345"
        assert watch.view_one(jid)["company"] == "Acme Inc"
        # A run that reads the board keeps it open: not a design title, not
        # one of the user's cities, but the user added it.
        watch._run(notify=False, score=False, log=lambda *a: None)
        con = watch.db()
        assert con.execute("SELECT closed FROM jobs WHERE id=?", (jid,)).fetchone()[0] is None
        con.close()
        feed.clear()
        watch._run(notify=False, score=False, log=lambda *a: None)
        con = watch.db()
        assert con.execute("SELECT closed FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        con.close()


def test_a_strong_match_notifies_on_the_mac_only_when_asked(monkeypatch):
    got = []
    monkeypatch.setattr(watch, "mac_notify", lambda t, b: got.append((t, b)))
    watch.tell([{"id": "acme/1", "company": "A", "title": "T"}], {"notify_mac": False})
    assert got == []
    watch.tell([{"id": "a", "company": "A", "title": "T"},
                {"id": "b", "company": "B", "title": "T"}], {"notify_mac": True})
    assert got == [("2 new strong matches", "A: T; B: T")]