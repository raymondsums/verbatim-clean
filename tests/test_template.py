"""The template is a contract, not a suggestion.

pdf.py owns the layout and profile.json owns the identity facts. The model
supplies content only. These tests feed deliberately wrong model output
through the conformers and assert the document shape survives.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tailor import conform_letter_md, conform_resume_md


MESSY = """## Contact
Jordan Lee
you@example.com · 555 010 0199 · www.jordanlee.example

## Skills
Product Design, Prototyping

Figma, Cursor

## Experience

### Senior Product Designer • Northwind, Inc. • 06/2018 - 06/2026

- **Bluebird Checkout.** Took completion from 20% to 45%.

## Awards
Some award the template has no room for.

## Summary
Product designer with 8 years.

## Education
- Example University · M.S. HCI · 2018
"""


def test_sections_reordered_to_template_order():
    md, _ = conform_resume_md(MESSY)
    order = [l for l in md.split("\n") if l.startswith("## ")]
    assert order == ["## Summary", "## Experience", "## Education", "## Skills"]


def test_unexpected_sections_dropped_and_reported():
    md, warns = conform_resume_md(MESSY)
    assert "Awards" not in md
    assert "Some award" not in md
    assert any("Awards" in w for w in warns)


def test_contact_details_never_reach_the_body():
    """Here they arrive as their own section, which is dropped wholesale."""
    md, warns = conform_resume_md(MESSY)
    assert "you@example.com" not in md
    assert "555 010 0199" not in md
    assert "jordanlee.example" not in md
    assert any("contact" in w.lower() for w in warns)


def test_contact_details_stripped_from_inside_a_kept_section():
    """The harder case: contact smuggled into a section the template keeps."""
    md, warns = conform_resume_md(
        "## Summary\n\nProduct designer.\nReach me at you@example.com\n"
        "or 555 010 0199.\n\n## Experience\n\n### A · B · 06/2018 - 06/2026\n"
        "\n## Education\n\n- School · Degree · 2018\n\n## Skills\n\nFigma\n"
    )
    assert "you@example.com" not in md
    assert "555 010 0199" not in md
    assert "Product designer." in md
    assert sum("removed a contact line" in w for w in warns) == 2


def test_role_heading_separator_normalised_for_the_renderer():
    md, warns = conform_resume_md(MESSY)
    heading = [l for l in md.split("\n") if l.startswith("### ")][0]
    assert "•" not in heading
    assert heading.count("·") == 2
    assert any("separator" in w for w in warns)


def test_conformed_heading_parses_into_title_org_dates():
    import pdf
    md, _ = conform_resume_md(MESSY)
    heading = [l for l in md.split("\n") if l.startswith("### ")][0]
    title, org, dates = pdf._split_heading(heading[4:])
    assert title == "Senior Product Designer"
    assert org == "Northwind, Inc."
    assert dates == "06/2018 - 06/2026"


def test_missing_required_section_is_reported():
    _, warns = conform_resume_md("## Summary\n\nJust a summary.\n")
    assert any("experience" in w for w in warns)
    assert any("education" in w for w in warns)
    assert any("skills" in w for w in warns)


def test_letter_body_carries_no_letterhead_or_signature():
    body, warns = conform_letter_md(
        "# Jordan Lee\n\nyou@example.com\n\n"
        "Dear Hooli team,\n\nOne paragraph.\n\nBest regards,\nJordan Lee\n"
    )
    assert not body.startswith("#")
    assert "you@example.com" not in body
    assert body.rstrip().endswith("Best regards,")
    assert "Jordan Lee" not in body


def test_letter_without_greeting_gets_one():
    body, warns = conform_letter_md("Straight into the pitch.\n")
    assert body.startswith("Dear hiring team,")
    assert any("greeting" in w for w in warns)


def test_letter_already_correct_is_left_alone():
    good = "Dear Hooli team,\n\nOne paragraph.\n\nBest regards,\n"
    body, warns = conform_letter_md(good)
    assert body.strip() == good.strip()
    assert warns == []
