"""Tests for autofill planning. Run: python3 tests/test_autofill.py

No test framework on this machine, so this is plain asserts with a small
runner. Every test points its storage at a temp dir, so running it never
touches the real autofill_map.json or corrections.json.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autofill  # noqa: E402

# The example profile, never the real one: the suite must run on a fresh
# clone, and nobody's personal details belong in an assertion.
autofill.PROFILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "profile.example.json")


def plan_of(domain, fields):
    """Just the steps; plan() also reports counts the callers use."""
    return autofill.plan(domain, fields)["plan"]


def field(index, label, name="", id_="", placeholder="", tag="input", **kw):
    d = {"index": index, "tag": tag, "label": label, "name": name,
         "id": id_, "placeholder": placeholder}
    d.update(kw)
    return d


class store:
    """Point autofill's json files at a temp dir for the duration."""

    def __init__(self, site_map=None, corrections=None):
        self.site_map = site_map or {}
        self.corrections = corrections or {}

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        self.old = (autofill.MAP_PATH, autofill.CORRECTIONS_PATH)
        autofill.MAP_PATH = os.path.join(self.dir, "map.json")
        autofill.CORRECTIONS_PATH = os.path.join(self.dir, "corrections.json")
        with open(autofill.MAP_PATH, "w") as f:
            json.dump(self.site_map, f)
        with open(autofill.CORRECTIONS_PATH, "w") as f:
            json.dump(self.corrections, f)
        return self

    def __exit__(self, *exc):
        autofill.MAP_PATH, autofill.CORRECTIONS_PATH = self.old

    def saved_map(self):
        with open(autofill.MAP_PATH) as f:
            return json.load(f)


class cli:
    """Replace the CLI mapping call: either a canned answer or a failure."""

    def __init__(self, mappings=None, fail=False):
        self.mappings, self.fail = mappings or [], fail
        self.calls = 0

    def __enter__(self):
        self.old = autofill._map_unknowns

        def fake(fields):
            self.calls += 1
            if self.fail:
                raise autofill.AutofillError("field mapping failed: boom")
            return list(self.mappings)

        autofill._map_unknowns = fake
        return self

    def __exit__(self, *exc):
        autofill._map_unknowns = self.old


# ---------- bug 1: one bad CLI call must not lose the whole fill ----------

def test_cli_failure_keeps_known_fields():
    known = {"first name|first_name|first_name|": "first_name"}
    with store({"x.test": known}), cli(fail=True):
        steps = plan_of("x.test", [
            field(0, "First Name", "first_name", "first_name"),
            field(1, "Preferred pronouns", "pronouns", "p"),
        ])
    fills = [s for s in steps if s["action"] == "fill"]
    assert len(fills) == 1, f"known field lost when the CLI failed: {steps}"
    assert fills[0]["index"] == 0 and fills[0]["value"] == "Ada", fills


def test_cli_failure_on_all_unknown_fields_is_not_an_error():
    with store(), cli(fail=True):
        steps = plan_of("x.test", [field(0, "Preferred pronouns", "pronouns")])
    assert steps == [], f"expected an empty plan, got {steps}"


def test_cli_failure_is_not_learned():
    with store(), cli(fail=True):
        plan_of("x.test", [field(0, "Preferred pronouns", "pronouns")])
        saved = autofill._read_json(autofill.MAP_PATH, {})
    assert not saved.get("x.test"), f"a failed mapping was cached: {saved}"


# ---------- bug 2: colliding signatures must not be filled or learned ----------

def collide(n=3):
    """What Greenhouse's react-select comboboxes produce: no name, no id,
    and the widget's own placeholder text as the label."""
    return [field(i, "Select...") for i in range(n)]


def test_colliding_signatures_are_never_filled_from_the_map():
    poisoned = {"select...|||": "location"}
    with store({"x.test": poisoned}), cli() as c:
        steps = plan_of("x.test", collide())
    assert not [s for s in steps if s["action"] == "fill"], \
        f"one map entry filled several different fields: {steps}"


def test_colliding_signatures_are_never_learned():
    with store(), cli([{"index": i, "key": "location"} for i in range(3)]):
        plan_of("x.test", collide())
        saved = autofill._read_json(autofill.MAP_PATH, {})
    assert "select...|||" not in saved.get("x.test", {}), \
        f"an ambiguous signature was cached: {saved}"


def test_distinct_fields_still_learn_normally():
    mappings = [{"index": 0, "key": "first_name"}, {"index": 1, "key": "email"}]
    with store(), cli(mappings):
        steps = plan_of("x.test", [
            field(0, "First Name", "first_name", "first_name"),
            field(1, "Email", "email", "email"),
        ])
        saved = autofill._read_json(autofill.MAP_PATH, {})
    assert len([s for s in steps if s["action"] == "fill"]) == 2, steps
    assert len(saved["x.test"]) == 2, saved


def test_second_visit_to_a_learned_form_needs_no_cli_call():
    mappings = [{"index": 0, "key": "first_name"}]
    fields = [field(0, "First Name", "first_name", "first_name")]
    with store(), cli(mappings) as c:
        plan_of("x.test", fields)
        plan_of("x.test", [dict(f) for f in fields])
        assert c.calls == 1, f"the map was not reused: {c.calls} CLI calls"


# ---------- the guardrails from the handoff, still holding ----------

