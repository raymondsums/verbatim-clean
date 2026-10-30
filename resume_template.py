"""The user's own resume as the tailoring template. Optional.

When content/resume.html exists, tailoring edits it in place instead of
writing a new resume. Without it, tailoring writes Markdown and pdf.py
lays it out.

A resume written from scratch tends to run onto a second page, so this
starts from the user's resume (content/resume.html): same file, same CSS.
The page needs <p class="summary">, <ul> lists of <li><b>Label</b> text
bullets, and <section class="skills"><h2>Skills</h2><p>...</p>.

The model may only reword the summary and the user's existing bullets, each
within its current length plus about 10%, reorder bullets inside a role,
and reorder the skills. The bold bullet labels, roles, dates, sections,
education, tools and contact block never change.

slots() lists what may change. fill() puts the model's edits back into the
page, refusing any edit that breaks a rule (the original text stays and a
note says why). fit() prints the page and, if it runs past one page, puts
back the original text of the slots that grew the most, one at a time,
until it fits.
"""

import html
import math
import os
import re

import claims

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "content", "resume.html")
GROWTH = 1.10

_SUMMARY = re.compile(r'(<p class="summary">)(.*?)(</p>)', re.S)
_UL = re.compile(r"(<ul>)(.*?)(</ul>)", re.S)
_LI = re.compile(r"<li><b>(.*?)</b>\s*(.*?)</li>", re.S)
_SKILLS = re.compile(r'(<section class="skills">\s*<h2>Skills</h2>\s*<p>)(.*?)(</p>)', re.S)


class TemplateError(Exception):
    pass


