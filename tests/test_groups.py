"""Tests for answers that learn. Run: python3 tests/test_groups.py

Question groups, the migration from the old note shape, versions captured
from what was sent, lessons from edits, source checks on drafts, and the
refine proposal. Nothing talks to Obsidian or the Claude CLI: the note is
held in memory and every model call is stubbed.
"""

import json
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import answers  # noqa: E402
import server  # noqa: E402

TODAY = date.today().isoformat()

OLD = """# Application Answers

Every application question answered, with the answer used, where it was
used, and the wordings it has appeared under. Maintained by the job
toolkit; safe to edit by hand.

## Why do you want to work at Acme?

**Current answer**

I build tools for myself.

I am in San Francisco.

**Used**
- Acme, 2026-08-07
- Acme, Product Designer, 2026-08-22

**Seen as**
- "Why are you interested in this position?"

## Describe a tradeoff. Tell us in three or four

**Current answer**

We shipped the smaller version first.

**Used**
- 2026-08-07

**Seen as**

## Describe a tradeoff. Tell us in three or four

**Current answer**

We shipped the smaller version first.

**Used**
- 2026-08-07

**Seen as**
"""

TITLES = {"Why do you want to work at Acme?": "Why this company"}


class note:
    """The note in memory, read and written through the real code paths,
    with model calls answered by `cli` (a function of system prompt)."""

    def __init__(self, text, cli=None):
        self.text, self.cli = text, cli
        self.cli_calls = []

    def __enter__(self):
        self.old = (answers.read_repo, answers._put_note, answers._run_cli,
                    answers.CACHE_PATH, answers.QUEUE_PATH,
                    answers.VERSIONS_LOG)
        d = tempfile.mkdtemp()
        answers.CACHE_PATH = os.path.join(d, "cache.md")
        answers.QUEUE_PATH = os.path.join(d, "queue.json")
        answers.VERSIONS_LOG = os.path.join(d, "versions.jsonl")
        answers.read_repo = lambda: self.text

        def put(text):
            self.text = text
        answers._put_note = put

        def run(system, schema, prompt, timeout=0):
            self.cli_calls.append((system, prompt))
            if not self.cli:
                raise AssertionError("model called when it should not be")
            return self.cli(system, prompt)
        answers._run_cli = run
        return self

    def __exit__(self, *exc):
        (answers.read_repo, answers._put_note, answers._run_cli,
         answers.CACHE_PATH, answers.QUEUE_PATH,
         answers.VERSIONS_LOG) = self.old


def migrated():
    return answers.migrate_text(OLD, TITLES)


# ---------- grouping and the note shape ----------

def test_migration_keeps_every_word_and_merges_the_duplicate():
    new = migrated()
    groups = answers.parse_repo(new)
    assert [g["question"] for g in groups] == [
        "Why this company", "Describe a tradeoff. Tell us in three or four"]
    why = groups[0]
    assert why["answer"] == "I build tools for myself.\n\nI am in San Francisco."
    # the old heading is kept as a wording, not lost
    assert why["seen_as"][0] == "Why do you want to work at Acme?", why
    # newest first; only the latest text was ever kept
    assert [v["date"] for v in why["versions"]] == ["2026-08-22", "2026-08-07"]
    assert why["versions"][0]["text"] == why["answer"]
    assert why["versions"][0]["role"] == "Product Designer"
    assert why["versions"][1]["text"] == ""
    assert len(groups[1]["versions"]) == 1, groups[1]
    assert "**Current answer**" not in new and "**Used**" not in new


def test_migration_runs_once():
    assert answers.migrate_text(migrated(), TITLES) == migrated()


def test_a_group_survives_a_round_trip():
    g = answers.parse_repo(migrated())[0]
    g["lessons"] = ["Cut the closing line. (Acme, 2026-10-01)"]
    again = answers.parse_group(answers.render_group(g)[3:])
    for k in ("question", "answer", "lessons", "seen_as", "versions", "kind"):
        assert again[k] == g[k], (k, again[k], g[k])


def test_the_old_shape_still_reads():
    g = answers.parse_repo(OLD)[0]
    assert g["answer"].startswith("I build tools")
    assert g["used"] == ["Acme, 2026-08-07", "Acme, Product Designer, 2026-08-22"]


