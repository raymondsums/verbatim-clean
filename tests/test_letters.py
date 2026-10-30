"""Cover letters the user reviews before the PDF: versions, notes, approve, lessons.
No model calls: the CLI and the PDF renderer are stubs."""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import answers  # noqa: E402
import letters  # noqa: E402
import server  # noqa: E402
import tailor  # noqa: E402

MASTER = """# Sam Example
## Fernbrook Labs
Route Board: I redesigned the dispatch board for field crews.
Jobs closed on time went from 40% to 62%.
"""

JD = "Northwind builds tools for field crews. You will design the dispatch app end to end."


@pytest.fixture()
def run(monkeypatch):
    out = tempfile.mkdtemp()
    monkeypatch.setattr(server, "OUTPUT_DIR", out)
    monkeypatch.setattr(letters, "LESSONS_PATH", os.path.join(tempfile.mkdtemp(), "l.json"))
    d = os.path.join(out, "northwind-designer")
    os.makedirs(d)
    with open(os.path.join(d, "jd.txt"), "w") as f:
        f.write("Source: https://x.test/1\n\n" + JD)
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump({"company": "Northwind", "role": "Designer"}, f)
    letters.start(d, "Dear team,\n\nI led the checkout flow.\n\nBest regards,\nRay")
    made = []
    monkeypatch.setattr(server, "render_pdf", lambda md, path, title="": (made.append(md), open(path, "w").write("pdf")))
    monkeypatch.setattr(server, "load_profile", lambda: {"name": "Jordan"})
    return server.app.test_client(), d, made


def test_review_revise_approve(run, monkeypatch):
    c, d, made = run
    master = os.path.join(tempfile.mkdtemp(), "master.md")
    with open(master, "w", encoding="utf-8") as f:
        f.write(MASTER)
    monkeypatch.setattr(tailor, "MASTER_PATH", master)
    seen = {}

    def cli(system, schema, prompt, timeout=None):
        seen["prompt"] = prompt
        return {"cover_letter_md": "Dear team,\n\nShort and to the point.\n\nBest regards,"}
    monkeypatch.setattr(answers, "_run_cli", cli)
    st = c.get("/api/letter/northwind-designer").get_json()
    assert [v["source"] for v in st["versions"]] == ["draft"] and st["approved"] is None
    assert not os.path.exists(os.path.join(d, "cover-letter.pdf"))
    # The user's edit in the box is kept as its own version before the note runs.
    st = c.post("/api/letter/northwind-designer/revise",
                json={"text": "Dear team,\n\nI led the checkout flow at Bluebird.\n\nBest regards,\nRay", "note": "shorter"}).get_json()
    assert [v["source"] for v in st["versions"]] == ["draft", "edit", "note"]
    assert "at Bluebird" in seen["prompt"] and "shorter" in seen["prompt"] and JD in seen["prompt"]
    assert st["versions"][-1]["text"].endswith("Best regards,\nRay")
    assert c.post("/api/letter/northwind-designer/revise", json={"text": "", "note": " "}).status_code == 400
    # Approve builds the PDF from exactly the text sent.
    st = c.post("/api/letter/northwind-designer/approve", json={"text": "Final words.\n\nBest regards,\nRay"}).get_json()
    assert st["approved"] == 4 and "Final words." in made[-1]
    assert os.path.exists(os.path.join(d, "cover-letter.pdf"))
    monkeypatch.setattr(answers, "extract_lessons", lambda draft, final, existing=(), question="":
                        ["Keep it short."] if "shorter" in question else [])
    assert c.post("/api/letter/northwind-designer/learn").get_json() == {"lessons": ["Keep it short."]}
    assert letters.lessons() == ["Keep it short."]


def test_missing_jd_and_bad_ids(run):
    c, d, _ = run
    os.remove(os.path.join(d, "jd.txt"))
    r = c.post("/api/letter/northwind-designer/revise", json={"text": "", "note": "shorter"})
    assert r.status_code == 400 and "job description" in r.get_json()["error"]
    assert c.get("/api/letter/..%2Fetc").status_code == 404
    assert c.get("/api/letter/nope").status_code == 404


def test_older_runs_open_as_an_approved_body(run):
    c, d, _ = run
    old = os.path.join(server.OUTPUT_DIR, "old-run")
    os.makedirs(old)
    with open(os.path.join(old, "cover-letter.md"), "w") as f:
        f.write("# Jordan\n\nProduct Designer\n\nDear team,\n\nHello there.\n\nBest regards,\nRay\n\nSF · jordan@x.test\n")
    st = c.get("/api/letter/old-run").get_json()
    assert st["approved"] == 1 and st["versions"][0]["text"] == "Dear team,\n\nHello there.\n\nBest regards,\nRay"


def test_a_failed_page_run_reports_error_not_running_or_done(run, monkeypatch):
    c, _, _ = run
    monkeypatch.setattr(answers, "cover_letter_context", lambda: "")
    monkeypatch.setattr(tailor, "tailor", lambda jd, **kw: {"notes": []})
    monkeypatch.setattr(server, "_quick_checks", lambda result: [])

    def save(*a, **kw):
        raise ValueError("disk full")
    monkeypatch.setattr(server, "_save_tailoring", save)
    r = c.post("/api/tailor", json={"company": "Northwind", "role": "Designer",
                                    "job_description": JD})
    assert r.status_code == 500 and "disk full" in r.get_json()["error"]
    st = c.get("/api/tailor/status?mode=page").get_json()
    assert st["status"] == "error" and "disk full" in st["error"], st

    def boom(jd, **kw):
        raise RuntimeError("model went away")
    monkeypatch.setattr(tailor, "tailor", boom)
    r = c.post("/api/tailor", json={"job_description": JD})
    assert r.status_code == 500
    st = c.get("/api/tailor/status?mode=page").get_json()
    assert st["status"] == "error" and "model went away" in st["error"], st
