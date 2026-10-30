"""Tests for voice samples, how much the user changed each draft, and the mic.
Run: python3 tests/test_voice_learning.py

Nothing reads real notes, a real portfolio, the logs or the model:
_isolate points every path at temp folders and model calls are stubbed.
"""

import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import answers  # noqa: E402
import dictate  # noqa: E402
import server  # noqa: E402
import voicesamples  # noqa: E402
from test_groups import JD, MASTER, migrated, note  # noqa: E402

DASH = chr(0x2014)


def fresh_dirs():
    """A temp vault and portfolio with the user's notes in them, and an empty
    cache, so each test builds its own samples."""
    d = tempfile.mkdtemp()
    vault, folio = os.path.join(d, "Vault"), os.path.join(d, "portfolio")
    os.makedirs(vault)
    os.makedirs(folio)
    voicesamples.CACHE_PATH = os.path.join(d, "voice_samples.json")
    voicesamples.SPOKEN_LOG = os.path.join(d, "spoken.jsonl")
    voicesamples.PORTFOLIO_DIR = folio
    voicesamples.VAULT_NOTES = ("Team Mission.md", "Draft Ideas.md")
    voicesamples.CASE_STUDIES = ("one.html", "two.html", "three.html")
    with open(os.path.join(vault, "Team Mission.md"), "w") as f:
        f.write("Prices went up every year and people noticed. We want to "
                "turn that around. You pay for what you use, which is fair. "
                "Bluebird tries to help before things go wrong. You get "
                "control over the price by how you use it.\n")
    with open(os.path.join(vault, "Draft Ideas.md"), "w") as f:
        f.write("- This line was written by Claude and has many many words "
                "in it so it would be long enough to be a sample of text\n")
    with open(os.path.join(vault, "Private Journal.md"), "w") as f:
        f.write("Private words that must never reach a job application "
                "prompt, long enough to be picked if the list allowed it.\n")
    page = ("<html><nav><p>Home Work About Contact and other links in the "
            "navigation bar here</p></nav><main>"
            "<p>I rebuilt the checkout flow one question at a time, and quote "
            "completion went from 20% to 45% while I was the only designer "
            "on the team.</p>"
            "<p>This copy has a dash " + DASH + " in it and plenty of words "
            "so it would otherwise count as a sample.</p>"
            "<p>The work was seamless and it was ultimately a robust and "
            "holistic result for every single person who used it.</p>"
            "</main><footer><p>Copyright footer text with enough words to "
            "count as a sample if footers were read.</p></footer></html>")
    for name in voicesamples.CASE_STUDIES[:3]:
        with open(os.path.join(folio, name), "w") as f:
            f.write(page)
    return vault, folio


# ---------- voice samples ----------

def test_samples_come_from_own_notes_and_case_studies_only():
    vault, _ = fresh_dirs()
    got = voicesamples.build(vault)
    texts = " ".join(s["text"] for s in got)
    assert {s["from"] for s in got} == {"vault", "portfolio"}, got
    assert "Prices went up every year" in texts
    assert "completion went from 20% to 45%" in texts
    assert "Private words" not in texts       # not on the list
    assert "Claude" not in texts              # not the user's
    assert DASH not in texts                  # edited copy
    assert "seamless" not in texts            # AI tells
    assert "navigation bar" not in texts and "Copyright" not in texts


def test_listed_notes_are_found_in_vault_folders_and_nothing_else_is():
    vault, _ = fresh_dirs()
    os.makedirs(os.path.join(vault, "Portfolio"))
    os.makedirs(os.path.join(vault, "Personal"))
    os.makedirs(os.path.join(vault, ".trash"))
    os.replace(os.path.join(vault, "Team Mission.md"),
               os.path.join(vault, "Portfolio", "Team Mission.md"))
    with open(os.path.join(vault, "Personal", "Journal.md"), "w") as f:
        f.write("Private words that must never reach a job application "
                "prompt, long enough to be picked if the list allowed it.\n")
    with open(os.path.join(vault, ".trash", "Team Mission.md"), "w") as f:
        f.write("Deleted note words that sit in the trash folder and are long "
                "enough to count as a sample of the user's writing if read.\n")
    got = voicesamples.build(vault)
    texts = " ".join(s["text"] for s in got)
    assert "Prices went up every year" in texts, got     # moved into a folder
    assert "Private words" not in texts        # off the list, any folder
    assert "trash folder" not in texts         # hidden folders skipped


def test_cached_portfolio_samples_stay_when_the_portfolio_is_missing():
    vault, folio = fresh_dirs()
    voicesamples.build(vault)
    voicesamples.PORTFOLIO_DIR = os.path.join(folio, "missing")
    again = voicesamples.build(vault)
    assert any(s["from"] == "portfolio" for s in again), again


