"""Tests for tailoring on the user's own resume. Run: python3 tests/test_resume_template.py

A made-up page in the same shape as the user's resume.html; no Chrome, the
printer and page counter are stubs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resume_template as rt  # noqa: E402

PAGE = """<html><head><style>.x{}</style></head><body><div class="page">
<header><h1>Pat Doe</h1><p class="contact">pat@example.com</p></header>
<section><h2>Summary</h2>
<p class="summary">Designer with 8 years on mobile and car software. Tripled checkout completion.</p>
</section>
<section><h2>Experience</h2>
<div class="entry"><div class="entry-head"><h3 class="title">Designer, <span class="org">Solo</span></h3><span class="dates">07/26 - Present</span></div>
<ul>
<li><b>Tool.</b> Built a tool for agents. Raised use from 2% to 38%.</li>
</ul></div>
<div class="entry"><div class="entry-head"><h3 class="title">Senior Designer, <span class="org">Acme</span></h3><span class="dates">06/18 - 06/26</span></div>
<ul>
<li><b>Quote.</b> Tripled checkout completion (20% to 45%) with one question at a time.</li>
<li><b>App.</b> Raised setup completion from 60% to 80% and cut churn to 5%.</li>
</ul></div>
</section>
<section class="skills">
<h2>Skills</h2>
<p>Product&nbsp;Design, Prototyping, User&nbsp;Research</p>
</section>
</div></body></html>"""


def test_slots_are_summary_bullets_by_role_and_skills():
    s = rt.slots(PAGE)
    assert s["summary"]["max_words"] == 14          # 12 words, plus 10%
    assert [[b["id"] for b in r] for r in s["roles"]] == [["b1"], ["b2", "b3"]]
    assert s["roles"][1][0]["label"] == "Quote."
    assert s["skills"] == ["Product Design", "Prototyping", "User Research"]


def test_fill_rewords_reorders_and_keeps_labels_and_layout():
    page, notes = rt.fill(PAGE, {
        "summary": "Designer with 8 years on car and mobile software.",
        "bullets": [{"id": "b3", "text": "Cut churn to 5% and raised setup from 60% to 80%."}],
        "order": [["b1"], ["b3", "b2"]],
        "skills": ["User Research", "Product Design", "Prototyping"]})
    assert notes == [], notes
    assert "Designer with 8 years on car and mobile software." in page
    assert page.index("<b>App.</b> Cut churn") < page.index("<b>Quote.</b>")
    assert "User&nbsp;Research, Product&nbsp;Design" in page
    assert '<span class="dates">06/18 - 06/26</span>' in page and ".x{}" in page


def test_fill_refuses_edits_that_break_the_rules():
    page, notes = rt.fill(PAGE, {
        "summary": "word " * 40,
        "bullets": [{"id": "b9", "text": "A new project."},
                    {"id": "b2", "text": "Em" + chr(0x2014) + "dash gone."}],
        "order": [["b1"], ["b2"]],
        "skills": ["Product Design", "Prototyping", "Rocket Science"]})
    assert "Designer with 8 years on mobile and car software." in page   # too long: kept
    assert "A new project" not in page                                  # unknown id
    assert chr(0x2014) not in page
    assert page.index("<b>Quote.</b>") < page.index("<b>App.</b>")      # bad order: kept
    assert "Rocket" not in page
    assert len(notes) == 3, notes


def test_markdown_has_roles_bullets_and_skills_but_no_contact():
    md = rt.to_markdown(PAGE)
    assert "### Senior Designer, Acme · 06/18 - 06/26" in md
    assert "- **Quote.** Tripled checkout completion (20% to 45%)" in md
    assert "Product Design, Prototyping, User Research" in md
    assert "pat@example.com" not in md and "Pat Doe" not in md


def test_fit_puts_back_the_line_that_grew_most_until_one_page():
    printed = []
    edits = {"summary": "", "order": [], "skills": [],
             "bullets": [{"id": "b2", "text": "Tripled checkout completion (20% to 45%) with one question at a time, now."},
                         {"id": "b3", "text": "Raised setup completion from 60% to 80%."}]}

    def printer(page, path):
        printed.append(page)

    def pages(path):
        return 2 if "now." in printed[-1] else 1

    page, notes, n = rt.fit(PAGE, edits, "/tmp/x.pdf", printer, pages)
    assert n == 1 and len(printed) == 2
    assert "with one question at a time.</li>" in page          # b2 back to the user's words
    assert "from 60% to 80%.</li>" in page                      # b3 edit kept
    assert notes == ["Put back the original Quote. to keep one page."], notes


def test_design_systems_never_reaches_a_tailored_skills_line():
    # The test profile lists "Design Systems" in leave_out_skills.
    ds = PAGE.replace("Prototyping, User", "Prototyping, Design&nbsp;Systems, User")
    assert rt.slots(ds)["skills"] == ["Product Design", "Prototyping", "User Research"]
    page, notes = rt.fill(ds, {"skills": ["Design Systems", "User Research",
                                          "Product Design", "Prototyping"]})
    assert notes == [], notes
    assert "Design&nbsp;Systems" not in page and "Design Systems" not in rt.to_markdown(page)
    assert "User&nbsp;Research, Product&nbsp;Design, Prototyping" in page
    page, _ = rt.fill(ds, {})
    assert "Design&nbsp;Systems" not in page

if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("pass ", name)
            n += 1
    print("%d/%d passed" % (n, n))


def test_change_notes_are_refused():
    from resume_template import is_change_note
    for note in ["Tailored the resume for Salesforce's design systems team.",
                 "Emphasized growth work to match the job description.",
                 "Reordered bullets so the summary leads with activation."]:
        assert is_change_note(note), note
    for real in ["Senior product designer with 8 years shipping insurance, mobile and in-car software.",
                 "Tripled checkout completion and cut new driver churn from 30% to 10%.",
                 "Led UX on Contoso Maps, used in millions of cars."]:
        assert not is_change_note(real), real