def test_cover_letter_boxes_are_one_group_with_no_model_call():
    for q in ["Cover letter", "Anything else you'd like to share with us?",
              "Additional information"]:
        assert answers.classify_kind(q) == answers.COVER_NOTE, q
    assert answers.classify_kind("Why do you want to work here?") == "answer"
    with note(migrated()) as n:
        out = answers.capture("Cover letter",
                              "I design for drivers and I would like to do it here.",
                              company="Acme")
        assert out["group"] == answers.COVER_NOTE_TITLE, out
        out = answers.capture("Anything else you'd like to share?",
                              "Happy to walk through any of the case studies.",
                              company="Beta")
        assert out["group"] == answers.COVER_NOTE_TITLE, out
    groups = answers.parse_repo(n.text)
    cover = answers.find_group(groups, answers.COVER_NOTE_TITLE)
    assert cover["kind"] == answers.COVER_NOTE and len(cover["versions"]) == 2
    assert "Kind: cover note" in n.text
    assert n.cli_calls == []


def test_a_new_question_is_grouped_under_the_model_title():
    def cli(system, prompt):
        raise AssertionError("not this path")
    with note(migrated()) as n:
        old_match = answers.match
        answers.match = lambda q: {"category": "normal", "entry": None,
                                   "title": "A project you are proud of"}
        try:
            out = answers.capture("Tell us about a project you're proud of *",
                                  "The checkout flow at Bluebird, which I rebuilt.",
                                  company="Acme")
        finally:
            answers.match = old_match
    g = answers.find_group(answers.parse_repo(n.text),
                           "A project you are proud of")
    assert out["group"] == "A project you are proud of", out
    assert g["seen_as"] == ["Tell us about a project you're proud of"], g


# ---------- versions from what was sent ----------

def test_a_sent_answer_is_a_new_version_and_the_best_answer_stays():
    with note(migrated()) as n:
        out = answers.capture("What draws you to Beta?",
                              "Beta makes the tool I use every day at work.",
                              company="Beta", role="Designer",
                              inserted="I build tools for myself.",
                              group="Why this company", jd="http://x/jds/a.txt")
        with open(answers.VERSIONS_LOG) as f:
            logged = json.loads(f.readline())
    assert out["outcome"] == "added", out
    g = answers.find_group(answers.parse_repo(n.text), "Why this company")
    assert g["answer"] == "I build tools for myself.\n\nI am in San Francisco."
    v = g["versions"][0]
    assert (v["date"], v["company"], v["role"], v["jd"]) == (
        TODAY, "Beta", "Designer", "http://x/jds/a.txt"), v
    assert v["text"] == "Beta makes the tool I use every day at work."
    assert "What draws you to Beta?" in g["seen_as"]
    assert logged["inserted"] == "I build tools for myself.", logged


def test_sending_the_same_application_again_replaces_that_version():
    with note(migrated()) as n:
        answers.capture("Why Beta?", "First try at this answer, long enough.",
                        company="Beta", group="Why this company")
        out = answers.capture("Why Beta?", "Second try, after a failed submit.",
                              company="Beta", group="Why this company")
        assert out["outcome"] == "replaced", out
        out = answers.capture("Why Beta?", "Second try, after a failed submit.",
                              company="Beta", group="Why this company")
        assert out["outcome"] == "unchanged", out
    g = answers.find_group(answers.parse_repo(n.text), "Why this company")
    assert len(g["versions"]) == 3, g["versions"]
    assert g["versions"][0]["text"] == "Second try, after a failed submit."


def test_guarded_and_fact_fields_never_become_versions():
    with note(migrated()) as n:
        for q, a in [("What are your salary expectations?",
                      "Something in the range we discussed, thanks."),
                     ("Do you require visa sponsorship?",
                      "Yes, I need a transfer, as written in my profile."),
                     ("LinkedIn", "https://www.linkedin.com/in/someone-xyz")]:
            out = answers.capture(q, a, group="Why this company")
            assert not out["saved"], out
    assert n.text == migrated()


