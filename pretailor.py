"""Morning pre-tailoring: a resume and letter made ahead
for the top shortlist jobs, checked, waiting for the user to look at.

Only jobs whose real job description is stored (the shortlist's
can_tailor). Nothing is applied, sent or logged in the tracker, and the job
is not marked tailored, so the fit scorer does not read it as interest.

Three checks on every run, and the run is ready only if all pass:
- numbers: every figure in the resume and letter appears in master.md,
  leaving out the parts of master.md that call a figure wrong or retired
- voice: no dash, AI tell or elevated word by voicecheck (long sentences
  are allowed in a resume)
- one page: the resume PDF is a single page
plus the claims check: nothing the user ruled out in profile.json's
never_claim (claims.py).

Each run also saves a word diff against the user's own resume (content/
resume.html, when it exists) so the user sees what changed.
Tailoring itself works on that same page (resume_template.py), so the
one-page check is on the user's real layout. Figures already on the user's resume count
as the user's.
"""

import difflib
import html
import json
import os
import re
import threading
import time

import claims
import resume_template
import voicecheck
from pdf import _pdf_pages

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "pretailor.json")
RESUME_HTML = os.path.join(BASE_DIR, "content", "resume.html")
MASTER_PATH = os.path.join(BASE_DIR, "content", "master.md")
TOP = 3

_lock = threading.Lock()
RUN = {"status": "idle"}

# A figure: 16%, 0.2%, 3x, 12x, 1,000+, 2,500, 958, 4.5. Dates (06/18) and
# years are not figures.
_FIGURE = re.compile(r"(?<![\w/.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s?%|x\b|\+)?(?![\w/])")
_NOT_ALLOWED = re.compile(r"\b(wrong|retired|superseded|do not use|not for use)\b", re.I)


def _chunks(text):
    """master.md split into bullets and paragraphs."""
    return [c for c in re.split(r"\n\s*\n|\n(?=\s*[-*] )|\n(?=\d+\. )", text or "") if c.strip()]


def _figures(text):
    out = set()
    for m in _FIGURE.finditer(text or ""):
        n, unit = m.group(1).replace(",", ""), (m.group(2) or "").strip()
        if not unit and re.fullmatch(r"(19|20)\d\d", n):
            continue
        out.add(n + unit)
    return out


def allowed_figures(master):
    return _figures("\n".join(c for c in _chunks(master) if not _NOT_ALLOWED.search(c)))


def bad_numbers(text, master, resume=""):
    """Figures in text that master.md does not give as usable, and that
    are not already on the user's own resume."""
    ok = allowed_figures(master) | _figures(resume)
    bare = {re.sub(r"[%x+]$", "", f) for f in ok}
    bad = []
    for f in sorted(_figures(text)):
        if f in ok:
            continue
        # A plain count ("5 designers") may match a figure written with a
        # unit elsewhere; a percent or multiplier must match exactly.
        if not re.search(r"[%x+]$", f) and f in bare:
            continue
        bad.append(f)
    return bad


def voice_problems(text):
    return [{"kind": h["kind"], "quote": h["quote"], "message": h["message"]}
            for h in voicecheck.voice(text) if h["kind"] != "long"]


def claim_problems(text):
    """Claims the user ruled out in profile.json's never_claim (claims.py)."""
    return claims.problems(text)


def _plain_html(page):
    page = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<(br|/p|/li|/h\d|/div)[^>]*>", "\n", page)
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def _plain_md(md):
    md = re.sub(r"(?m)^#+\s*", "", md)
    return re.sub(r"[*_`]+", "", md)


def word_diff(old, new):
    """[-removed-] and {+added+} between two texts, word by word."""
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(" ".join(a[i1:i2]))
            continue
        if i2 > i1:
            out.append("[-" + " ".join(a[i1:i2]) + "-]")
        if j2 > j1:
            out.append("{+" + " ".join(b[j1:j2]) + "+}")
    return " ".join(out)


