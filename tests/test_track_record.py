"""Tests for the track record signal. Run: python3 tests/test_track_record.py

Nothing here reads the real tracker or calls the model. The record and tag cache
are pointed at a temp dir, and the model call is a stub.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs import fit  # noqa: E402
from jobs import track_record as tr  # noqa: E402


class store:
    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        self.old = (tr.RECORD_PATH, tr.TAGS_PATH, fit.cli)
        tr.RECORD_PATH = os.path.join(self.dir, "record.json")
        tr.TAGS_PATH = os.path.join(self.dir, "tags.json")
        tr._cache.clear()
        self.calls = []
        return self

    def __exit__(self, *exc):
        tr.RECORD_PATH, tr.TAGS_PATH, fit.cli = self.old
        tr._cache.clear()

    def stub(self, tags):
        def run(prompt, system, schema):
            self.calls.append(prompt)
            return {"companies": [{"id": k, "tags": v} for k, v in tags.items()
                                  if "id: %s\n" % k in prompt]}
        fit.cli = run


def row(company, status="Applied", rejected_at=None, location=""):
    return {"company": company, "status": status, "rejected_at": rejected_at,
            "location": location, "role": "Product Designer", "link": ""}


def job(tier, tags, location="San Francisco, CA", **extra):
    j = {"tier": tier, "tags": json.dumps(tags), "title": "Senior Product Designer",
         "company": "Acme", "employer": "acme", "about": "", "status": "seen",
         "location": location}
    j.update(extra)
    return j


def record(areas=None, locations=None, apps=150, calls=4):
    return {"overall": {"apps": apps, "calls": calls},
            "areas": areas or {}, "locations": locations or {}}


# ── Call counting

def test_what_counts_as_a_call():
    assert tr.got_call(row("a", "Phone Screen"))
    assert tr.got_call(row("a", "Interviewing"))
    assert tr.got_call(row("a", "Onsite"))
    assert tr.got_call(row("a", "Offer"))
    assert tr.got_call(row("a", "Rejected", "Phone Screen"))
    assert not tr.got_call(row("a", "Rejected", "Applied"))
    assert not tr.got_call(row("a", "Rejected"))
    assert not tr.got_call(row("a", "No response"))
    assert not tr.got_call(row("a", "Applied"))


def test_saved_rows_are_not_applications():
    rows = [row("a", "Saved"), row("b", "No response"), row("c", "Rejected", "Phone Screen")]
    rec = tr.count(rows, {})
    assert rec["overall"]["apps"] == 2 and rec["overall"]["calls"] == 1, rec


def test_counts_per_area_and_location_type():
    rows = [row("Soylent", "Rejected", "Phone Screen", "On-site"),
            row("Soylent", "No response", location="Hybrid"),
            row("Cyberdyne", "Applied", location="Remote"),
            row("Piper", "Rejected", "Applied", "Remote")]
    tags = {"soylent": ["automotive", "hardware"], "cyberdyne": ["automotive"], "piper": ["fintech"]}
    rec = tr.count(rows, tags)
    assert rec["areas"]["automotive"]["apps"] == 3 and rec["areas"]["automotive"]["calls"] == 1, rec
    assert rec["areas"]["fintech"] == {"apps": 1, "calls": 0, "rate": 0}, rec
    assert rec["locations"]["remote"]["apps"] == 2, rec
    assert rec["locations"]["on-site"]["calls"] == 1, rec
    assert rec["called"] == ["Soylent"], rec


def test_each_company_is_tagged_once():
    with store() as s:
        s.stub({"soylent": ["automotive", "not a real tag"], "piper": ["fintech"]})
        rows = [row("Soylent"), row("Soylent, Inc."), row("Piper")]
        tags = tr.tag_companies(rows, log=lambda *_: None)
        assert tags == {"soylent": ["automotive"], "piper": ["fintech"]}, tags
        assert len(s.calls) == 1, "one call for every new company"
        tr.tag_companies(rows + [row("Piper")], log=lambda *_: None)
        assert len(s.calls) == 1, "cached companies are never asked again"


def test_build_writes_the_record_and_refresh_waits_a_day():
    with store() as s:
        s.stub({"soylent": ["automotive"]})
        rec = tr.build([row("Soylent", "Phone Screen")], log=lambda *_: None)
        assert rec["areas"]["automotive"]["calls"] == 1
        assert tr.load()["overall"]["apps"] == 1
        assert tr.refresh(log=lambda *_: None) is False, "already built today"


# ── Tier moves

def test_an_area_with_calls_moves_up():
    rec = record({"automotive": {"apps": 28, "calls": 3}})
    tier, note = fit.adjust(job("look", ["automotive"]), [], None, rec)
    assert tier == "strong", (tier, note)
    assert note == ("Up from Worth a look: you got calls from 3 of 28 "
                    "automotive applications."), note


def test_one_call_is_not_enough_to_move_up():
    rec = record({"robotics": {"apps": 2, "calls": 1}})
    assert fit.adjust(job("look", ["robotics"]), [], None, rec)[0] == "look"


def test_calls_at_the_overall_rate_do_not_move_up():
    rec = record({"consumer": {"apps": 60, "calls": 2}}, apps=150, calls=4)
    assert fit.adjust(job("look", ["consumer"]), [], None, rec)[0] == "look"


def test_many_applications_without_calls_move_down():
    rec = record(locations={"remote": {"apps": 49, "calls": 1}})
    tier, note = fit.adjust(job("strong", ["fintech"], location="Remote, US"), [], None, rec)
    assert tier == "look", (tier, note)
    assert note == "Down from Strong match: 1 call from 49 remote applications.", note
    rec = record({"big tech": {"apps": 35, "calls": 0}})
    tier, note = fit.adjust(job("look", ["big tech"]), [], None, rec)
    assert tier == "stretch" and "no calls from 35 big tech" in note, (tier, note)


def test_too_few_applications_do_not_move_down():
    rec = record(locations={"remote": {"apps": 19, "calls": 0}})
    assert fit.adjust(job("strong", [], location="Remote"), [], None, rec)[0] == "strong"


def test_one_call_at_an_average_rate_does_not_move_down():
    # 3 calls from 116 overall: 1 from 35 is no worse than average.
    rec = record(locations={"on-site": {"apps": 35, "calls": 1}}, apps=116, calls=3)
    assert fit.adjust(job("strong", []), [], None, rec)[0] == "strong"


def test_mixed_evidence_moves_nothing():
    rec = record({"automotive": {"apps": 28, "calls": 3}},
                 {"remote": {"apps": 49, "calls": 1}})
    assert fit.adjust(job("look", ["automotive"], location="Remote"), [], None, rec) == ("look", "")


def test_the_users_own_preference_beats_the_track_record():
    rec = record({"automotive": {"apps": 28, "calls": 3}})
    tier, note = fit.adjust(job("look", ["automotive"]), ["less automotive"], None, rec)
    assert tier == "stretch" and "you asked for" in note, (tier, note)


def test_the_track_record_beats_learned_interest():
    rec = record({"automotive": {"apps": 28, "calls": 3}})
    acted = [job("look", ["automotive"], status="dismissed") for _ in range(4)]
    tier, note = fit.adjust(job("look", ["automotive"]), [], fit.learn(acted), rec)
    assert tier == "strong" and "calls from 3 of 28" in note, (tier, note)


def test_a_stretch_never_becomes_strong():
    rec = record({"automotive": {"apps": 28, "calls": 3}})
    tier, note = fit.adjust(job("stretch", ["automotive"]), [], None, rec)
    assert tier == "look", (tier, note)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
    print("%d passed" % n)
