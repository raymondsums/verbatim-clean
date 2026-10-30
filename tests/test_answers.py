"""Tests for the answer repo matching. Run: python3 tests/test_answers.py

Nothing here talks to Obsidian or the Claude CLI. The repo text is stubbed
and the guarded categories are settled by regex before any model call, so
these run offline and fast.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import answers  # noqa: E402

REPO = """# Application Answers

## Why this company

**Current answer**
Because the product is one I would use myself.

**Used**
- Umbrella, Product Designer, 2026-08-06

**Seen as**
- "What draws you to Umbrella?"
"""


class repo:
    """Stub the Obsidian read, and fail loudly if the CLI is reached."""

    def __init__(self, text=REPO, allow_cli=False):
        self.text, self.allow_cli = text, allow_cli

    def __enter__(self):
        self.old_read = answers.read_repo
        self.old_bin = answers.CLAUDE_BIN
        answers.read_repo = lambda: self.text
        if not self.allow_cli:
            answers.CLAUDE_BIN = "/nonexistent/claude"
        return self

    def __exit__(self, *exc):
        answers.read_repo = self.old_read
        answers.CLAUDE_BIN = self.old_bin


def test_guarded_questions_never_reach_the_model():
    with repo():
        out = answers.match_many([
            "Will you now or in the future require visa sponsorship?",
            "What are your salary expectations?",
            "How would you describe your gender identity?",
        ])
    assert [o["category"] for o in out] == ["authorization", "salary", "eeo"], out
    assert all(o["entry"] is None for o in out), out


def test_guarded_questions_are_answered_without_obsidian():
    # All questions guarded means no repo read at all, so a closed Obsidian
    # cannot break the sponsorship and salary flags.
    def boom():
        raise answers.AnswersError("Obsidian is not reachable")

    old = answers.read_repo
    answers.read_repo = boom
    try:
        out = answers.match_many(["Do you require sponsorship?"])
    finally:
        answers.read_repo = old
    assert out[0]["category"] == "authorization", out


def test_empty_repo_matches_nothing_without_a_model_call():
    with repo(text="# Application Answers\n"):
        out = answers.match_many(["Why do you want to work here?"])
    assert out[0]["entry"] is None and out[0]["category"] == "normal", out


def test_repo_parses_into_entries():
    entries = answers.parse_repo(REPO)
    assert len(entries) == 1, entries
    e = entries[0]
    assert e["question"] == "Why this company"
    assert e["answer"].startswith("Because the product")
    assert e["used"] == ["Umbrella, Product Designer, 2026-08-06"]
    assert e["seen_as"] == ["What draws you to Umbrella?"]


def test_obsidian_down_is_a_plain_message_not_a_crash():
    import requests
    old = answers.requests.get
    old_key = os.environ.get("OBSIDIAN_API_KEY")
    os.environ["OBSIDIAN_API_KEY"] = "test-key"  # get past the header check

    def refuse(*a, **kw):
        raise requests.exceptions.ConnectionError("refused")

    answers.requests.get = refuse
    old_cache = answers.CACHE_PATH
    answers.CACHE_PATH = os.path.join(tempfile.mkdtemp(), "none.md")
    try:
        answers.read_repo()
    except answers.AnswersError as e:
        assert "Obsidian is not reachable" in str(e), e
    else:
        raise AssertionError("a closed Obsidian did not raise AnswersError")
    finally:
        answers.requests.get = old
        answers.CACHE_PATH = old_cache
        if old_key is None:
            os.environ.pop("OBSIDIAN_API_KEY", None)
        else:
            os.environ["OBSIDIAN_API_KEY"] = old_key


class offline:
    """Obsidian closed (or open, with a note held in memory), with the cache
    and the write queue in a temp dir."""

    def __init__(self, cache=None, up=False, note=REPO):
        self.cache, self.up, self.note = cache, up, note
        self.puts = []

    def __enter__(self):
        import requests
        d = tempfile.mkdtemp()
        self.old = (answers.CACHE_PATH, answers.QUEUE_PATH,
                    answers.requests.get, answers.requests.put,
                    os.environ.get("OBSIDIAN_API_KEY"))
        answers.CACHE_PATH = os.path.join(d, "cache.md")
        answers.QUEUE_PATH = os.path.join(d, "queue.json")
        os.environ["OBSIDIAN_API_KEY"] = "test-key"
        if self.cache is not None:
            with open(answers.CACHE_PATH, "w") as f:
                f.write(self.cache)

        class Resp:
            def __init__(self, text="", status=200):
                self.text, self.status_code = text, status
                self.ok = status < 400

        def get(*a, **kw):
            if not self.up:
                raise requests.exceptions.ConnectionError("refused")
            return Resp(self.note)

        def put(url, headers=None, data=b"", **kw):
            if not self.up:
                raise requests.exceptions.ConnectionError("refused")
            self.note = data.decode("utf-8")
            self.puts.append(self.note)
            return Resp()

        answers.requests.get, answers.requests.put = get, put
        return self

    def __exit__(self, *exc):
        (answers.CACHE_PATH, answers.QUEUE_PATH, answers.requests.get,
         answers.requests.put, key) = self.old
        if key is None:
            os.environ.pop("OBSIDIAN_API_KEY", None)
        else:
            os.environ["OBSIDIAN_API_KEY"] = key


def test_closed_obsidian_reads_the_last_copy():
    with offline(cache=REPO):
        assert answers.read_repo() == REPO


def test_a_good_read_refreshes_the_cache():
    with offline(up=True):
        answers.read_repo()
        with open(answers.CACHE_PATH) as f:
            assert f.read() == REPO


def test_answers_saved_while_closed_land_on_the_next_read():
    with offline(cache=REPO) as o:
        out = answers.record("Describe a project you are proud of",
                             "The one where the dashboard got simpler.",
                             company="Example Co")
        assert out == "created", out
        assert os.path.exists(answers.QUEUE_PATH)
        # matching sees it straight away, from the cache
        assert "Describe a project" in answers.read_repo()
        assert o.puts == []

        o.up = True
        text = answers.read_repo()
        assert "Describe a project you are proud of" in text, text
        assert "Example Co" in o.note and o.puts, o.puts
        assert not os.path.exists(answers.QUEUE_PATH)
        # flushed once, not again on the next read
        answers.read_repo()
        assert len(o.puts) == 1, len(o.puts)


def test_facts_are_not_answers():
    for q, a in [
        ("LinkedIn Profile URL", "https://www.linkedin.com/in/someone-x"),
        ("Anything else we should see?", "https://example.com/portfolio/case"),
        ("Preferred contact", "someone.example@example.com"),
        ("Best number to reach you", "(555) 010-0000"),
        ("Website", "a long description that is still a fact field"),
    ]:
        assert answers.looks_like_fact(q, a), (q, a)
        try:
            answers.record(q, a)
        except answers.AnswersError as e:
            assert "fact" in str(e), e
        else:
            raise AssertionError(f"stored a fact: {q}")
    assert not answers.looks_like_fact(
        "Why do you want to work here?",
        "Because the product is one I would use myself, every day.")


def test_drafts_never_name_the_tool_or_make_a_ruled_out_claim():
    # The test profile (tests/fixtures/profile.test.json) rules out
    # claiming to have built or led a "design system".
    blocked = ["Verbatim fills these forms for me.", "I built the Harbor design system.",
               "I led our design system work.",
               "I made a job application extension.", "I built the design system.",
               "Led the Harbor design system.", "Owned our design system end to end.",
               "I defined the standards for the Harbor design system.",
               "created a design system from scratch.",
               # Claims a narrower pattern let through.
               "I created and maintained the design system.",
               "I led a team that built the design system.",
               "We built a design system for the app.", "I've built two design systems.",
               "I'm leading the design system work.",
               "Having built our design system, I moved on.",
               "I was the one who built our design system.",
               "I set up the Harbor design system.",
               "I built out the Harbor design system's components.",
               "Owner of the Harbor design system.",
               "I architected a new design system for Northwind.",
               "As lead, I established our design system.",
               "My team and I built the Harbor design system.",
               "I designed the Harbor design system.", "Started our design system in 2019.",
               "Built Verbatim, a job tool.", "My job application tool drafts answers."]
    kept = ["I maintained the Harbor design system.",
            "Checkout completion tripled, from 20% to 45%.", "I built Sketchpad, a browser extension.",
            "I quoted customers verbatim in the research readout.",
            "Leading the accessibility audit, I maintained the Harbor design system.",
            "I partnered with the lead engineer on our design system.",
            "My own design system work was maintenance of Harbor.",
            "I designed for the Harbor design system.",
            "I designed screens using the Harbor design system.",
            "I contributed components to the Harbor design system."]
    for t in blocked:
        assert answers._never_claim(t), t
    for t in kept:
        assert not answers._never_claim(t), t


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
