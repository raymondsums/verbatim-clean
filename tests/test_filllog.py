"""Tests for the fill log. Run: python3 tests/test_filllog.py

The log is pointed at a temp file, so the real logs/fills.jsonl is never
touched. The routes run against the example profile, never the real one.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import autofill  # noqa: E402
import filllog  # noqa: E402
import server  # noqa: E402

EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "profile.example.json")


class log:
    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        self.old = (filllog.LOG_PATH, autofill.MAP_PATH,
                    autofill.CORRECTIONS_PATH, autofill.PROFILE_PATH)
        filllog.LOG_PATH = os.path.join(self.dir, "logs", "fills.jsonl")
        autofill.MAP_PATH = os.path.join(self.dir, "map.json")
        autofill.CORRECTIONS_PATH = os.path.join(self.dir, "corrections.json")
        autofill.PROFILE_PATH = EXAMPLE
        return self

    def __exit__(self, *exc):
        (filllog.LOG_PATH, autofill.MAP_PATH, autofill.CORRECTIONS_PATH,
         autofill.PROFILE_PATH) = self.old

    def lines(self):
        try:
            with open(filllog.LOG_PATH) as f:
                return [json.loads(x) for x in f]
        except OSError:
            return []

    def raw(self):
        with open(filllog.LOG_PATH) as f:
            return f.read()


def test_one_line_per_fill_with_counts_and_ats():
    with log() as l:
        fid = filllog.log_fill("job-boards.greenhouse.io", 5, 2, 1, 3)
        [e] = l.lines()
    assert fid and e["fill_id"] == fid, e
    assert e["ats"] == "greenhouse" and e["domain"] == "job-boards.greenhouse.io"
    assert (e["filled"], e["flagged"], e["skipped"], e["unmapped"]) == (5, 2, 1, 3)


def test_test_pages_are_not_logged():
    with log() as l:
        assert filllog.log_fill("localhost", 3, 0, 0, 0) is None
        filllog.log_correction("127.0.0.1", "x", ["a|b|c|"])
        assert l.lines() == []


def test_stats_give_accuracy_per_domain():
    with log():
        a = filllog.log_fill("jobs.ashbyhq.com", 4, 0, 0, 0)
        filllog.log_fill("jobs.ashbyhq.com", 6, 1, 1, 2)
        filllog.log_correction("jobs.ashbyhq.com", a, ["name|n|n|"])
        out = filllog.stats()
    d = out["jobs.ashbyhq.com"]
    assert d["fills"] == 2 and d["filled"] == 10 and d["corrected"] == 1, d
    assert d["accuracy"] == 0.9, d
    assert d["coverage"] == round(10 / 14, 3), d


def plan_fields():
    return [
        {"index": 0, "tag": "input", "label": "Email", "name": "email",
         "id": "email", "placeholder": ""},
        {"index": 1, "tag": "input", "label": "Favourite colour",
         "name": "colour", "id": "colour", "placeholder": ""},
    ]


def test_the_plan_route_logs_counts_and_returns_the_fill_id():
    with log() as l:
        with open(autofill.MAP_PATH, "w") as f:
            json.dump({"jobs.lever.co": {"email|email|email|": "email",
                                         "favourite colour|colour|colour|": "none"}}, f)
        client = server.app.test_client()
        out = client.post("/api/autofill/plan", json={
            "domain": "jobs.lever.co", "fields": plan_fields()}).get_json()
        [e] = l.lines()
    assert out["fill_id"] == e["fill_id"], (out, e)
    assert (e["filled"], e["flagged"], e["skipped"], e["unmapped"]) == (1, 0, 0, 1), e
    assert e["ats"] == "lever", e


def test_a_correction_is_logged_by_signature_never_by_value():
    with log() as l:
        client = server.app.test_client()
        client.post("/api/autofill/correction", json={
            "domain": "jobs.lever.co", "fill_id": "abc123",
            "corrections": [{"signature": "email|email|email|", "key": "email",
                             "filled": "old-value@example.com",
                             "corrected": "new-value@example.com"}]})
        [e] = l.lines()
        raw = l.raw()
    assert e["event"] == "correction" and e["fill_id"] == "abc123", e
    assert e["signatures"] == ["email|email|email|"] and e["count"] == 1, e
    assert "value@example.com" not in raw, raw


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