def test_queued_versions_replay_in_the_new_shape():
    # Obsidian closed: the change lands in the cache and the queue, then is
    # replayed onto the real note on the next good read.
    import requests
    d = tempfile.mkdtemp()
    real = {"text": migrated()}
    up = {"on": False}

    class Resp:
        def __init__(self, text="", status=200):
            self.text, self.status_code, self.ok = text, status, status < 400

    def get(*a, **kw):
        if not up["on"]:
            raise requests.exceptions.ConnectionError("refused")
        return Resp(real["text"])

    def put(url, headers=None, data=b"", **kw):
        if not up["on"]:
            raise requests.exceptions.ConnectionError("refused")
        real["text"] = data.decode("utf-8")
        return Resp()

    old = (answers.CACHE_PATH, answers.QUEUE_PATH, answers.requests.get,
           answers.requests.put, os.environ.get("OBSIDIAN_API_KEY"))
    answers.CACHE_PATH = os.path.join(d, "cache.md")
    answers.QUEUE_PATH = os.path.join(d, "queue.json")
    answers.requests.get, answers.requests.put = get, put
    os.environ["OBSIDIAN_API_KEY"] = "test-key"
    with open(answers.CACHE_PATH, "w") as f:
        f.write(migrated())
    # one item queued by the previous version of the code, old shape
    with open(answers.QUEUE_PATH, "w") as f:
        json.dump([{"question": "Why Gamma?", "answer": "An older queued answer.",
                    "use": "Gamma, 2026-09-01", "canonical": "Why this company"}], f)
    try:
        answers.record_version("Why this company", "Why Beta?",
                               "A queued answer for Beta, long enough.",
                               company="Beta")
        assert "Beta" in answers.read_repo()  # visible from the cache
        assert "Beta" not in real["text"]
        up["on"] = True
        answers.read_repo()
        g = answers.find_group(answers.parse_repo(real["text"]),
                               "Why this company")
        assert [v["company"] for v in g["versions"][:2]] == ["Beta", "Gamma"], \
            g["versions"]
        assert g["answer"] == "I build tools for myself.\n\nI am in San Francisco."
        assert "**Lessons**" in real["text"] and "**Used**" not in real["text"]
        assert not os.path.exists(answers.QUEUE_PATH)
    finally:
        (answers.CACHE_PATH, answers.QUEUE_PATH, answers.requests.get,
         answers.requests.put, key) = old
        if key is None:
            os.environ.pop("OBSIDIAN_API_KEY", None)
        else:
            os.environ["OBSIDIAN_API_KEY"] = key


def test_approving_is_the_only_way_the_best_answer_changes():
    with note(migrated()) as n:
        answers.set_best("Why this company", "A better answer the user approved.")
    g = answers.find_group(answers.parse_repo(n.text), "Why this company")
    assert g["answer"] == "A better answer the user approved."
    assert len(g["versions"]) == 2  # versions untouched


# ---------- lessons ----------

def test_no_edits_means_no_lessons_and_no_model_call():
    with note(migrated()) as n:
        assert answers.learn_lessons("Why this company", "Same text here.",
                                     "Same  text here.") == []
    assert n.cli_calls == []


def test_lessons_are_written_deduplicated_and_dash_free():
    def cli(system, prompt):
        return {"lessons": [
            "Cut the closing line about being excited \u2014 every time.",
            "Cut the closing line about being excited.",
            "Name the product, not the team.",
            "Keep it under four sentences.",
            "One too many."]}
    text = migrated().replace(
        "**Lessons**\n", "**Lessons**\n- Keep it under four sentences. (Acme, 2026-09-01)\n", 1)
    with note(text, cli) as n:
        new = answers.learn_lessons("Why this company", "Draft text here.",
                                    "Edited text the user sent.", company="Beta")
    assert new == ["Cut the closing line about being excited - every time.",
                   "Name the product, not the team.", "One too many."], new
    g = answers.find_group(answers.parse_repo(n.text), "Why this company")
    assert len(g["lessons"]) == 4, g["lessons"]
    assert g["lessons"][1].endswith(f"(Beta, {TODAY})"), g["lessons"]
    assert "\u2014" not in n.text


# ---------- drafts and their sources ----------

MASTER = "Bluebird Checkout: 3x checkout completion, 20% to 45%. Led Contoso Maps."
JD = "Acme builds payroll software for small teams. " * 3


