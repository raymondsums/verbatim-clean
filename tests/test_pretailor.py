"""Tests for the morning pre-tailoring. Run: python3 tests/test_pretailor.py

No model, no Chrome, no real files: tailoring and saving are stubs, and
every path points at a temp folder.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pretailor  # noqa: E402

MASTER = """## Bluebird Checkout

- CANONICAL metrics: tripled checkout completion
  (20% to 45%, 3x). Visitor conversion went from 0.5% to 2.5%, a 17x lift
  that bound 640 orders in one quarter
- The 0.4% to 3.1% (nearly 10x) conversion figure is wrong, and the
  1,000+ order count is retired

## Driver app

- Setup completion went from 60% to 80%, churn from 30% to 10%, with 5
  designers on the team, 2018 to 2026
"""

GOOD = ("## Experience\n\n### Senior Product Designer, Northwind · 06/18 - 06/26\n\n"
        "- **Bluebird Checkout.** Tripled checkout completion (20% to 45%). Visitor "
        "conversion went from 0.5% to 2.5% and 640 orders were bound.\n"
        "- Setup completion went from 60% to 80% with 5 designers, from 2018.\n")
LETTER = "I tripled checkout completion at Bluebird, from 20% to 45%. I would like to talk."


def test_wrong_and_retired_figures_are_caught_and_canonical_ones_pass():
    assert pretailor.bad_numbers(GOOD + LETTER, MASTER) == []
    bad = pretailor.bad_numbers("Conversion went from 0.4% to 3.1%, nearly 10x, "
                                "and 1,000+ orders. Also 42% more.", MASTER)
    assert bad == ["0.4%", "1000+", "10x", "3.1%", "42%"], bad


def test_voice_and_pages_decide_ready():
    d = tempfile.mkdtemp()
    one, two = os.path.join(d, "one.pdf"), os.path.join(d, "two.pdf")
    with open(one, "wb") as f:
        f.write(b"/Type /Pages /Type /Page")
    with open(two, "wb") as f:
        f.write(b"/Type /Pages /Type /Page /Type /Page")
    assert pretailor.check(GOOD, LETTER, one, MASTER)[0] is True
    ready, p = pretailor.check(GOOD, LETTER, two, MASTER)
    assert not ready and p["pages"] == 2
    ready, p = pretailor.check(GOOD, "It was seamless " + chr(0x2014) + " truly.", one, MASTER)
    assert not ready and {h["kind"] for h in p["voice"]} >= {"dash"}


def test_ruled_out_claims_block_ready():
    # The test profile rules out claiming to have built a "design system".
    d = tempfile.mkdtemp()
    one = os.path.join(d, "one.pdf")
    with open(one, "wb") as f:
        f.write(b"/Type /Pages /Type /Page")
    ready, p = pretailor.check(GOOD + "\n- Defined the standards for the Harbor design system.\n",
                               LETTER, one, MASTER)
    assert not ready and p["claims"], p
    assert pretailor.check(GOOD + "\n- Maintained the Harbor design system.\n",
                           LETTER, one, MASTER)[0]


def test_word_diff_marks_what_changed():
    assert pretailor.word_diff("a b c d", "a x c d e") == "a [-b-] {+x+} c d {+e+}"


def test_run_tailors_top_can_tailor_jobs_once_and_saves_checks():
    d = tempfile.mkdtemp()
    pretailor.STATE_PATH = os.path.join(d, "state.json")
    pretailor.MASTER_PATH = os.path.join(d, "master.md")
    pretailor.RESUME_HTML = os.path.join(d, "resume.html")
    with open(pretailor.MASTER_PATH, "w") as f:
        f.write(MASTER)
    with open(pretailor.RESUME_HTML, "w") as f:
        f.write("<html><body><li>Setup completion went from 60% to 80%.</li></body></html>")
    out = os.path.join(d, "output")
    jobs = [{"id": "a", "can_tailor": True}, {"id": "b", "can_tailor": False},
            {"id": "c", "can_tailor": True}, {"id": "d", "can_tailor": True},
            {"id": "e", "can_tailor": True}]
    tailored = []

    def posting(jid):
        return ("https://x.test/" + jid, "Co " + jid, "Designer", "A real posting " * 40)

    def tailor(text, company, role):
        tailored.append(company)
        return {"resume_md": GOOD, "cover_letter_md": LETTER, "notes": ""}

    def save(text, url, company, role, result):
        oid = company.replace(" ", "-")
        os.makedirs(os.path.join(out, oid))
        with open(os.path.join(out, oid, "resume.pdf"), "wb") as f:
            f.write(b"/Type /Pages /Type /Page")
        return {"id": oid, "letter": {"versions": [{"text": LETTER}]},
                "files": {"resume_pdf": "/output/%s/resume.pdf" % oid}}

    done = pretailor.run(lambda: {"jobs": jobs}, posting, tailor, save, out, log=lambda *_: None)
    assert done == ["a", "c", "d"], done            # b has no JD; top 3 only
    state = pretailor.load_state()
    assert state["a"]["ready"] is True and state["a"]["problems"]["pages"] == 1
    diff = open(os.path.join(out, "Co-a", "resume-diff.txt")).read()
    assert "{+" in diff and "Setup completion went from 60% to" in diff
    assert json.load(open(os.path.join(out, "Co-a", "pretailor.json")))["out_id"] == "Co-a"
    # The next morning skips what was already made.
    again = pretailor.run(lambda: {"jobs": jobs}, posting, tailor, save, out, log=lambda *_: None)
    assert again == ["e"] and tailored == ["Co a", "Co c", "Co d", "Co e"]


# Four made runs, with posting links in the shapes real boards use.
MADE = [
    {"id": "greenhouse:acmefin:1234567", "company": "Acmefin", "role": "Product Designer",
     "url": "https://www.acmefin.com/careers/current-openings/job?gh_jid=1234567&gh_jid=1234567"},
    {"id": "greenhouse:paywise:2345678", "company": "Paywise", "role": "Product Designer, Risk",
     "url": "https://paywise.com/jobs/search?gh_jid=2345678"},
    {"id": "workday:cloudco|wd12|External_Career_Site:/job/New-York---New-York/Lead-UI-UX-Designer_JR100200",
     "company": "Cloudco", "role": "Lead UI/UX Designer",
     "url": "https://cloudco.wd12.myworkdayjobs.com/External_Career_Site/job/New-York---New-York/Lead-UI-UX-Designer_JR100200"},
    {"id": "serpapi:design:eyJleGFtcGxlIjoxfQ", "company": "Threadco, Inc.",
     "role": "Sr. UX Designer", "url": "https://www.ziprecruiter.com/c/threadco/Job/Sr.-UX-Designer/-in-San-Francisco,CA?jid=0a1b2c3d4e5f6071&utm_source=google_jobs_apply"},
]


def test_tailored_resume_matches_only_its_own_application_page():
    hit = lambda url, title="": (pretailor.match_page(url, title, MADE) or {}).get("company")
    # The right job, on its posting and on the pages applying to it goes through.
    assert hit("https://acmefin.com/careers/current-openings/job?gh_jid=1234567") == "Acmefin"
    assert hit("https://job-boards.greenhouse.io/acmefin/jobs/1234567") == "Acmefin"
    assert hit("https://paywise.com/jobs/listing/product-designer-risk/2345678/apply") == "Paywise"
    assert hit("https://cloudco.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/"
               "New-York---New-York/Lead-UI-UX-Designer_JR100200/apply") == "Cloudco"
    assert hit("https://www.ziprecruiter.com/c/threadco/Job/Sr.-UX-Designer/-in-San-Francisco,CA?jid=0a1b2c3d4e5f6071") == "Threadco, Inc."
    # Another company, another job at the same company, or a look-alike id: none.
    assert hit("https://job-boards.greenhouse.io/otherco/jobs/1234567") is None
    assert hit("https://job-boards.greenhouse.io/acmefin/jobs/1234568") is None
    assert hit("https://acmefin.com/careers/current-openings/job?gh_jid=7000001") is None
    assert hit("https://paywise.com/jobs/listing/product-designer-terminal/3456789/apply") is None
    assert hit("https://jobs.lever.co/otherco2/2345678-0") is None
    assert hit("https://www.threadco.com/careers/job/12345") is None
    assert hit("not a url") is None


def test_two_jobs_on_one_page_means_the_standard_resume():
    twins = MADE[:1] + [dict(MADE[0], id="greenhouse:acmefin-2:1234567")]
    assert pretailor.match_page("https://job-boards.greenhouse.io/acmefin/jobs/1234567", "", twins) is None


def test_files_only_for_ready_runs_with_a_pdf():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "run-a"))
    assert pretailor.files({"ready": True, "out_id": "run-a"}, d) is None      # no PDF yet
    open(os.path.join(d, "run-a", "resume.pdf"), "wb").close()
    got = pretailor.files({"ready": True, "out_id": "run-a", "company": "Co"}, d)
    assert got["resume"] == "/output/run-a/resume.pdf" and got["letter"] == ""
    open(os.path.join(d, "run-a", "cover-letter.pdf"), "wb").close()
    assert pretailor.files({"ready": True, "out_id": "run-a"}, d)["letter"] == "/output/run-a/cover-letter.pdf"
    assert pretailor.files({"ready": False, "out_id": "run-a"}, d) is None


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("pass ", name)
            n += 1
    print("%d/%d passed" % (n, n))
