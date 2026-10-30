"""Cue's cards, now served by Verbatim under /api/cue.

Ported from ~/ai/flashcards/tests/test_import.py. Each test gets its own
card database, so the real data/cue.sqlite is never touched.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import cue  # noqa: E402
import server  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CUE_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
    monkeypatch.setattr(cue, "SEED_PATH", os.path.join(tempfile.mkdtemp(), "no-seed.json"))
    assert "test.db" in cue.db_path()
    cue.init_db()
    return server.app.test_client()


JOB = "acme-2026-10-17"


def cards(n=3):
    return [{"key": f"gap:{i}", "category": "gap", "front": f"Q{i}", "back": f"A{i}"} for i in range(n)]


def imp(client, cs, company="Acme", role="Product Designer"):
    return client.post(f"/api/cue/jobs/{JOB}/import", json={"company": company, "role": role, "cards": cs})


def test_existing_decks_untouched(client):
    before = client.get("/api/cue/cards?deck=recruiter").get_json()
    port = client.get("/api/cue/cards?deck=portfolio").get_json()
    assert imp(client, cards()).status_code == 200
    assert client.get("/api/cue/cards?deck=recruiter").get_json() == before
    assert client.get("/api/cue/cards?deck=portfolio").get_json() == port
    # openers still carry the [this company] placeholder
    assert any("[this company]" in c["front"] for c in before)


def test_import_creates_job_and_cards(client):
    r = imp(client, cards()).get_json()
    assert r["cards"] == 3 and r["added"] == 3 and r["path"] == f"#cue/{JOB}"
    got = client.get(f"/api/cue/cards?deck=interview&job={JOB}").get_json()
    assert {c["front"] for c in got} == {"Q0", "Q1", "Q2"}
    jobs = client.get("/api/cue/jobs").get_json()
    assert jobs == [{**jobs[0], "job": JOB, "company": "Acme", "role": "Product Designer", "cards": 3}]
    assert cue.job_counts() == {JOB: 3}


def test_update_keeps_progress_notes_and_hand_cards(client):
    imp(client, cards())
    got = {c["gen_key"]: c for c in client.get(f"/api/cue/cards?job={JOB}").get_json()}
    keep = got["gap:0"]["id"]
    client.post(f"/api/cue/cards/{keep}/review", json={"rating": "good"})
    client.post(f"/api/cue/cards/{keep}/notes", json={"notes": "my note"})
    noted_gone = got["gap:2"]["id"]
    client.post(f"/api/cue/cards/{noted_gone}/notes", json={"notes": "keep me"})
    hand = client.post("/api/cue/cards", json={"deck": "interview", "category": "other", "front": "Mine",
                                               "back": "By hand", "job": JOB}).get_json()

    new = [{"key": "gap:0", "category": "gap", "front": "Q0 new", "back": "A0 new"},
           {"key": "why", "category": "why", "front": "Why Acme?", "back": "Because"}]
    r = imp(client, new).get_json()
    assert r["added"] == 1 and r["updated"] == 1 and r["removed"] == 1 and r["kept"] == 1

    after = {c["id"]: c for c in client.get(f"/api/cue/cards?job={JOB}").get_json()}
    k = after[keep]
    assert k["front"] == "Q0 new" and k["notes"] == "my note" and k["reviews"] == 1
    assert after[hand["id"]]["front"] == "Mine" and after[hand["id"]]["gen_key"] is None
    assert after[noted_gone]["notes"] == "keep me" and after[noted_gone]["gen_key"] is None
    assert got["gap:1"]["id"] not in after          # no notes, gone from the set
    assert r["cards"] == 4


def test_hand_edited_text_wins(client):
    imp(client, cards(1))
    c = client.get(f"/api/cue/cards?job={JOB}").get_json()[0]
    client.put(f"/api/cue/cards/{c['id']}", json={"category": "gap", "front": "Q0", "back": "My own words"})
    imp(client, [{"key": "gap:0", "category": "gap", "front": "Q0", "back": "Model words"}])
    assert client.get(f"/api/cue/cards?job={JOB}").get_json()[0]["back"] == "My own words"


def test_other_jobs_untouched(client):
    imp(client, cards())
    client.post("/api/cue/jobs/other-2026-10-01/import", json={"company": "Other", "cards": cards(1)})
    imp(client, cards(1))
    assert len(client.get("/api/cue/cards?job=other-2026-10-01").get_json()) == 1


def test_bad_input(client):
    assert client.post("/api/cue/jobs/BAD ID/import", json={}).status_code in (400, 404)
    assert imp(client, []).status_code == 400
    assert client.post(f"/api/cue/jobs/{JOB}/import", json={"cards": cards()}).status_code == 400


def test_stats_and_delete(client):
    s = client.get("/api/cue/stats").get_json()
    assert set(s) == {"total", "due", "today_reviews", "by_category", "streak"}
    one = client.get("/api/cue/cards?deck=portfolio").get_json()[0]
    assert client.delete(f"/api/cue/cards/{one['id']}").status_code == 204
    assert client.get("/api/cue/stats").get_json()["total"] == s["total"] - 1