def test_unsourced_sentences_and_invented_numbers_are_flagged():
    out = answers.check_sources([
        {"text": "I tripled checkout completion at Bluebird, from 20% to 45%.",
         "source": "master", "quote": "3x checkout completion, 20% to 45%"},
        {"text": "I led navigation for Contoso Maps.", "source": "job description",
         "quote": "Led Contoso Maps"},  # wrong source, still found
        {"text": "I grew revenue 40% at Bluebird.", "source": "master",
         "quote": "3x checkout completion"},          # number not in any source
        {"text": "I have always admired Acme.", "source": "none", "quote": ""},
        # every word is somewhere in the sources, but not in one place
        {"text": "Payroll teams led navigation.", "source": "none", "quote": ""},
    ], {"master": MASTER, "job description": JD})
    assert [s["sourced"] for s in out] == [True, True, False, False, False], out
    assert out[1]["source"] == "master"


def test_a_claim_about_him_never_rests_on_the_posting():
    out = answers.check_sources([
        {"text": "I built payroll software for small teams.",
         "source": "job description", "quote": "builds payroll software for small teams"},
        {"text": "Acme builds payroll software for small teams.",
         "source": "job description", "quote": "builds payroll software for small teams"},
        {"text": "I led Contoso Maps.", "source": "job description",
         "quote": "Led Contoso Maps"},
    ], {"master": MASTER, "job description": JD})
    assert [s["sourced"] for s in out] == [False, True, True], out
    assert out[0]["source"] == "none" and out[2]["source"] == "master", out


def test_a_draft_that_needs_own_line_asks_first():
    def cli(system, prompt):
        return {"ask": "What draws you to Acme personally?", "sentences": []}
    with note(migrated(), cli):
        out = answers.draft_field("Why Acme?", JD, MASTER)
    assert out == {"ask": "What draws you to Acme personally?"}, out


def test_own_line_is_a_source_and_asking_stops():
    seen = {}

    def cli(system, prompt):
        seen["prompt"] = prompt
        return {"ask": "Another question?", "sentences": [
            {"text": "I run payroll for my own small studio.", "source": "your line",
             "quote": "I run payroll for my own small studio", "new_paragraph": False},
            {"text": "Acme builds payroll software for small teams.",
             "source": "job description", "quote": "builds payroll software",
             "new_paragraph": True},
            {"text": "I need visa sponsorship to start.", "source": "none",
             "quote": "", "new_paragraph": False}]}
    with note(migrated(), cli):
        out = answers.draft_field(
            "Why Acme?", JD, MASTER, own_line="I run payroll for my own small studio",
            limit={"chars": 300}, group=answers.parse_repo(migrated())[0])
    assert "ask" not in out
    assert [s["sourced"] for s in out["sentences"]] == [True, True], out
    assert out["text"] == ("I run payroll for my own small studio.\n\n"
                           "Acme builds payroll software for small teams.")
    assert "at most 300 characters" in seen["prompt"]
    assert "Asking is not allowed." in seen["prompt"]
    assert "I build tools for myself." in seen["prompt"]  # the user's best answer


# ---------- refine ----------

def three_versions():
    text = migrated()
    with note(text) as n:
        for c, a in [("Beta", "I build tools for myself. Beta is one I use."),
                     ("Gamma", "I build tools for myself. I am in San Francisco.")]:
            answers.capture("Why?", a, company=c, group="Why this company")
        groups = answers.parse_repo(n.text)
        tags = {answers.entry_key(g, v): "mine" for g in groups
                for v in g["versions"]}
        tags.update({answers.entry_key(g): "mine" for g in groups})
        answers.set_sources(tags)
    return n.text


def test_refine_needs_three_sent_versions():
    with note(migrated()):
        try:
            answers.refine("Why this company")
        except answers.AnswersError as e:
            assert "3 sent versions" in str(e), e
        else:
            raise AssertionError("refined with one version")


def test_refine_proposes_from_own_words_and_writes_nothing():
    def cli(system, prompt):
        assert "Version 3" in prompt, prompt
        return {"sentences": [
            {"text": "I build tools for myself.", "new_paragraph": False,
             "from_versions": [1, 2, 3]},
            {"text": "I am in San Francisco.", "new_paragraph": False,
             "from_versions": [1]},
            {"text": "I am a visionary leader.", "new_paragraph": False,
             "from_versions": [2]}],
            "notes": ["Dropped the company names."]}
    text = three_versions()
    with note(text, cli) as n:
        p = answers.refine("Why this company")
    assert [s["sourced"] for s in p["sentences"]] == [True, True, False], p
    assert p["current"].startswith("I build tools")
    assert n.text == text  # a proposal only


# ---------- the server routes ----------

