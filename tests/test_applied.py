"""Tests for what happens after a submit. Run: python3 tests/test_applied.py

Nothing here talks to Obsidian or the model. The map and the review
queue are pointed at a temp dir, so the real ones are never touched.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import applied  # noqa: E402
import autofill  # noqa: E402

# The example profile, never the real one.
PROFILE = json.load(open(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "profile.example.json")))


class store:
    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        self.old = (autofill.MAP_PATH, applied.PENDING_PATH)
        autofill.MAP_PATH = os.path.join(self.dir, "map.json")
        applied.PENDING_PATH = os.path.join(self.dir, "pending.json")
        return self

    def __exit__(self, *exc):
        autofill.MAP_PATH, applied.PENDING_PATH = self.old

    def map_for(self, domain):
        return autofill._read_json(autofill.MAP_PATH, {}).get(domain, {})


def field(sig, label, value):
    return {"signature": sig, "label": label, "value": value}


def test_a_value_you_already_stored_identifies_the_field():
    with store() as s:
        out = applied.harvest("x.test", [
            field("e-mail|em|e|", "E-mail", "ada@example.com"),
            field("employer|emp|p|", "Employer", "Example Co"),
        ], PROFILE)
        assert out["learned"] == 2, out
        assert s.map_for("x.test") == {"e-mail|em|e|": "email",
                                       "employer|emp|p|": "employer"}, s.map_for("x.test")


def test_eeo_salary_and_authorization_are_never_learned_or_queued():
    with store() as s:
        out = applied.harvest("x.test", [
            field("gender|g|g|", "Gender", "Male"),
            field("race|r|r|", "Race/Ethnicity", "Asian"),
            field("veteran|v|v|", "Veteran status", "I am not a protected veteran"),
            field("salary|s|s|", "Desired salary", "180000"),
            field("sponsor|sp|sp|", "Will you require sponsorship?", "Yes"),
            # guarded by its value even though the label looks harmless
            field("status|st|st|", "Status", "I require sponsorship for a visa"),
        ], PROFILE)
        assert out == {"learned": 0, "queued": 0}, out
        assert s.map_for("x.test") == {}, s.map_for("x.test")
        assert applied.pending() == [], applied.pending()


def test_an_unknown_value_is_queued_for_review_not_applied():
    with store() as s:
        applied.harvest("x.test", [
            field("how did you hear|src|s|", "How did you hear about us?", "LinkedIn"),
        ], PROFILE)
        queue = applied.pending()
        assert len(queue) == 1 and queue[0]["value"] == "LinkedIn", queue
        assert s.map_for("x.test") == {}, "a guess was written into the map"


def test_profile_is_never_written():
    path = os.path.join(applied.BASE_DIR, "profile.example.json")
    before = open(path).read()
    with store():
        applied.harvest("x.test", [
            field("how did you hear|src|s|", "How did you hear about us?", "A friend"),
            field("e-mail|em|e|", "E-mail", "ada@example.com"),
        ], PROFILE)
    assert open(path).read() == before, "harvesting changed profile.json"


def test_ambiguous_signatures_are_left_alone():
    with store() as s:
        out = applied.harvest("x.test", [
            field("|||", "", "Something"),
            field("|||", "", "Something else"),
        ], PROFILE)
        assert out["learned"] == 0 and out["queued"] == 0, out
        assert s.map_for("x.test") == {}, s.map_for("x.test")


def test_the_same_value_is_not_queued_twice():
    with store():
        f = [field("how did you hear|src|s|", "How did you hear about us?", "LinkedIn")]
        applied.harvest("x.test", f, PROFILE)
        applied.harvest("x.test", f, PROFILE)
        assert len(applied.pending()) == 1, applied.pending()


def test_recording_needs_a_company():
    try:
        applied.record_application("")
    except applied.AppliedError as e:
        assert "company" in str(e), e
    else:
        raise AssertionError("an empty company was accepted")


def _rows():
    import tracker
    return tracker.list_all()


def test_pay_and_location_reach_the_tracker_as_the_posting_wrote_them():
    """The tracker tidies a messy range itself, so this must pass the
    posting's own wording straight through rather than format it here."""
    applied.record_application("Stark", url="https://x.test/j/1",
                             role="Security Engineer, Cloud",
                             salary="$211.4K - $290.6K",
                             location="New York, NY (HQ); Remote (US)")
    row = next(r for r in _rows() if r["company"] == "Stark")
    assert row["role"] == "Security Engineer, Cloud", row
    assert row["location"] == "New York, NY (HQ); Remote (US)", row
    assert row["salary"], row
    assert row["status"] == "Applied"


def test_a_missing_pay_or_location_is_left_empty_not_guessed():
    applied.record_application("Somewhere")
    row = next(r for r in _rows() if r["company"] == "Somewhere")
    assert not row["salary"] and not row["location"] and not row["role"], row


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


def test_track_saves_once_and_moves_saved_on_to_applied():
    import tracker
    assert applied.track("Acme", "https://acme.test/j/12345678", "Designer", "$150K - $200K", "SF") == {"status": "Saved", "added": True}
    assert applied.track("Acme", "https://acme.test/j/12345678", "Designer") == {"status": "Saved", "added": False}
    assert applied.track("Acme", "https://acme.test/j/12345678", "Designer", status="Applied") == {"status": "Applied", "added": False}
    row = next(r for r in tracker.list_all() if r["company"] == "Acme")
    tracker.update(row["id"], {"status": "Phone Screen"})
    assert applied.track("Acme", "https://acme.test/j/12345678", status="Applied") == {"status": "Phone Screen", "added": False}
    assert applied.track("Beta", "https://beta.test/j/87654321", "Designer", status="Applied") == {"status": "Applied", "added": True}


if __name__ == "__main__":
    sys.exit(main())
