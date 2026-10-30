"""Applications: stages, dates, the No response sweep and the prep
requests an outside tool can poll. Each test gets its own database."""

import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import server  # noqa: E402
import tracker  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("TRACKER_DB", os.path.join(tempfile.mkdtemp(), "t.sqlite"))
    return server.app.test_client()


def add(client, **kw):
    r = client.post("/api/applications/add", json=dict({"company": "Acme", "role": "Product Designer"}, **kw))
    assert r.status_code == 200, r.get_json()
    return r.get_json()["application"]


def move(client, app_id, **kw):
    r = client.post("/api/applications/update", json=dict({"id": app_id}, **kw))
    assert r.status_code == 200, r.get_json()
    return r.get_json()["application"]


def test_add_sets_follow_up_and_salary(client):
    a = add(client, salary="$155-185K", date="2026-10-01")
    assert a["status"] == "Applied" and a["follow_up"] == "2026-10-08"
    assert a["salary"] == "$155K – $185K"
    assert client.post("/api/applications/add", json={"company": " "}).status_code == 400
    assert client.post("/api/applications/add", json={"company": "X", "status": "Maybe"}).status_code == 400


def test_saved_to_applied_stamps_today(client):
    a = add(client, status="Saved", date="2026-01-01")
    assert a["follow_up"] is None
    a = move(client, a["id"], status="Applied")
    today = date.today().isoformat()
    assert a["date"] == today and a["last_contact"] == today


def test_rejected_keeps_the_stage_it_reached(client):
    a = add(client)
    move(client, a["id"], status="Phone Screen")
    a = move(client, a["id"], status="Rejected")
    assert a["rejected_at"] == "Phone Screen" and a["rejected_on"] == date.today().isoformat()
    a = move(client, a["id"], status="Applied")
    assert a["rejected_at"] is None


def test_old_applied_rows_become_no_response_once(client):
    old = (date.today() - timedelta(days=40)).isoformat()
    a = add(client, date=old)
    add(client, company="Fresh")
    rows = {r["company"]: r for r in client.get("/api/applications").get_json()}
    assert rows["Acme"]["status"] == "No response" and rows["Acme"]["follow_up"] is None
    assert rows["Fresh"]["status"] == "Applied"
    move(client, a["id"], status="Applied")
    rows = {r["company"]: r for r in client.get("/api/applications").get_json()}
    assert rows["Acme"]["status"] == "Applied"


def test_prep_request_claim_and_debrief(client):
    a = add(client, date="2026-10-20")
    assert client.get("/api/jobs/prep").get_json() == []
    move(client, a["id"], status="Phone Screen")
    pending = client.get("/api/jobs/prep").get_json()
    assert [(p["tier"], p["stage"], p["company"], p["date"]) for p in pending] == \
        [("screen", "Phone Screen", "Acme", "2026-10-20")]
    claim = {"company": "Acme", "date": "2026-10-20", "tier": "screen"}
    assert client.post("/api/jobs/prep/claim", json=claim).get_json() == {"ok": True}
    assert client.post("/api/jobs/prep/claim", json=claim).get_json()["ok"] is False
    assert client.post("/api/jobs/prep/claim", json=dict(claim, tier="interview")).status_code == 404
    assert client.get("/api/jobs/prep").get_json() == []
    # Interviewing then Onsite share one prep pass, and the screen gets a debrief.
    move(client, a["id"], status="Interviewing")
    move(client, a["id"], status="Onsite")
    tiers = sorted(p["tier"] for p in client.get("/api/jobs/prep").get_json())
    assert tiers == ["debrief:Interviewing", "interview"]
    move(client, a["id"], status="Rejected")
    tiers = sorted(p["tier"] for p in client.get("/api/jobs/prep").get_json())
    assert tiers == ["debrief:Interviewing", "debrief:Onsite"]


def test_remove(client):
    a = add(client)
    assert client.post("/api/applications/remove", json={"id": a["id"]}).status_code == 200
    assert client.post("/api/applications/remove", json={"id": a["id"]}).status_code == 404
    assert client.get("/api/applications").get_json() == []


# ── Live: one list of applications for the watch list and the extension

import applied  # noqa: E402
from jobs import watch  # noqa: E402
from test_watch import add as add_watched, store  # noqa: E402


def watched(s):
    con = watch.db()
    add_watched(con, "gh:1", title="Product Designer", company="Acme", url="https://boards.greenhouse.io/acme/jobs/111111")
    add_watched(con, "gh:2", title="Senior Product Designer", company="Beta", url="https://jobs.lever.co/beta/abc")
    add_watched(con, "gh:3", title="Staff Designer", company="Gamma", url="https://jobs.ashbyhq.com/gamma/x", status="applied")
    con.close()


def test_mark_applied_and_undo_live(client):
    with store() as s:
        watched(s)
        r = client.post("/api/watch/status", json={"id": "gh:2", "status": "applied"}).get_json()
        assert r["ok"] and r["tracked"]["added"]
        assert [r["id"] for r in watch.list_jobs("all")] == ["gh:1"]
        assert applied.check_duplicate("Beta", "https://jobs.lever.co/beta/abc")["posting"]["job_id"] == "gh:2"
        # Undo takes back the row Verbatim added.
        assert client.post("/api/watch/status", json={"id": "gh:2", "status": "seen"}).get_json()["ok"]
        assert tracker.find(job_id="gh:2") is None
        # A row that moved on is not taken back.
        client.post("/api/watch/status", json={"id": "gh:2", "status": "applied"})
        tracker.update(tracker.find(job_id="gh:2")["id"], {"status": "Phone Screen"})
        r = client.post("/api/watch/status", json={"id": "gh:2", "status": "seen"})
        assert r.status_code == 409 and "Phone Screen" in r.get_json()["error"]
        # Tailoring saves, then Log it moves the same row on.
        assert applied.save_application("Acme", "https://boards.greenhouse.io/acme/jobs/111111", "Product Designer")
        row = applied.check_duplicate("Acme", "https://boards.greenhouse.io/acme/jobs/111111")["posting"]
        assert row["status"] == "Saved" and row["job_id"] == "gh:1"
        assert applied.promote_saved(row)
        assert tracker.find(job_id="gh:1")["status"] == "Applied"


def test_added_application_takes_its_posting_off_the_lists(client):
    with store() as s:
        watched(s)
        r = client.post("/api/applications/add", json={"company": "Beta", "link": "https://jobs.lever.co/beta/abc"})
        assert r.get_json()["application"]["job_id"] == "gh:2"
        assert "gh:2" not in {j["id"] for j in watch.list_jobs("all")}
        # Moved back to Saved, it returns to the list; rejected, it stays off.
        app_id = r.get_json()["application"]["id"]
        client.post("/api/applications/update", json={"id": app_id, "status": "Saved"})
        assert "gh:2" in {j["id"] for j in watch.list_jobs("all")}
        client.post("/api/applications/update", json={"id": app_id, "status": "Rejected"})
        assert "gh:2" not in {j["id"] for j in watch.list_jobs("all")}