def test_pick_is_short_and_prefers_what_they_typed_or_said():
    vault, _ = fresh_dirs()
    log = os.path.join(tempfile.mkdtemp(), "versions.jsonl")
    with open(log, "w") as f:
        f.write(json.dumps({"group": "Why this company", "inserted": "",
                            "typed": True, "sent": "I want to work on cars "
                            "because I have shipped navigation for Contoso and "
                            "I still like the problem of a screen in a car."}) + "\n")
        # Typed by someone else (a tool), or a draft the user edited: not samples.
        f.write(json.dumps({"group": "x", "inserted": "", "typed": False,
                            "sent": "Leveraging my passion " * 10}) + "\n")
        f.write(json.dumps({"group": "x", "inserted": "a draft", "typed": True,
                            "sent": "Edited draft words " * 10}) + "\n")
    voicesamples.log_spoken("Why us?", "so um the thing I keep coming back "
                            "to is that the car is a place people spend real "
                            "time in and the screen should just get out of "
                            "the way")
    got = voicesamples.pick("Why do you want to work on cars?", JD, vault, log)
    assert 1 <= len(got) <= voicesamples.PICK, got
    assert got[0].startswith("I want to work on cars"), got
    assert any("the car is a place" in g for g in got), got
    assert not any("Leveraging" in g or "Edited draft" in g for g in got), got
    assert all(len(g.split()) <= voicesamples.SAMPLE_WORDS for g in got)


def test_voice_samples_note_gives_only_own_quoted_text_and_ranks_first():
    vault, _ = fresh_dirs()
    os.makedirs(os.path.join(vault, "Voice Samples"))
    with open(os.path.join(vault, "Voice Samples", "Voice Samples.md"), "w") as f:
        f.write("# Voice Samples\n\nKept by hand, intro words that are "
                "not the user's writing and long enough to count as a sample.\n\n"
                "## 1. Car answer\n\n**Source:** an answer.\n\n"
                "> Working on cars is where I feel most at home. I drive an EV\n"
                "> and charge it a few times a week, so I know the pain points.\n\n"
                "**What it shows:**\n- Side notes about the sample that are "
                "long enough to be picked if they were read at all.\n\n"
                "## 2. Too short\n\n> Four words only here.\n\n"
                "## 3. Intro\n\n**Source:** pasted. **Provenance unconfirmed:** "
                "checking.\n\n> Polished intro words that might have been "
                "touched by a tool and are long enough to count as a sample.\n\n"
                "## 4. Bio\n\n**Source:** AI-polished, approved by the user.\n\n"
                "> Smoothed bio words that a tool rewrote for the user and that are "
                "long enough to count as a sample here.\n")
    got = voicesamples.pick("Why do you want to work on cars?", JD, vault,
                            os.path.join(vault, "none.jsonl"))
    assert got[0].startswith("Working on cars is where I feel most at home. "
                             "I drive an EV and charge"), got
    assert not any("Side notes" in g or "Kept by hand" in g or "Four words" in g or "Polished" in g
                   or "Smoothed" in g
                   for g in got), got
    # Read fresh each time, not from the weekly cache.
    assert not any(s["from"] == "written" for s in voicesamples.build(vault))


def test_samples_are_style_only_never_a_source():
    vault, _ = fresh_dirs()
    sample = ("I rebuilt the checkout flow one question at a time, and quote "
              "completion went from 20% to 45% while I was the only designer.")
    seen = {}

    def cli(system, prompt):
        seen["system"], seen["prompt"] = system, prompt
        return {"ask": "", "sentences": [
            {"text": "I was the only designer on a checkout flow rebuild.",
             "source": "past answer", "quote": "I was the only designer",
             "new_paragraph": False}]}
    with note(migrated(), cli):
        out = answers.draft_field("Tell us about a project", JD, MASTER,
                                  samples=[sample])
    assert "Style only" in seen["prompt"] and sample in seen["prompt"]
    assert "style" in seen["system"] and "Never take a fact" in seen["system"]
    # The sample text is not a source, so the claim from it is flagged.
    assert out["sentences"][0]["sourced"] is False, out


def test_outline_is_shown_for_layout_but_is_not_a_source():
    fresh_dirs()
    outline = "First the problem, then the tradeoff, then the result at Initech."
    seen = {}

    def cli(system, prompt):
        seen["prompt"] = prompt
        return {"ask": "", "sentences": [
            {"text": "I worked on the result at Initech.", "source": "past answer",
             "quote": "the result at Initech", "new_paragraph": False}]}
    with note(migrated(), cli):
        out = answers.draft_field("Tell us about a project", JD, MASTER, outline=outline)
    assert "laid out" in seen["prompt"] and outline in seen["prompt"]
    assert "nothing in it counts as true" in seen["prompt"]
    assert out["sentences"][0]["sourced"] is False, out


def test_no_samples_leaves_the_prompt_as_it_was():
    def cli(system, prompt):
        assert "How the user writes" not in prompt
        return {"ask": "", "sentences": [
            {"text": "I tripled checkout completion at Bluebird.", "source": "master",
             "quote": "3x checkout completion", "new_paragraph": False}]}
    with note(migrated(), cli):
        answers.draft_field("Tell us about a project", JD, MASTER)


# ---------- how much the user changed ----------

