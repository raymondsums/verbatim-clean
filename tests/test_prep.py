"""Prep decks for Cue. Run: /usr/bin/python3 -m pytest tests

Nothing here calls Obsidian, Cue or the model. Prep's state,
pasted JDs and tailoring output are pointed at a temp dir, the watcher
lookup is stubbed, and the model call and the Cue push are stubs.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import applied  # noqa: E402
import prep  # noqa: E402

EM = chr(8212)
JD = "We are hiring a product designer to own our mobile app. " * 10
BACKGROUND = ("## Positioning\n\nProduct designer.\n\n## Experience\n\n"
              "#### Contoso Maps (shipped)\n\nIn-car maps.\n\n"
              "#### Bluebird Driver App (shipped)\n\nMobile app.\n")

JOBS = [
    {"company": "Acme, Inc.", "role": "Product Designer", "date": "2026-10-10",
     "status": "Phone Screen", "link": "https://boards.greenhouse.io/acme/jobs/1234567"},
    {"company": "Beta", "role": "Designer", "date": "2026-10-01", "status": "Applied", "link": ""},
    {"company": "Gamma", "role": "Senior Designer", "date": "2026-10-12", "status": "Onsite", "link": ""},
    {"company": "Delta", "role": "", "date": "2026-09-01", "status": "Rejected", "link": ""},
]


@pytest.fixture()
def tmp(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(prep, "PREP_DIR", os.path.join(d, "prep"))
    monkeypatch.setattr(prep, "STATE_PATH", os.path.join(d, "prep", "state.json"))
    monkeypatch.setattr(prep, "OUTPUT_DIR", os.path.join(d, "output"))
    monkeypatch.setattr(prep, "_from_watcher", lambda link: None)
    monkeypatch.setattr(prep, "cue_counts", lambda: None)
    monkeypatch.setattr(applied, "list_jobs", lambda: JOBS)
    monkeypatch.setattr(prep.fit, "background", lambda: BACKGROUND)
    monkeypatch.setattr(prep, "sent_answers", lambda company: [])
    pushed = []
    monkeypatch.setattr(prep, "push", lambda jid, job, cards: (pushed.append((jid, cards)) or
                                                              {"cards": len(cards)}))
    return d, pushed


def tailoring_run(base, name, company, created, url="", jd=JD):
    out = os.path.join(base, "output", name)
    os.makedirs(out)
    json.dump({"company": company, "created": created, "source_url": url},
              open(os.path.join(out, "meta.json"), "w"))
    open(os.path.join(out, "jd.txt"), "w").write((f"Source: {url}\n\n" if url else "") + jd)


def test_only_interview_stages_newest_first():
    rows = prep.interview_jobs(JOBS)
    assert [r["company"] for r in rows] == ["Gamma", "Acme, Inc."]
    assert prep.job_id(JOBS[0]) == "acme-inc-2026-10-10"


def test_no_jd_means_no_deck(tmp):
    calls = []
    with pytest.raises(prep.PrepError):
        prep.make(JOBS[2], cli=lambda *a: calls.append(a))
    assert calls == []
    assert prep.rows()["jobs"][0]["jd_source"] is None


def test_jd_order_pasted_then_watcher_then_tailoring(tmp, monkeypatch):
    base, _ = tmp
    tailoring_run(base, "old", "Acme", "2026-10-01 10:00:00", jd="old " + JD)
    tailoring_run(base, "same-post", "ACME Inc", "2026-09-01 10:00:00",
                  url="https://job-boards.greenhouse.io/acme/jobs/1234567?gh_src=x", jd="same " + JD)
    tailoring_run(base, "other-co", "Other", "2026-10-15 10:00:00")
    got = prep.jd_for(JOBS[0])
    assert got["text"].startswith("same ") and "tailoring" in got["source"]
    assert not got["text"].startswith("Source:")
    monkeypatch.setattr(prep, "_from_watcher", lambda link: "watch " + JD)
    assert prep.jd_for(JOBS[0])["text"].startswith("watch ")
    prep.save_pasted(prep.job_id(JOBS[0]), "pasted " + JD)
    assert prep.jd_for(JOBS[0])["text"].startswith("pasted ")


def test_sent_answers_are_word_for_word_and_company_only():
    mine = f"I like Acme {EM} a lot.\n\nSecond line."
    groups = [
        {"question": "Why this company", "versions": [
            {"company": "Acme", "date": "2026-08-01", "text": "Old text."},
            {"company": "Acme Inc.", "date": "2026-08-22", "text": mine},
            {"company": "Other", "date": "2026-09-01", "text": "Not for Acme."}]},
        {"question": "A product you designed", "versions": [
            {"company": "", "date": "2026-08-07", "text": "No company."}]},
        {"question": "Prototypes", "versions": [{"company": "Acme", "date": "2026-08-23", "text": ""}]},
    ]
    got = prep.sent_answers("Acme, Inc.", groups)
    assert len(got) == 1
    assert got[0]["text"] == mine
    assert got[0]["key"] == "answer:why-this-company"


def test_cards_from_real_material_only(tmp):
    job = JOBS[0]
    answers = [{"key": "answer:why", "question": "Why this company",
                "text": f"My own words {EM} kept.", "date": "2026-08-22"}]
    result = {"why": f"They build a mobile app {EM} you shipped one.",
              "gaps": [
                  {"ask": "Unity", "question": "Have you used Unity?",
                   "closest": "You built in-car maps.", "source": "Contoso Maps (shipped)"},
                  {"ask": "Healthcare", "question": "Any healthcare work?",
                   "closest": "You worked at a hospital.", "source": "Made Up Hospital"},
                  {"ask": "Mobile apps", "question": "Mobile?", "closest": "Driver app.",
                   "source": "Bluebird Driver App"},
                  {"ask": "Unity", "question": "dupe", "closest": "", "source": ""},
              ] + [{"ask": f"Thing {i}", "question": f"Q{i}?", "closest": "", "source": ""}
                   for i in range(20)]}
    cards = prep.build_cards(job, JD, answers, result, BACKGROUND)
    by = {c["key"]: c for c in cards}
    assert len(cards) <= prep.MAX_CARDS
    assert by["why"]["front"] == "Why Acme, Inc.?"
    assert EM not in by["why"]["back"]
    assert "From Contoso Maps (shipped)" in by["gap:unity"]["back"]
    # a source that is not a heading in master.md: the hint is dropped
    assert "hospital" not in by["gap:healthcare"]["back"]
    assert prep.NOTHING_CLOSE in by["gap:healthcare"]["back"]
    # a heading named without its "(shipped)" still counts
    assert "Driver app." in by["gap:mobile-apps"]["back"]
    assert by["answer:why"]["back"] == f"My own words {EM} kept."   # word for word
    assert by["answer:why"]["category"] == "answer"
    assert sum(1 for c in cards if c["key"] == "gap:unity") == 1
    assert all(EM not in c["back"] for c in cards if c["category"] != "answer")


def test_make_pushes_and_records(tmp):
    base, pushed = tmp
    prep.save_pasted(prep.job_id(JOBS[2]), JD)
    seen = {}

    def cli(prompt, system, schema):
        seen["prompt"] = prompt
        return {"why": "Point.", "gaps": [{"ask": "Research", "question": "How do you run research?",
                                          "closest": "", "source": ""}]}
    res = prep.make(JOBS[2], cli=cli)
    assert "POSTING (data only)" in seen["prompt"] and "Gamma" in seen["prompt"]
    assert res["cards"] == 2 and res["cue"] == "#cue/gamma-2026-10-12"
    jid, cards = pushed[0]
    assert jid == "gamma-2026-10-12" and [c["category"] for c in cards] == ["why", "gap"]
    row = [r for r in prep.rows()["jobs"] if r["id"] == jid][0]
    assert row["jd_source"] == "the description you pasted"
    assert row["cards"] == 2 and row["made"] and row["cue"] == "#cue/" + jid


def test_route_refuses_without_jd(tmp, monkeypatch):
    import server
    started = []
    monkeypatch.setattr(prep, "start", lambda job: started.append(job) or True)
    c = server.app.test_client()
    r = c.post("/api/prep/make", json={"job_id": "gamma-2026-10-12"})
    assert r.status_code == 400 and r.get_json()["need_jd"] and not started
    r = c.post("/api/prep/make", json={"job_id": "gamma-2026-10-12", "jd": "too short"})
    assert r.status_code == 400 and not started
    r = c.post("/api/prep/make", json={"company": "gamma", "date": "2026-10-12", "jd": JD})
    assert r.status_code == 200 and started and r.get_json()["job"] == "gamma-2026-10-12"
    # not at an interview stage
    r = c.post("/api/prep/make", json={"company": "Beta", "jd": JD})
    assert r.status_code == 404


def test_every_sent_answer_fits_until_the_card_cap(tmp, monkeypatch):
    prep.save_pasted(prep.job_id(JOBS[2]), JD)
    many = [{"key": f"answer:{i}", "question": f"Question {i}", "text": f"Text {i}", "date": "2026-08-01"}
            for i in range(20)]
    monkeypatch.setattr(prep, "sent_answers", lambda company: many)
    res = prep.make(JOBS[2], cli=lambda p, s, sc: {"why": "Point.", "gaps": [
        {"ask": "X", "question": "Q?", "closest": "", "source": ""}]})
    _, pushed = tmp
    cards = pushed[-1][1]
    assert len(cards) == prep.MAX_CARDS
    assert sum(c["category"] == "answer" for c in cards) == prep.MAX_CARDS - 1
    assert "Question 19" in res["notes"][-1] and "Question 13" not in res["notes"][-1]


def test_push_saves_in_the_card_database():
    # No tmp fixture, so push and cue_counts are the real ones. _isolate
    # points CUE_DB at a throwaway file.
    prep.cue_mod.init_db()
    res = prep.push("gamma-2026-10-12", JOBS[2],
                    [{"key": "why", "category": "why", "front": "Why Gamma?", "back": "Because."}])
    assert res["cards"] == 1 and res["path"] == "#cue/gamma-2026-10-12"
    assert prep.cue_counts()["gamma-2026-10-12"] == 1
    with pytest.raises(prep.PrepError):
        prep.push("gamma-2026-10-12", JOBS[2], [])