def check(resume_md, letter_body, pdf_path, master, resume=""):
    both = resume_md + "\n\n" + letter_body
    try:
        pages = _pdf_pages(pdf_path)
    except OSError:
        pages = 0
    problems = {
        "numbers": bad_numbers(both, master, resume),
        "voice": voice_problems(both),
        "claims": claim_problems(both),
        "pages": pages,
    }
    ready = (not problems["numbers"] and not problems["voice"]
             and not problems["claims"] and pages == 1)
    return ready, problems


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(STATE_PATH + ".tmp", STATE_PATH)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def run(shortlist, posting, tailor, save, output_dir, top=TOP, log=print):
    """Tailor the first top can_tailor jobs on the shortlist that have no
    run yet. shortlist() -> {jobs}; posting(job_id) -> (url, company,
    title, text); tailor(text, company, role) -> result; save(text, url,
    company, role, result) -> payload with id. Returns the job ids done."""
    state = load_state()
    master = _read(MASTER_PATH)
    page = _read(RESUME_HTML)
    try:
        base = _plain_md(resume_template.to_markdown(page)) if page else ""
    except Exception:
        base = _plain_html(page)
    picked = [j for j in shortlist()["jobs"] if j["can_tailor"] and j["id"] not in state][:top]
    done = []
    for j in picked:
        found = posting(j["id"])
        if not found:
            continue
        url, company, title, text = found
        try:
            result = tailor(text, company, title)
            payload = save(text, url, company, title, result)
        except Exception as e:
            log("pretailor %s: %s" % (j["id"], e))
            state[j["id"]] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": str(e)[:300]}
            _save_state(state)
            continue
        out_dir = os.path.join(output_dir, payload["id"])
        letter = payload["letter"]["versions"][0]["text"]
        ready, problems = check(result["resume_md"], letter,
                                os.path.join(out_dir, "resume.pdf"), master, base)
        diff = word_diff(base, _plain_md(result["resume_md"])) if base else ""
        with open(os.path.join(out_dir, "resume-diff.txt"), "w", encoding="utf-8") as f:
            f.write(diff + "\n")
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "out_id": payload["id"],
                 "company": company, "role": title, "ready": ready, "problems": problems,
                 "files": payload["files"], "diff": "/output/%s/resume-diff.txt" % payload["id"]}
        with open(os.path.join(out_dir, "pretailor.json"), "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=1)
        state[j["id"]] = entry
        _save_state(state)
        done.append(j["id"])
        log("pretailor %s: %s" % (j["id"], "ready" if ready else "needs a look"))
    return done


# ── Where the made files show up: a "Tailored resume"
#    button on the job board and the Applied list, and the extension
#    attaching the tailored PDF on that job's own application page.
#    Only runs that passed every check count.

def files(entry, output_dir):
    """{resume, letter} URLs for a ready run whose PDF is on disk, else
    None. The letter only once the user approved it (the PDF exists then)."""
    if not entry or not entry.get("ready") or not entry.get("out_id"):
        return None
    d = os.path.join(output_dir, entry["out_id"])
    if not os.path.exists(os.path.join(d, "resume.pdf")):
        return None
    base = "/output/%s/" % entry["out_id"]
    return {"resume": base + "resume.pdf",
            "letter": base + "cover-letter.pdf" if os.path.exists(os.path.join(d, "cover-letter.pdf")) else "",
            "company": entry.get("company") or "", "role": entry.get("role") or ""}


def tailored_jobs(lookup, output_dir):
    """Every run that passed its checks and still has its PDF, with its
    watched posting: [{id, url, company, role, files}]. lookup(job_id) ->
    (url, company, title) or None."""
    out = []
    for jid, entry in load_state().items():
        got = files(entry, output_dir)
        row = got and lookup(jid)
        if row:
            out.append({"id": jid, "url": row[0], "company": row[1], "role": row[2], "files": got})
    return out


def company_key(name):
    name = re.sub(r",?\s+(inc|llc|ltd|corp|corporation|co)\.?$", "", (name or "").strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def norm_url(url):
    """Host, path and the query that names the posting. The tail an apply
    page adds (/apply, /application) and tracking parameters are dropped;
    the query stays, since gh_jid=... is often the only thing that says
    which job a careers page is showing."""
    from urllib.parse import parse_qsl, urlsplit
    try:
        u = urlsplit((url or "").strip())
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/(apply|application)/?$", "", u.path.rstrip("/"), flags=re.I).rstrip("/")
    q = sorted({(k, v) for k, v in parse_qsl(u.query)
                if not k.lower().startswith("utm_") and k.lower() not in ("gh_src", "source", "src", "ref")})
    return host + path + ("?" + "&".join("%s=%s" % kv for kv in q) if q else "")


# Postings whose id is a real number or uuid from the company's own board.
_ID_ATS = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable", "rippling", "gem")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def posting_ids(job_id):
    """The ids that name this posting inside an apply page's URL."""
    ats, _, pid = (job_id or "").partition(":")
    if ats not in _ID_ATS:
        return set()
    pid = pid.rsplit(":", 1)[-1]
    out = set(m.group(0).lower() for m in _UUID.finditer(pid))
    out |= {m.lower() for m in re.findall(r"(?<![A-Za-z0-9])([A-Za-z]{0,4}\d{5,})(?![A-Za-z0-9])", pid)}
    return {i for i in out if len(i) >= 6}


def match_page(page_url, title, jobs):
    """The one job whose tailored resume belongs on this page, or None.

    jobs: [{id, url, company, ...}]. A job matches when the page is its
    posting (same normalized URL), or when the page URL carries the
    posting's own id and the company's name is in the URL or the page
    title. Two different jobs matching means none: a wrong company's
    resume must never go out, so anything unsure gets the standard one."""
    here = norm_url(page_url)
    if not here:
        return None
    low = (page_url or "").lower()
    where = re.sub(r"[^a-z0-9]", "", low) + " " + re.sub(r"[^a-z0-9]", "", (title or "").lower())
    hits = []
    for j in jobs:
        if j.get("url") and norm_url(j["url"]) == here:
            hits.append(j)
            continue
        key = company_key(j.get("company"))
        if len(key) < 3 or key not in where:
            continue
        if any(re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(i), low) for i in posting_ids(j.get("id"))):
            hits.append(j)
    ids = {j["id"] for j in hits}
    if len(ids) == 1:
        return hits[0]
    # The page itself is one job's posting: that one, over others matched
    # only by id (a resume just made from this page beside a pre-tailored
    # one for the same posting).
    exact = [j for j in hits if j.get("url") and norm_url(j["url"]) == here]
    return exact[0] if len({j["id"] for j in exact}) == 1 else None


def made_folders(output_dir, skip=()):
    """Folders tailored from the Tailor page or the panel that have a
    resume, newest first, as [{id, url, company, role, files}]. id is
    "made:<folder>"; url is the posting it was made from."""
    out = []
    try:
        names = sorted(os.listdir(output_dir), reverse=True)
    except OSError:
        return out
    for name in names:
        d = os.path.join(output_dir, name)
        resume = "/output/%s/resume.pdf" % name
        if resume in skip or not re.fullmatch(r"[a-z0-9-]+", name) \
                or not os.path.exists(os.path.join(d, "resume.pdf")):
            continue
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not meta.get("source_url"):
            continue
        letter = "/output/%s/cover-letter.pdf" % name if os.path.exists(os.path.join(d, "cover-letter.pdf")) else ""
        company, role = meta.get("company") or "", meta.get("role") or ""
        out.append({"id": "made:" + name, "url": meta["source_url"], "company": company, "role": role,
                    "files": {"resume": resume, "letter": letter, "company": company, "role": role}})
    return out