def test_salary_and_cover_letter_are_flagged_never_filled():
    site = {"what are your salary expectations?|salary||": "salary",
            "cover letter|cover_letter|cover_letter|": "cover_letter"}
    with store({"x.test": site}), cli():
        steps = plan_of("x.test", [
            field(0, "What are your salary expectations?", "salary"),
            field(1, "Cover letter", "cover_letter", "cover_letter"),
        ])
    assert all(s["action"] == "flag" for s in steps), steps


# ---------- a correction only stands for the key it was made under ----------

def test_a_correction_made_under_another_key_is_not_replayed():
    # The field was once taken for a full name and corrected as one. Now
    # it maps to first_name, and that old full name must not land in it.
    sig = "first name|first_name|first_name|"
    fix = {"x.test": {sig: {"key": "full_name", "filled": "a",
                            "corrected": "Ada Byron Lovelace"}}}
    with store(site_map={"x.test": {sig: "first_name"}}, corrections=fix):
        with cli() as c:
            steps = plan_of("x.test", [field(0, "First name", "first_name",
                                             "first_name")])
    assert c.calls == 0
    assert steps[0]["value"] != "Ada Byron Lovelace", steps


def test_a_correction_under_the_same_key_still_wins():
    sig = "phone|phone|phone|"
    fix = {"x.test": {sig: {"key": "phone", "filled": "a",
                            "corrected": "(555) 010-0000"}}}
    with store(site_map={"x.test": {sig: "phone"}}, corrections=fix):
        with cli():
            steps = plan_of("x.test", [field(0, "Phone", "phone", "phone")])
    assert steps[0]["value"] == "(555) 010-0000", steps


def test_plan_counts_unmapped_fields():
    sig = "colour|colour|colour|"
    with store(site_map={"x.test": {sig: "none"}}):
        with cli():
            out = autofill.plan("x.test", [field(0, "Colour", "colour", "colour")])
    assert out["unmapped"] == 1 and out["plan"] == [], out


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


# ---------- legal and preferred names ----------

def test_legal_and_preferred_boxes_get_the_right_name_even_against_the_map():
    # A map learned before legal names existed must not put the everyday
    # name in a legal box.
    stale = {"legal first name *|legal_first|lf|": "first_name"}
    with store({"x.test": stale}), cli(fail=True):
        steps = plan_of("x.test", [
            field(0, "Legal first name *", "legal_first", "lf"),
            field(1, "Legal last name", "legal_last", "ll"),
            field(2, "Preferred first name", "pf", "pf"),
            field(3, "Preferred last name", "pl", "pl"),
            field(4, "Full name", "fullname", "fn"),
            field(5, "Nickname", "nick", "nn"),
        ])
    got = {s["index"]: s.get("value") for s in steps}
    assert got == {0: "Augusta Ada", 1: "King", 2: "Ada", 3: "Lovelace",
                   4: "Ada Augusta Ada King", 5: "Ada"}, steps


def test_a_note_in_brackets_does_not_decide_the_name_key():
    label = ("[Optional] Preferred Full Name (The name that you would like to be "
             "referred to as. Please input your legal name in the first and last "
             "name fields above)")
    assert autofill.name_rule({"label": label}) == "full_name"
    assert autofill.name_rule({"label": "Legal First & Last Name"}) == "legal_full_name"


def test_a_years_threshold_question_asks_for_the_years():
    for label in ("Do you have more than 6 years of product design experience?",
                  "Do you have 5 or more years of professional product design experience, including shipped work?",
                  "Do you have at least 3 years of experience with Figma?"):
        assert autofill.rule_key({"label": label}) == "years_of_experience", label
    assert autofill.rule_key({"label": "How many years have you lived here?"}) is None
    assert autofill.rule_key({"label": "Do you have experience with Figma?"}) is None


def test_gender_identity_is_the_gender_question():
    for label in ("Gender Identity*", "Gender Identity (optional)",
                  "How do you currently describe your gender identity? *",
                  "What is your gender identity?*"):
        assert autofill.rule_key({"label": label}) == "eeo_gender", label
    assert autofill.rule_key({"label": "Do you identify as transgender?"}) is None


def test_a_box_labelled_country_is_the_country():
    for label in ("Country", "Country*", "Country of residence *"):
        assert autofill.rule_key({"label": label}) == "country", label
    assert autofill.rule_key({"label": "Which country will you work from?"}) is None


def test_legally_authorized_is_not_a_name_box():
    assert autofill.name_rule({"label": "Are you legally authorized to work in the US?"}) is None
    assert autofill.name_rule({"label": "First name"}) is None


def test_linkedin_always_uses_the_profile_link_over_a_site_correction():
    sig = "linkedin profile|li|li|"
    fixes = {"x.test": {sig: {"key": "linkedin", "filled": "a", "corrected": "www.linkedin.com/in/old"}}}
    with store({"x.test": {sig: "linkedin"}}, fixes), cli(fail=True):
        steps = plan_of("x.test", [field(0, "LinkedIn profile", "li", "li")])
    assert steps[0]["value"] == autofill._values(autofill._profile())["linkedin"], steps


def test_an_empty_profile_value_is_flagged_not_filled_from_history():
    with store({"x.test": {"zip code|z|z|": "zip_code"}}), cli(fail=True):
        steps = plan_of("x.test", [field(0, "ZIP code", "z", "z")])
    assert steps[0]["action"] == "flag", steps


if __name__ == "__main__":
    sys.exit(main())