def test_learn_route_records_versions_and_draws_lessons_from_edits():
    lessons = []
    with note(migrated()) as n:
        old_learn, old_thread = answers.learn_lessons, server.threading.Thread
        answers.learn_lessons = lambda g, d, f, company="": lessons.append((g, d, f))

        class Now:
            def __init__(self, target, args, daemon):
                self.t, self.a = target, args

            def start(self):
                self.t(*self.a)
        server.threading.Thread = Now
        old_out = server.OUTPUT_DIR
        server.OUTPUT_DIR = tempfile.mkdtemp()
        old_jds = server.JDS_DIR
        server.JDS_DIR = tempfile.mkdtemp()
        try:
            r = server.app.test_client().post("/api/answers/learn", json={
                "items": [{"question": "Why Beta?", "group": "Why this company",
                           "inserted": "I build tools for myself.",
                           "answer": "I build tools for myself, and Beta is one."}],
                "url": "https://job-boards.greenhouse.io/beta/jobs/1",
                "title": "Apply", "page_text": "Beta posting text. " * 30,
            }).get_json()
        finally:
            answers.learn_lessons, server.threading.Thread = old_learn, old_thread
            server.OUTPUT_DIR, server.JDS_DIR = old_out, old_jds
    assert r["saved"] == 1 and r["lessons_pending"] == 1, r
    assert lessons == [("Why this company", "I build tools for myself.",
                        "I build tools for myself, and Beta is one.")], lessons
    g = answers.find_group(answers.parse_repo(n.text), "Why this company")
    assert g["versions"][0]["company"] == "Beta"
    assert g["versions"][0]["jd"].startswith("http://localhost:5053/jds/")


def five_versions():
    with note(migrated()) as n:
        for i, c in enumerate(["Beta", "Gamma", "Delta", "Epsilon"]):
            answers.capture("Why %s?" % c,
                            "%s makes a tool I would use, number %d of many." % (c, i),
                            company=c, group="Why this company")
    return n.text


def test_after_enough_versions_only_new_answers_are_kept():
    text = five_versions()
    g = answers.find_group(answers.parse_repo(text), "Why this company")
    assert len(g["versions"]) >= answers.ENOUGH_VERSIONS, g["versions"]
    with note(text) as n:
        # The same answer with a word changed: not new, but the wording
        # of the question is still worth keeping.
        out = answers.capture("What pulls you toward Zeta?",
                              "Gamma makes a tool I would use, number 1 of lots.",
                              company="Zeta", group="Why this company")
        assert out["saved"] and out["outcome"] == "enough", out
        g = answers.find_group(answers.parse_repo(n.text), "Why this company")
        assert len(g["versions"]) == answers.ENOUGH_VERSIONS
        assert "What pulls you toward Zeta?" in g["seen_as"]
        out = answers.capture("Why Eta?",
                              "I sold sandwiches for a summer and learned to "
                              "watch people order, which is design work.",
                              company="Eta", group="Why this company")
        assert out["outcome"] == "added", out


def test_answers_typed_without_verbatim_are_grouped_in_one_call():
    calls = []

    def many(questions):
        calls.append(questions)
        return [{"question": questions[0], "category": "normal",
                 "entry": {"question": "Why this company"}},
                {"question": questions[1], "category": "salary", "entry": None}]
    with note(migrated()) as n:
        old = answers.match_many
        answers.match_many = many
        old_out, old_jds = server.OUTPUT_DIR, server.JDS_DIR
        server.OUTPUT_DIR, server.JDS_DIR = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            r = server.app.test_client().post("/api/answers/learn", json={
                "items": [{"question": "Why Beta?",
                           "answer": "Beta is the tool I already open first."},
                          {"question": "Anything about pay we should know?",
                           "answer": "Happy to talk about it once we meet."}],
                "url": "https://job-boards.greenhouse.io/beta/jobs/1",
                "title": "Apply", "page_text": "Beta posting text. " * 30,
            }).get_json()
        finally:
            answers.match_many = old
            server.OUTPUT_DIR, server.JDS_DIR = old_out, old_jds
    assert len(calls) == 1, calls
    assert r["saved"] == 1 and r["lessons_pending"] == 0, r
    g = answers.find_group(answers.parse_repo(n.text), "Why this company")
    assert g["versions"][0]["text"] == "Beta is the tool I already open first."
    assert "pay" not in n.text


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
