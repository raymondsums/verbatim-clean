"""Tests for who saved answers are credited to. Run: python3 tests/test_attribution.py

Saved answers used to be credited to whichever company was tailored for
last. These point the output folder at a temp dir of fake tailoring runs
and stub the answer repo, so nothing real is read or written.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import answers  # noqa: E402
import server  # noqa: E402


class runs:
    """Fake tailoring runs, oldest first, in a temp output dir."""

    def __init__(self, *metas):
        self.metas = metas

    def __enter__(self):
        self.old = server.OUTPUT_DIR
        server.OUTPUT_DIR = tempfile.mkdtemp()
        for n, (company, role) in enumerate(self.metas):
            d = os.path.join(server.OUTPUT_DIR, f"run-{n}")
            os.makedirs(d)
            with open(os.path.join(d, "meta.json"), "w") as f:
                json.dump({"company": company, "role": role}, f)
            t = time.time() - 100 + n  # later runs are newer
            os.utime(d, (t, t))
        return self

    def __exit__(self, *exc):
        server.OUTPUT_DIR = self.old


class captured:
    """Stub answers.capture and record what company each call got."""

    def __enter__(self):
        self.calls = []
        self.old = answers.capture

        def fake(question, answer, company="", role="", **kw):
            self.calls.append((company, role))
            return {"saved": True, "question": question, "outcome": "created",
                    "group": question}

        server.answers_mod.capture = fake
        return self

    def __exit__(self, *exc):
        server.answers_mod.capture = self.old


ITEM = {"question": "Why do you want to work here?",
        "answer": "Because the product is one I would use myself."}


def post(payload):
    client = server.app.test_client()
    return client.post("/api/answers/capture-batch", json=payload).get_json()


def test_the_newest_run_does_not_label_another_company():
    with runs(("Umbrella", "Product Designer")), captured() as c:
        out = post({"items": [ITEM],
                    "url": "https://example-jobs.test/apply/123",
                    "title": "Apply: Senior Designer", "company": "Example"})
    assert c.calls == [("", "")], c.calls
    assert out["recorded_under"] == "", out


def test_a_run_for_the_page_company_names_company_and_role():
    with runs(("GameChanger", "Senior Product Designer"),
              ("Umbrella", "Product Designer")), captured() as c:
        post({"items": [ITEM],
              "url": "https://jobs.lever.co/gamechanger/abc-123/apply",
              "title": "GameChanger - Senior Product Designer",
              "company": "Gamechanger"})
    assert c.calls == [("GameChanger", "Senior Product Designer")], c.calls


def test_a_board_url_names_the_company_without_a_role():
    with runs(("Umbrella", "Product Designer")), captured() as c:
        post({"items": [ITEM],
              "url": "https://job-boards.greenhouse.io/acmerobotics/jobs/42",
              "title": "Job Application", "company": ""})
    assert c.calls == [("Acmerobotics", "")], c.calls


def test_an_embedded_greenhouse_form_reads_the_board_from_the_query():
    assert server._company_from_url(
        "https://job-boards.greenhouse.io/embed/job_app?for=outside&token=1"
    ) == "Outside"


def test_an_old_extension_with_no_page_info_records_no_company():
    with runs(("Umbrella", "Product Designer")), captured() as c:
        post({"items": [ITEM]})
    assert c.calls == [("", "")], c.calls


def test_company_names_match_whole_words_only():
    assert server._company_in("Box", "https://jobs.example.test/box/apply")
    assert not server._company_in("Box", "https://dropbox.test/careers")
    assert server._company_in("Game Changer", "gamechanger careers")
    assert not server._company_in("", "anything")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"pass  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}\n      {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}\n      {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
