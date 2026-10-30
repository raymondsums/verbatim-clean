"""Source tags, the prompt filter, interview notes, held stories, the
motivation file and the vault snapshot fallback. All local, no model."""

import os
import tempfile

import _isolate  # noqa: F401  keeps the real vault and .env out

import answers
import interviewnotes
import vaultio

NOTE = """# Application Answers

## A project you led
Best source: jobright

**Current best answer**

I seamlessly led everything.

**Lessons**
- Say the number.

**Seen as**

**Versions**

##### 2026-10-19 · Beta
Source: mine

I led the quote redesign at Brightline and it tripled completion.

##### 2026-08-07 · Acme
Source: jobright
JD: [snapshot](https://example.com/jd.txt)

I seamlessly led everything.
"""


def test_tags_round_trip_and_only_own_words_feed_prompts():
    g = answers.parse_repo(NOTE)[0]
    assert g["best_source"] == "jobright"
    assert [v["source"] for v in g["versions"]] == ["mine", "jobright"]
    assert g["versions"][1]["jd"] == "https://example.com/jd.txt"
    p = answers.for_prompts(g)
    assert p["answer"].startswith("I led the quote redesign")
    assert [v["company"] for v in p["versions"]] == ["Beta"]
    again = answers.parse_group(answers.render_group(g)[3:])
    assert again["best_source"] == "jobright"
    assert [v["source"] for v in again["versions"]] == ["mine", "jobright"]


def test_pasted_posting_text_never_lands_in_the_jd_line():
    g = answers.parse_repo(NOTE)[0]
    g["versions"][0]["jd"] = "Umbrella is growing our team of passionate creatives"
    assert "JD:" not in answers.render_group(g).split("· Beta")[1].split("#####")[0]


def test_counts_as_own():
    assert answers.counts_as_own("mine")
    assert answers.counts_as_own("verbatim-draft, edited 34%")
    assert not answers.counts_as_own("verbatim-draft, edited 2%")
    assert not answers.counts_as_own("jobright")
    assert not answers.counts_as_own("jobright-edited")
    assert not answers.counts_as_own("")


def test_jobright_edited_lends_its_layout_only():
    edited = NOTE.replace("Source: mine", "Source: jobright").replace(
        "Best source: jobright", "Best source: jobright-edited")
    g = answers.parse_repo(edited)[0]
    assert g["best_source"] == "jobright-edited"
    assert answers.for_prompts(g) is None or not answers.for_prompts(g).get("answer")
    assert answers.own_versions(g) == []
    assert answers.outline_for(g) == "I seamlessly led everything."
    # One of the user's own answers wins, so the layout is not needed.
    assert answers.outline_for(answers.parse_repo(NOTE)[0]) == ""
    assert answers.outline_for(None) == ""


def test_source_for_a_sent_answer():
    draft = "I led the quote redesign at Brightline and it tripled completion."
    assert answers.source_for(draft, draft, "draft") == "verbatim-draft, edited 0%"
    assert answers.source_for("I typed this myself, no tool here.", typed=True) == "mine"
    assert answers.source_for("It works seamlessly across teams.") == "jobright"
    assert answers.source_for("Pasted from somewhere, who knows.") == "unknown"
    assert answers.source_for(draft, draft, "best", stored_source="mine") == "mine"
    assert answers.source_for(draft, draft, "best", stored_source="jobright") == "jobright"


def test_sources_op_sets_tags_by_key():
    g = answers.parse_repo(NOTE)[0]
    key = answers.entry_key(g, g["versions"][1])
    text, n = answers._apply_op(NOTE, {"op": "sources", "tags": {key: "mine"}})
    assert n == 1
    assert answers.parse_repo(text)[0]["versions"][1]["source"] == "mine"


def test_why_company_and_cover_questions_are_company_specific():
    assert answers.company_specific("Why do you want to join Umbrella?")
    assert answers.company_specific("What excites you about this role?")
    assert answers.company_specific("Additional information", "cover note")
    assert not answers.company_specific("Tell us about a project you led")


def test_held_back_stories_are_not_offered():
    text = answers.STORY_SEED + "\n" + answers.render_story(
        {"title": "Real one", "situation": "s", "action": "a", "result": ""}) \
        + "\n" + answers.render_story({"title": "Laundered", "situation": "s",
                                       "action": "a", "result": "",
                                       "held": "from a Jobright answer"})
    old = answers.read_note
    answers.read_note = lambda rel: text
    try:
        assert [s["title"] for s in answers.list_stories()] == ["Real one"]
        assert len(answers.list_stories(held=True)) == 2
        assert answers.find_story("laundered") is None
    finally:
        answers.read_note = old


def test_motivation_drops_deal_breakers(tmp_path):
    p = tmp_path / "motivation.md"
    p.write_text("# Title\nSam's own words.\n\n## Why I'm looking\nNext challenge.\n"
                 "(Note: check this.)\n\n## Deal breakers\nNo dev tools.\n")
    old = answers.MOTIVATION_PATH
    answers.MOTIVATION_PATH = str(p)
    try:
        got = answers.load_motivation()
    finally:
        answers.MOTIVATION_PATH = old
    assert "Next challenge." in got
    assert "dev tools" not in got and "Note:" not in got and "Title" not in got


INTERVIEW = """# Acme prep

## Script: "Tell me about yourself"
I'm Sam, a product designer with eight years of experience. I worked on
car navigation at Northwind and the Brightline app, and I like hard constraints a lot.
**Principles:** lead with identity.

## Script: Visa / Sponsorship question
I need sponsorship for this role, and that is all this line says about it.

## Script: "Why Acme?"
Acme is great because it is Acme and I love Acme a lot, many many words here.

## Gating answers, word for word
**Why available now**
"My old team at Northwind wound down last spring, so I am free now and being choosy about which team I join next, since I want to get it right."
**Start date**
"I can start right away, whenever the team is ready for me to begin." Lock it fast, it is a real filter for them.
Do not volunteer a constraint you do not have to, ever, in this call.
**Comp**
"My range is $999k, said every single time without blinking at all here."
**Why Acme**
"Two reasons about Acme only, that are about Acme and nobody else at all."
"""


def test_interview_notes_keep_own_answers_and_drop_the_rest():
    secs = dict(interviewnotes.sections("Interview Acme", INTERVIEW))
    body = "\n".join(secs.values())
    assert any("Tell me about yourself" in k for k in secs)
    assert "Principles" not in body
    assert "sponsorship" not in body.lower() and "this role, and that" not in body
    assert "$" not in body and "Acme is great" not in body
    assert "Two reasons about Acme" not in body
    assert "wound down last spring" in body
    assert "I can start right away" in body
    assert "Lock it fast" not in body and "Do not volunteer" not in body


def test_vault_reads_fall_back_to_the_snapshot(monkeypatch, tmp_path):
    snap = tmp_path / "snap"
    (snap / "Interviews").mkdir(parents=True)
    (snap / "Writing in My Voice.md").write_text("voice")
    monkeypatch.setattr(vaultio, "SNAPSHOT_DIR", str(snap))
    real_open = open

    def refuse(path, *a, **k):
        if str(path).startswith(str(tmp_path / "vault")):
            raise PermissionError(1, "Operation not permitted")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", refuse)
    assert vaultio.read(str(tmp_path / "vault"), "Writing in My Voice.md") == "voice"
    assert vaultio.status() == "snapshot"
    try:
        vaultio.read(str(tmp_path / "vault"), "Job Search/Application Answers.md")
    except vaultio.Refused:
        pass
    else:
        raise AssertionError("no snapshot of the answer note should mean Refused")