def test_changed_share_counts_words_not_commas():
    a = "I build tools for myself and use them every day."
    assert answers.changed_share(a, a) == 0
    assert answers.changed_share(a, a.replace("myself and", "myself, and")) == 0
    assert answers.changed_share(a, "Completely different words here now.") == 100
    half = answers.changed_share(a, "I build tools for myself.")
    assert 20 <= half <= 50, half


def test_capture_logs_where_the_text_came_from():
    with note(migrated()):
        answers.capture("Why Beta?", "Beta makes a tool I would use myself.",
                        company="Beta", group="Why this company",
                        inserted="Beta makes a tool.", inserted_from="draft")
        answers.capture("What else?", "Something I typed myself, all of it.",
                        company="Beta", group="Anything else", typed=True)
        with open(answers.VERSIONS_LOG) as f:
            rows = [json.loads(x) for x in f]
    assert rows[0]["from"] == "draft" and rows[0]["typed"] is False
    assert rows[1]["typed"] is True and rows[1]["inserted"] == ""


def test_answers_page_shows_how_much_he_changed_each_draft():
    with note(migrated()):
        answers.capture("Why Beta?", "I build tools for myself, and Beta is "
                        "one I would build.", company="Beta",
                        group="Why this company",
                        inserted="I build tools for myself.",
                        inserted_from="draft")
        answers.capture("Why Gamma?", "Gamma is where I want my next ten "
                        "years to go, honestly.", company="Gamma",
                        group="Why this company",
                        inserted="I build tools for myself.",
                        inserted_from="draft")
        # The user's own best answer put in the box is not a draft.
        answers.capture("Why Delta?", "My best answer, changed a lot for "
                        "Delta this time around.", company="Delta",
                        group="Why this company",
                        inserted="I build tools for myself.",
                        inserted_from="best")
        old = server._outcomes
        server._outcomes = lambda: None
        try:
            d = server.app.test_client().get("/api/answers/groups").get_json()
        finally:
            server._outcomes = old
    g = next(x for x in d["groups"] if x["title"] == "Why this company")
    assert len(g["changes"]) == 2, g["changes"]
    assert g["changes"][0] < g["changes"][1], g["changes"]
    by = {v["company"]: v["changed"] for v in g["versions"]}
    assert by["Beta"] == g["changes"][0] and by["Gamma"] == g["changes"][1], by
    assert by["Delta"] is None, by


def test_answers_page_copy_has_no_dashes():
    with open(os.path.join(os.path.dirname(server.__file__), "index.html")) as f:
        page = f.read()
    start = page.index("function changedWords")
    chunk = page[start:page.index("function versionHtml")]
    assert DASH not in chunk and chr(0x2013) not in chunk
    assert "You changed about" in chunk


# ---------- saying an answer ----------

def test_tidy_keeps_own_words_or_gives_them_back_raw():
    raw = ("so um I think the the main thing is I like building tools "
           "for myself you know and I use them every day")
    with note(migrated(), lambda s, p: {"text": "I like building tools for "
                                        "myself, and I use them every day."}):
        out = dictate.tidy(raw, "Why us?")
    assert out == {"text": "I like building tools for myself, and I use "
                   "them every day.", "tidied": True}, out
    # The tidy pass brought in words the user did not say: the user's raw words win.
    with note(migrated(), lambda s, p: {"text": "I am passionate about "
                                        "crafting delightful tools."}):
        out = dictate.tidy(raw, "Why us?")
    assert out == {"text": raw, "tidied": False}, out


def test_the_dictate_route_turns_audio_into_own_words():
    old = dictate.transcribe
    dictate.transcribe = lambda audio, mime="": (
        "um I have shipped navigation for Contoso and I like working on cars")
    try:
        with note(migrated(), lambda s, p: {"text": "I have shipped "
                                            "navigation for Contoso, and I "
                                            "like working on cars."}):
            c = server.app.test_client()
            r = c.post("/api/answers/dictate", json={
                "audio": base64.b64encode(b"fake").decode(),
                "mime": "audio/webm", "question": "Why us?",
                "limit": {"words": 50}}).get_json()
            raw_only = c.post("/api/answers/dictate", json={
                "audio": base64.b64encode(b"fake").decode(),
                "question": "Why us?", "tidy": False}).get_json()
            bad = c.post("/api/answers/dictate", json={"audio": "%%%"})
    finally:
        dictate.transcribe = old
    assert r["tidied"] and r["text"].startswith("I have shipped"), r
    assert r["raw"].startswith("um I have"), r
    assert raw_only["tidied"] is False and raw_only["text"] == raw_only["raw"]
    assert bad.status_code == 400


def test_no_speech_tools_is_a_plain_error_not_a_crash():
    old = dictate.WHISPER
    dictate.WHISPER = "/nowhere/whisper-cli"
    try:
        r = server.app.test_client().post("/api/answers/dictate", json={
            "audio": base64.b64encode(b"fake").decode()})
    finally:
        dictate.WHISPER = old
    assert r.status_code == 502 and "not set up" in r.get_json()["error"]


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