def load():
    try:
        with open(TEMPLATE_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise TemplateError("could not read the user's resume template: %s" % e)


def _text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _words(text):
    return len((text or "").split())


def limit(text):
    """Most words an edit of this text may have."""
    return math.ceil(_words(text) * GROWTH)


def slots(page):
    """{summary, roles: [[{id, label, text, max_words}]], skills: [..]}."""
    m = _SUMMARY.search(page)
    if not m:
        raise TemplateError("the resume has no summary paragraph")
    out = {"summary": {"id": "summary", "text": _text(m.group(2)),
                       "max_words": limit(_text(m.group(2)))},
           "roles": [], "skills": []}
    n = 0
    for ul in _UL.finditer(page):
        role = []
        for li in _LI.finditer(ul.group(2)):
            n += 1
            text = _text(li.group(2))
            role.append({"id": "b%d" % n, "label": _text(li.group(1)),
                         "text": text, "max_words": limit(text)})
        out["roles"].append(role)
    s = _SKILLS.search(page)
    if s:
        out["skills"] = [_text(x) for x in s.group(2).split(",")
                         if _text(x) and not _forbidden_skill(_text(x))]
    return out


# Skills a tailored resume never lists, even though the user's own resume
# does: profile.json's leave_out_skills (claims.py). Dropped here so the
# model never sees them and fill() never prints them; the claims check in
# pretailor.py stays as the backstop.
def _forbidden_skill(skill):
    s = " ".join((skill or "").split()).lower()
    return bool(s) and s in claims.leave_out_skills()


def _clean(text):
    text = (text or "").replace(chr(0x2014), ",").replace(chr(0x2013), "-")
    return " ".join(text.split())


# The model sometimes writes its own change note into a slot ("Tailored the
# resume for Acme's ...") and it got printed on the resume. The user's own
# wording never talks about the resume or
# the posting, so a slot that does is a note, not an edit, and is refused.
_CHANGE_NOTE = re.compile(
    r"^\s*(i\s+)?(tailored|rewrote|reworded|reordered|re-?framed|emphasi[sz]ed|"
    r"updated|adjusted|changed|kept|added|removed|swapped|highlighted|edited)\b"
    r"|\b(the|this|his|your)\s+(resume|cv|summary)\b"
    r"|\bjob\s+description\b|\bthe\s+(posting|jd|role'?s\s+requirements)\b", re.I)


def is_change_note(text):
    return bool(_CHANGE_NOTE.search(text or ""))


def fill(page, edits):
    """The page with the model's edits applied, and notes on any edit that
    was refused. edits: {summary, bullets: [{id, text}], order: [[ids]],
    skills: [..]}."""
    base = slots(page)
    notes = []
    by_id = {b["id"]: b for role in base["roles"] for b in role}
    new_text = {}

    def take(slot, text):
        text = _clean(text)
        if not text:
            return slot["text"]
        if is_change_note(text):
            notes.append("Kept the original %s: the edit read like a note about the changes, not resume text."
                         % (slot.get("label") or "summary"))
            return slot["text"]
        if _words(text) > slot["max_words"]:
            notes.append("Kept the original %s: the edit ran %d words, the limit is %d."
                         % (slot.get("label") or "summary", _words(text), slot["max_words"]))
            return slot["text"]
        return text

    summary = take(base["summary"], edits.get("summary"))
    for e in edits.get("bullets") or []:
        if e.get("id") in by_id:
            new_text[e["id"]] = take(by_id[e["id"]], e.get("text"))

    orders = edits.get("order") or []
    roles_out = []
    for i, role in enumerate(base["roles"]):
        ids = [b["id"] for b in role]
        want = orders[i] if i < len(orders) else ids
        if sorted(want or []) != sorted(ids):
            if want != ids:
                notes.append("Kept the original bullet order in role %d: the new order "
                             "added or dropped a bullet." % (i + 1))
            want = ids
        roles_out.append(want)

    skills = base["skills"]
    got = [_clean(x) for x in edits.get("skills") or [] if not _forbidden_skill(_clean(x))]
    if got and sorted(x.lower() for x in got) == sorted(x.lower() for x in skills):
        skills = got
    elif got:
        notes.append("Kept the original skills order: the new list added or dropped a skill.")

    page = _SUMMARY.sub(lambda m: m.group(1) + html.escape(summary, quote=False) + m.group(3),
                        page, count=1)
    k = iter(range(len(roles_out)))

    def ul_sub(m):
        i = next(k)
        lis = []
        for bid in roles_out[i]:
            b = by_id[bid]
            lis.append("        <li><b>%s</b> %s</li>" % (
                html.escape(b["label"], quote=False),
                html.escape(new_text.get(bid, b["text"]), quote=False)))
        return m.group(1) + "\n" + "\n".join(lis) + "\n      " + m.group(3)

    page = _UL.sub(ul_sub, page)
    if base["skills"]:
        page = _SKILLS.sub(lambda m: m.group(1) + ", ".join(
            html.escape(x, quote=False).replace(" ", "&nbsp;") for x in skills) + m.group(3),
            page, count=1)
    return page, notes


def to_markdown(page):
    """The filled page as plain markdown, for text boxes and the checks.
    The name and contact lines are left out; they come from profile.json."""
    out = []
    for sec in re.finditer(r"<section[^>]*>(.*?)</section>", page, re.S):
        body = sec.group(1)
        h2 = re.search(r"<h2>(.*?)</h2>", body, re.S)
        if h2:
            out.append("\n## " + _text(h2.group(1)))
        for part in re.finditer(r'<h3 class="title">(.*?)</h3>\s*<span class="dates">(.*?)</span>'
                                r'|<li><b>(.*?)</b>(.*?)</li>|<p[^>]*>(.*?)</p>'
                                r'|<div class="edu-line">(.*?)</div>', body, re.S):
            if part.group(1):
                out.append("\n### %s · %s" % (_text(part.group(1)), _text(part.group(2))))
            elif part.group(3):
                out.append("- **%s** %s" % (_text(part.group(3)), _text(part.group(4))))
            elif part.group(5):
                out.append(_text(part.group(5)))
            elif part.group(6):
                out.append(_text(part.group(6)))
    return "\n".join(out).strip() + "\n"


def fit(page, edits, pdf_path, printer, pages):
    """Print the filled page; while it runs past one page, put back the
    original text of the slot that grew the most. Returns (page, notes,
    page count)."""
    base = slots(page)
    edits = dict(edits)
    bullets = {e.get("id"): e for e in edits.get("bullets") or []}
    notes = []
    while True:
        filled, fill_notes = fill(page, dict(edits, bullets=list(bullets.values())))
        printer(filled, pdf_path)
        n = pages(pdf_path)
        if n <= 1:
            return filled, fill_notes + notes, n
        grown = []
        for role in base["roles"]:
            for b in role:
                e = bullets.get(b["id"])
                if e and _clean(e.get("text")) and _clean(e.get("text")) != b["text"]:
                    grown.append((_words(e["text"]) - _words(b["text"]), b["id"], b["label"]))
        if edits.get("summary") and _clean(edits["summary"]) != base["summary"]["text"]:
            grown.append((_words(edits["summary"]) - _words(base["summary"]["text"]),
                          "summary", "summary"))
        if not grown:
            return filled, fill_notes + notes + ["Still two pages with the user's own wording."], n
        grown.sort(reverse=True)
        _, gid, label = grown[0]
        if gid == "summary":
            edits["summary"] = ""
        else:
            bullets.pop(gid)
        notes.append("Put back the original %s to keep one page." % label)
