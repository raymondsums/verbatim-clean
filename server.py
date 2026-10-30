"""Verbatim local server.

Paste a job description, get a tailored resume and cover letter. Facts
come from profile.json and are prepended verbatim; the model never sees
them. The answer repo, the job board and the extension all talk to this
same server.

Run:  python3 server.py     then open http://localhost:5053
(PORT in .env changes the port.)
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

from dotenv import load_dotenv
from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

import answers as answers_mod
import applied as applied_mod
import autofill as autofill_mod
import cue as cue_mod
import tracker as tracker_mod
import letters as letters_mod
import dictate
import filllog
import jd_fetch
import llm
import postcheck
import predraft
import pretailor
import resume_template
import prep as prep_mod
import tailor as tailor_mod
import voicecheck
import interviewnotes
import vaultio
import voicesamples
import weekly
from jobs import watch as watch_mod
from pdf import (render_pdf, render_resume_pdf, short_link, to_plain_text,
                 PdfError, _chrome_print, _pdf_pages)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
PROFILE_PATH = os.environ.get("VERBATIM_PROFILE") or os.path.join(BASE_DIR, "profile.json")
load_dotenv(os.path.join(BASE_DIR, ".env"))
PORT = int(os.environ.get("PORT") or 5053)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
_LOCK = threading.RLock()

# Cue, the practice cards. gunicorn imports this file, so the database is
# made ready here rather than under __main__.
app.register_blueprint(cue_mod.bp)
app.register_blueprint(tracker_mod.bp)
cue_mod.init_db()


def load_profile():
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return json.load(f)


def contact_header(profile):
    """The contact block, assembled verbatim from the profile store."""
    line = " · ".join(x for x in (
        profile.get("location"),
        profile.get("phone"),
        profile.get("email"),
        short_link(profile.get("portfolio")),
        short_link(profile.get("linkedin")),
    ) if x)
    title = profile.get("title", "")
    head = f"# {profile.get('name', '')}\n\n"
    if title:
        head += f"{title}\n\n"
    return head + f"{line}\n"


def contact_line(profile):
    """Just the contact details, for the cover letter footer."""
    return " · ".join(x for x in (
        profile.get("location"),
        profile.get("phone"),
        profile.get("email"),
        short_link(profile.get("portfolio")),
        short_link(profile.get("linkedin")),
    ) if x)


def letter_header(profile):
    """Cover letter masthead: name and title only. The contact details run
    as a footer instead, so the letter opens on the greeting."""
    head = f"# {profile.get('name', '')}\n\n"
    if profile.get("title"):
        head += f"{profile['title']}\n"
    return head


def _resolve_jd(raw):
    """The JD text, fetching it when the box held a URL.

    Returns (text, source_url). Raises jd_fetch.JdFetchError with a
    paste-it-instead message when the link cannot be read.
    """
    raw = (raw or "").strip()
    if not jd_fetch.looks_like_url(raw):
        return raw, None
    try:
        return jd_fetch.fetch(raw)["text"], raw
    except jd_fetch.JdFetchError as e:
        raise jd_fetch.JdFetchError(
            f"could not read the posting from that link ({e}). "
            "Paste the job description text instead.")


def _slug(company, role):
    base = "-".join(x for x in (company, role) if x) or "application"
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()[:60]
    return f"{time.strftime('%Y-%m-%d')}-{base}"


@app.get("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


# The app's icons: the quote marks on an ink tile. favicon.svg flips to a light tile on a dark tab bar.
ICONS_DIR = os.path.join(BASE_DIR, "icons")


@app.get("/icons/<name>")
def icon_file(name):
    mime = "application/manifest+json" if name.endswith(".webmanifest") else None
    return send_from_directory(ICONS_DIR, name, mimetype=mime, max_age=86400)


@app.get("/favicon.ico")
def favicon_ico():
    return send_from_directory(ICONS_DIR, "favicon-32.png", mimetype="image/png", max_age=86400)


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return send_from_directory(ICONS_DIR, "apple-touch-icon.png", max_age=86400)


@app.post("/api/jd/resolve")
def api_jd_resolve():
    """A posting's text plus the company and role it names.

    Takes either a URL to fetch, or already-captured text (from the
    extension) that just needs company and role extracted.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    text = (data.get("text") or "").strip()
    title = (data.get("title") or "").strip()
    if url and jd_fetch.looks_like_url(url) and not text:
        try:
            page = jd_fetch.fetch(url)
        except jd_fetch.JdFetchError as e:
            return jsonify({"error": (
                f"could not read the posting from that link ({e}). "
                "Paste the job description text instead.")}), 502
        text, title = page["text"], page["title"]
    elif not text:
        return jsonify({"error": "that does not look like a link"}), 400
    meta = tailor_mod.extract_meta(text, title=title, url=url)
    return jsonify({"text": text, "title": title,
                    "company": meta["company"], "role": meta["role"],
                    "salary": meta.get("salary", ""),
                    "location": meta.get("location", ""),
                    "source_url": url or None})


@app.post("/api/tailor")
def api_tailor():
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    role = (data.get("role") or "").strip()

    job = PAGE_JOB
    job.clear()
    job.update({"status": "running", "started": time.time(), "mode": "both",
                "for": " at ".join(x for x in (role, company) if x)})
    _stage(job, "reading")
    try:
        jd, source_url = _resolve_jd(data.get("job_description"))
    except jd_fetch.JdFetchError as e:
        job.update({"status": "error", "error": str(e)})
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        job.update({"status": "error", "error": str(e)})
        return jsonify({"error": str(e)}), 500

    try:
        _stage(job, "writing")
        result = tailor_mod.tailor(
            jd, company=company, role=role,
            answers=answers_mod.cover_letter_context())
        _stage(job, "checking")
        result["notes"] = result.get("notes", []) + _quick_checks(result)
        _stage(job, "fitting")
        out = _save_tailoring(jd, source_url, company, role, result)
    except tailor_mod.TailorError as e:
        job.update({"status": "error", "error": str(e)})
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        # Never left "running", and "done" only once it is saved.
        job.update({"status": "error", "error": str(e)})
        return jsonify({"error": str(e)}), 500
    job["status"] = "done"
    return jsonify(out)


def _half_folder(source_url, mode):
    """The newest folder for this posting still missing the half being made
    now, so a resume and a letter made one at a time land together."""
    if not source_url or mode not in ("resume", "letter"):
        return None
    missing = "resume.md" if mode == "resume" else "letter.json"
    try:
        names = sorted(os.listdir(OUTPUT_DIR), reverse=True)
    except OSError:
        return None
    for name in names:
        d = os.path.join(OUTPUT_DIR, name)
        if not re.fullmatch(r"[a-z0-9-]+", name) or os.path.exists(os.path.join(d, missing)):
            continue
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                if json.load(f).get("source_url") == source_url:
                    return d
        except (OSError, ValueError):
            continue
    return None


def _save_tailoring(jd, source_url, company, role, result, extra=None, mode="both"):
    """Assemble, save, and render one tailoring run. Shared by the page
    route and the extension's background job. mode "resume" or "letter"
    saves only that half (tailor.MODES)."""
    profile = load_profile()
    header = contact_header(profile)

    # On the user's own resume (resume_template.py): fill it, print it, and put
    # back original lines until it is one page. Rendered first so the
    # saved text matches the PDF.
    page_html, page_pdf, pages_error = None, None, None
    if mode != "letter" and result.get("resume_edits") and result.get("resume_page"):
        page_pdf = tempfile.mktemp(suffix=".pdf")
        try:
            page_html, fit_notes, _n = resume_template.fit(
                result["resume_page"], result["resume_edits"], page_pdf,
                _chrome_print, _pdf_pages)
            result["resume_md"] = resume_template.to_markdown(page_html)
            result["notes"] = [n for n in result["notes"] if n not in fit_notes] + fit_notes
        except (PdfError, resume_template.TemplateError) as e:
            pages_error = str(e)

    letter_body = result["cover_letter_md"].strip()
    if not letter_body.rstrip().endswith("Best regards,"):
        letter_body += "\n\nBest regards,"
    letter_body += "\n" + profile.get("name", "")

    resume_md = header + "\n" + result["resume_md"].strip() + "\n"
    letter_md = (letter_header(profile) + "\n" + letter_body + "\n\n"
                 + contact_line(profile) + "\n")

    with _LOCK:
        out_dir = _half_folder(source_url, mode)
        old_meta = {}
        if out_dir:
            try:
                with open(os.path.join(out_dir, "meta.json"), encoding="utf-8") as f:
                    old_meta = json.load(f)
            except (OSError, ValueError):
                pass
        else:
            out_id = _slug(company, role)
            out_dir = os.path.join(OUTPUT_DIR, out_id)
            n = 2
            while os.path.exists(out_dir):
                out_dir = os.path.join(OUTPUT_DIR, f"{out_id}-{n}")
                n += 1
            os.makedirs(out_dir)
        out_id = os.path.basename(out_dir)

        def write(name, text):
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                f.write(text)

        write("jd.txt", (f"Source: {source_url}\n\n" if source_url else "") + jd)
        if mode != "letter":
            write("resume.md", resume_md)
            # plain text for application textareas, which are not markdown aware
            write("resume.txt", to_plain_text(resume_md))
        if mode != "resume":
            # The letter waits for the user's review: version 1 only, no files and
            # no PDF until the user approves it (letters.py).
            letters_mod.start(out_dir, letter_body)
        write("meta.json", json.dumps({**old_meta, **{
            "company": company, "role": role,
            # What the posting said it pays and where, copied not inferred.
            # Kept here so pressing Log it can put them in the tracker
            # without re-reading a page that is long gone by then.
            "salary": (extra or {}).get("salary", ""),
            "location": (extra or {}).get("location", ""),
            "source_url": source_url,
            "created": old_meta.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "notes": (old_meta.get("notes") or []) + result["notes"],
        }}, indent=2))

    pdf_error = pages_error
    if mode == "letter":
        pass
    elif page_html and page_pdf and os.path.exists(page_pdf):
        shutil.move(page_pdf, os.path.join(out_dir, "resume.pdf"))
        with open(os.path.join(out_dir, "resume.html"), "w", encoding="utf-8") as f:
            f.write(page_html)
    elif not pages_error:
        try:
            render_resume_pdf(result["resume_md"], profile,
                              os.path.join(out_dir, "resume.pdf"))
        except PdfError as e:
            pdf_error = str(e)

    return {
        "id": out_id,
        "resume_md": resume_md,
        "cover_letter_md": letter_md,
        "resume_txt": to_plain_text(resume_md),
        "cover_letter_txt": to_plain_text(letter_md),
        "notes": result["notes"],
        "pdf_error": pdf_error,
        "letter": {"versions": [{"n": 1, "text": letter_body, "source": "draft"}], "approved": None},
        "files": {
            "resume_pdf": f"/output/{out_id}/resume.pdf",
            "cover_letter_pdf": f"/output/{out_id}/cover-letter.pdf",
            "resume_md": f"/output/{out_id}/resume.md",
            "cover_letter_md": f"/output/{out_id}/cover-letter.md",
            "resume_txt": f"/output/{out_id}/resume.txt",
            "cover_letter_txt": f"/output/{out_id}/cover-letter.txt",
        },
    }


def _render_letter(body, out_dir):
    """The approved letter body with the user's letterhead and contact footer, as
    the .md, .txt and PDF the page and the extension hand out."""
    profile = load_profile()
    letter_md = (letter_header(profile) + "\n" + body.strip() + "\n\n"
                 + contact_line(profile) + "\n")
    with open(os.path.join(out_dir, "cover-letter.md"), "w", encoding="utf-8") as f:
        f.write(letter_md)
    with open(os.path.join(out_dir, "cover-letter.txt"), "w", encoding="utf-8") as f:
        f.write(to_plain_text(letter_md))
    try:
        render_pdf(letter_md, os.path.join(out_dir, "cover-letter.pdf"), title="Cover letter")
    except PdfError as e:
        raise letters_mod.LetterError("Unable to make the PDF. Try again.", 502) from e


def _letter_reply(fn, *args):
    try:
        return jsonify(fn(*args))
    except letters_mod.LetterError as e:
        return jsonify({"error": str(e)}), e.status


@app.get("/api/letter/<out_id>")
def api_letter(out_id):
    return _letter_reply(letters_mod.get, OUTPUT_DIR, out_id)


@app.post("/api/letter/<out_id>/save")
def api_letter_save(out_id):
    data = request.get_json(silent=True) or {}
    return _letter_reply(letters_mod.save, OUTPUT_DIR, out_id, data.get("text"))


@app.post("/api/letter/<out_id>/revise")
def api_letter_revise(out_id):
    data = request.get_json(silent=True) or {}
    return _letter_reply(letters_mod.revise, OUTPUT_DIR, out_id, data.get("text"), data.get("note"))


@app.post("/api/letter/<out_id>/approve")
def api_letter_approve(out_id):
    data = request.get_json(silent=True) or {}
    r = _letter_reply(letters_mod.approve, OUTPUT_DIR, out_id, data.get("text"), _render_letter)
    return r


@app.get("/api/letter/<out_id>/doc")
def api_letter_doc(out_id):
    """This run's approved cover letter PDF, base64, for the extension to
    attach. Only once approved: before that there is no PDF."""
    import base64
    try:
        path = letters_mod._dir(OUTPUT_DIR, out_id)
    except letters_mod.LetterError as e:
        return jsonify({"found": False, "reason": str(e)}), e.status
    pdf = os.path.join(path, "cover-letter.pdf")
    if not os.path.exists(pdf):
        return jsonify({"found": False, "reason": "approve the letter first"})
    with open(pdf, "rb") as f:
        blob = base64.b64encode(f.read()).decode()
    name = load_profile().get("name", "")
    return jsonify({"found": True, "doc": {"filename": f"{name} Cover Letter.pdf".strip(), "b64": blob}})


@app.post("/api/letter/<out_id>/learn")
def api_letter_learn(out_id):
    return jsonify({"lessons": letters_mod.learn(OUTPUT_DIR, out_id)})


@app.get("/output/<out_id>/<name>")
def get_output(out_id, name):
    if not re.fullmatch(r"[a-z0-9-]+", out_id) or name not in (
            "resume.pdf", "cover-letter.pdf", "resume.md",
            "cover-letter.md", "jd.txt", "meta.json"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(os.path.join(OUTPUT_DIR, out_id), name)


@app.get("/api/answers")
def api_answers():
    try:
        return jsonify({"answers": answers_mod.parse_repo(answers_mod.read_repo())})
    except (answers_mod.AnswersError, Exception) as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/answers/match")
def api_answers_match():
    data = request.get_json(silent=True) or {}
    try:
        m = answers_mod.match(data.get("question"))
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502

    category, entry = m["category"], m["entry"]
    if category == "authorization":
        profile = load_profile()
        return jsonify({
            "category": category, "match": None,
            "verbatim": {
                "authorized_to_work_in_us": profile.get("authorized_to_work_in_us"),
                "requires_sponsorship": profile.get("requires_sponsorship"),
                "statement": profile.get("work_authorization"),
            },
            "flag": ("Fill from the stored wording below, exactly as "
                     "written. If the question does not clearly fit it, "
                     "leave the field empty and answer it yourself."),
        })
    if category == "eeo":
        eeo = load_profile().get("eeo") or {}
        if eeo:
            return jsonify({
                "category": category, "match": None, "verbatim": eeo,
                "flag": ("Your stored choices, filled exactly as written. "
                         "Skip any that do not fit the question."),
            })
        return jsonify({
            "category": category, "match": None,
            "flag": ("No stored choices yet. Your call each time until "
                     "you store them in profile.json."),
        })
    if category == "salary":
        return jsonify({
            "category": category, "match": None,
            "flag": ("Your call each time. The toolkit never stores or "
                     "suggests an answer for this one."),
        })
    return jsonify({"category": "normal", "match": entry})


@app.post("/api/answers/suggest")
def api_answers_suggest():
    """What the repo already has for the free-text questions on a form.

    Suggestions only. Nothing here fills anything by itself: the extension
    shows you your own stored words and you decide, per the project rule
    that an answer bank which rewrites itself is one you stop trusting.
    """
    data = request.get_json(silent=True) or {}
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        return jsonify({"error": "no questions"}), 400
    try:
        results = answers_mod.match_many([str(q) for q in questions[:12]])
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502

    profile = load_profile()
    out = []
    for r in results:
        item = {"question": r["question"], "category": r["category"],
                "answer": "", "used": [], "flag": ""}
        if r["category"] == "normal" and r["entry"]:
            e = r["entry"]
            if answers_mod.company_specific(r["question"], e.get("kind")):
                item["flag"] = ("Written for another company. Draft this "
                                "one for this posting.")
            elif answers_mod.counts_as_own(e.get("best_source")):
                item["answer"] = e["answer"]
            else:
                item["flag"] = ("The stored answer may not be yours. Draft "
                                "this one.")
            item["used"] = e["used"][-3:]
            item["group"] = e["question"]
        elif r["category"] == "authorization":
            item["answer"] = profile.get("work_authorization") or ""
            item["flag"] = ("Your stored wording, exactly as written. If it "
                            "does not fit this question, answer it yourself.")
        elif r["category"] == "salary":
            item["flag"] = "Your call each time. Nothing is stored for this."
        elif r["category"] == "eeo":
            item["flag"] = "Your stored choices are filled by Fill, not here."
        out.append(item)
    return jsonify({"suggestions": out})


@app.post("/api/answers/draft")
def api_answers_draft():
    data = request.get_json(silent=True) or {}
    try:
        jd, _ = _resolve_jd(data.get("job_description"))
    except jd_fetch.JdFetchError as e:
        return jsonify({"error": str(e)}), 502
    try:
        prior = None
        try:
            prior = [p for p in (answers_mod.for_prompts(g) for g in
                                 answers_mod.parse_repo(answers_mod.read_repo())
                                 if not answers_mod.specific_group(g)) if p]
        except Exception:
            prior = None  # the repo is a nicety here, not a dependency
        text = answers_mod.draft(
            data.get("question"), jd,
            tailor_mod.load_master(),
            company=(data.get("company") or "").strip(),
            role=(data.get("role") or "").strip(),
            base_answer=(data.get("base_answer") or "").strip() or None,
            prior_answers=prior,
            previous_draft=(data.get("previous_draft") or "").strip() or None,
            feedback=(data.get("feedback") or "").strip() or None,
            samples=_voice_samples(data.get("question"), jd),
        )
    except (answers_mod.AnswersError, tailor_mod.TailorError) as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"draft": text})


@app.post("/api/answers/record")
def api_answers_record():
    data = request.get_json(silent=True) or {}
    try:
        outcome = answers_mod.record(
            data.get("question"), data.get("answer"),
            company=(data.get("company") or "").strip(),
            role=(data.get("role") or "").strip(),
            canonical=data.get("canonical"),
        )
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "outcome": outcome})


CAPTURE_PATH = os.path.join(BASE_DIR, "last_capture.json")
CAPTURE_FRESH_SECONDS = 900

# One tailoring job at a time, kicked off by the extension's Capture
# button so the popup can show progress and results without the toolkit
# page. Single worker process (see the plist), so module state is safe.
TAILOR_JOB = {"status": "idle"}
# The panel's letter button runs on its own, beside a resume run;
# TAILOR_JOB is the resume, or both from the Tailor page.
LETTER_JOB = {"status": "idle"}


# The Tailor page's own run, so it can show its steps while it waits.
PAGE_JOB = {"status": "idle"}


def _job_for(mode):
    return LETTER_JOB if mode == "letter" else PAGE_JOB if mode == "page" else TAILOR_JOB


# The steps a run goes through, with typical seconds from timed runs.
# The page and
# panel draw their progress from the stage the server is on; the seconds
# only pace the bar inside a step.
STAGES = {
    "resume": [("reading", "Reading the posting", 3), ("writing", "Writing your resume", 126),
               ("checking", "Checking numbers and voice", 1), ("fitting", "Fitting to one page", 4)],
    "letter": [("reading", "Reading the posting", 3), ("writing", "Writing your letter", 90),
               ("checking", "Checking numbers and voice", 1)],
    "both": [("reading", "Reading the posting", 3), ("writing", "Writing your resume and letter", 150),
             ("checking", "Checking numbers and voice", 1), ("fitting", "Fitting to one page", 4)],
}


def _stage(job, key):
    job["stage"], job["stage_at"] = key, time.time()


def _quick_checks(result):
    """The pre-tailoring checks on a fresh draft, as notes for the user: figures
    master.md does not give, voice slips, and claims ruled out in
    profile.json. Nothing is changed."""
    text = (result.get("resume_md") or "") + "\n\n" + (result.get("cover_letter_md") or "")
    try:
        with open(pretailor.MASTER_PATH, encoding="utf-8") as f:
            master = f.read()
        base = resume_template.to_markdown(resume_template.load())
    except Exception:
        return []
    out = ["Check: %s is not a figure master.md gives." % n for n in pretailor.bad_numbers(text, master, base)]
    out += ["Check: %s" % (v["message"] if v.get("message") else v["quote"]) for v in pretailor.voice_problems(text)]
    out += ["Check: a claim ruled out in profile.json, \"%s\"" % c for c in pretailor.claim_problems(text)]
    return out


def _tailor_job(text, title, url, mode="both"):
    job = _job_for(mode)
    try:
        _stage(job, "reading")
        meta = tailor_mod.extract_meta(text, title=title, url=url)
        company, role = meta["company"], meta["role"]
        job["for"] = " at ".join(x for x in (role, company) if x)
        _stage(job, "writing")
        result = tailor_mod.tailor(
            text, company=company, role=role,
            answers=answers_mod.cover_letter_context(), mode=mode)
        _stage(job, "checking")
        result["notes"] = result.get("notes", []) + _quick_checks(result)
        if mode != "letter":
            _stage(job, "fitting")
        payload = _save_tailoring(text, url, company, role, result, meta, mode=mode)
        job.update({"status": "done", "company": company,
                    "role": role, "payload": payload, "mode": mode})
        # Tailoring means the user means to apply: track it as Saved.
        try:
            applied_mod.save_application(company, url, role, meta.get("salary", ""), meta.get("location", ""))
        except Exception:
            pass   # the tracker is a convenience here, never a reason to fail
        _watch_board(company, url)
    except Exception as e:
        job.update({"status": "error", "error": str(e)})


def _posting_for_tailor(job_id, url):
    """{text, title, url} for a watched job or a posting link."""
    if job_id:
        found = watch_mod.job_text(job_id)
        if not found:
            return {"error": "that job is not in the watch list"}
        url, company, title, text = found
        if len(text) >= 200:
            return {"text": text, "title": "%s at %s" % (title, company), "url": url}
    try:
        page = jd_fetch.fetch(url)
    except Exception as e:
        return {"error": "could not read the posting: %s" % e}
    text = page.get("text", "")
    if len(text) < 200:
        return {"error": "the posting page had no readable text; open it and "
                         "use Capture in the extension"}
    return {"text": text, "title": page.get("title", ""), "url": url}


@app.post("/api/tailor/start")
def api_tailor_start():
    """Start tailoring from the freshest capture, in the background."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") if data.get("mode") in tailor_mod.MODES else "both"
    job = _job_for(mode)
    with _LOCK:
        if job.get("status") == "running":
            return jsonify({"ok": True, "already_running": True,
                            "for": job.get("for", "")})
        try:
            with open(CAPTURE_PATH, encoding="utf-8") as f:
                cap = json.load(f)
        except (OSError, ValueError):
            cap = {}
        # From the job watcher: the posting text is already stored, so no
        # capture is needed. A bare url falls back to fetching the page.
        if data.get("job_id") or data.get("url"):
            cap = _posting_for_tailor(data.get("job_id"), data.get("url"))
            if cap.get("error"):
                return jsonify({"error": cap["error"]}), 400
            if data.get("job_id"):
                watch_mod.mark(data["job_id"], "tailored")
        elif not cap.get("text") or \
                time.time() - cap.get("ts", 0) > CAPTURE_FRESH_SECONDS:
            return jsonify({"error": "nothing captured recently; capture "
                                     "the posting first"}), 400
        job.clear()
        job.update({"status": "running", "started": time.time(),
                    "for": cap.get("title", "")[:80], "mode": mode})
    threading.Thread(target=_tailor_job,
                     args=(cap["text"], cap.get("title", ""),
                           cap.get("url", ""), mode),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/tailor/status")
def api_tailor_status():
    job = _job_for(request.args.get("mode"))
    out = {"status": job.get("status", "idle"),
           "for": job.get("for", ""), "mode": job.get("mode", "both")}
    if out["status"] == "running":
        out["elapsed"] = int(time.time() - job.get("started", 0))
        out["stage"] = job.get("stage", "reading")
        out["stage_elapsed"] = round(time.time() - job.get("stage_at", time.time()), 1)
        out["stages"] = [{"key": k, "label": l, "est": e}
                         for k, l, e in STAGES.get(job.get("mode", "both"), STAGES["both"])]
    elif out["status"] == "done":
        p = job.get("payload", {})
        # The letter is the extension's only once the user approved it in
        # Verbatim: before that its text is a draft and there is no PDF.
        approved = bool(p.get("id")) and os.path.exists(
            os.path.join(OUTPUT_DIR, p["id"], "cover-letter.pdf"))
        letter_txt = ""
        if approved:
            try:
                with open(os.path.join(OUTPUT_DIR, p["id"], "cover-letter.txt"), encoding="utf-8") as f:
                    letter_txt = f.read()
            except OSError:
                pass
        out.update({"company": job.get("company", ""),
                    "role": job.get("role", ""),
                    "id": p.get("id", ""),
                    "files": p.get("files", {}),
                    "notes": p.get("notes", []),
                    "letter_approved": approved,
                    "cover_letter_txt": letter_txt,
                    "pdf_error": p.get("pdf_error")})
    elif out["status"] == "error":
        out["error"] = job.get("error", "")
    return jsonify(out)


@app.post("/api/extension/capture")
def api_extension_capture():
    """The extension sends the posting text from the page you are on."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if len(text) < 200:
        return jsonify({"error": "that page had no posting text on it"}), 400
    with _LOCK:
        with open(CAPTURE_PATH, "w", encoding="utf-8") as f:
            json.dump({"url": (data.get("url") or "").strip(),
                       "title": (data.get("title") or "").strip(),
                       "text": text[:jd_fetch.MAX_CHARS],
                       "ts": time.time()}, f)
    return jsonify({"ok": True})


@app.get("/api/last-capture")
def api_last_capture():
    """The toolkit page picks up a fresh capture on load."""
    try:
        with open(CAPTURE_PATH, encoding="utf-8") as f:
            cap = json.load(f)
    except (OSError, ValueError):
        return jsonify({})
    if time.time() - cap.get("ts", 0) > CAPTURE_FRESH_SECONDS:
        return jsonify({})
    return jsonify(cap)


@app.post("/api/autofill/plan")
def api_autofill_plan():
    data = request.get_json(silent=True) or {}
    try:
        result = autofill_mod.plan(data.get("domain"), data.get("fields"))
    except autofill_mod.AutofillError as e:
        return jsonify({"error": str(e)}), 502
    if data.get("preview"):
        # One dropdown's suggestion pill, not a fill: nothing is logged.
        return jsonify(result)
    actions = [p["action"] for p in result["plan"]]
    result["fill_id"] = filllog.log_fill(
        data.get("domain"), filled=actions.count("fill"),
        flagged=actions.count("flag"), skipped=result["unidentified"],
        unmapped=result.get("unmapped", 0))
    return jsonify(result)


@app.post("/api/autofill/correction")
def api_autofill_correction():
    data = request.get_json(silent=True) or {}
    try:
        n = autofill_mod.record_corrections(data.get("domain"),
                                            data.get("corrections"))
    except autofill_mod.AutofillError as e:
        return jsonify({"error": str(e)}), 502
    filllog.log_correction(
        data.get("domain"), data.get("fill_id"),
        [c.get("signature") for c in data.get("corrections") or []
         if isinstance(c, dict)])
    return jsonify({"ok": True, "stored": n})


@app.get("/api/fills/stats")
def api_fills_stats():
    """Accuracy per domain from the fill log. Same as python3 filllog.py."""
    return jsonify(filllog.stats())


def _latest_output_meta():
    """Company, role, pay and location of the most recent tailoring run.

    Returns four empty strings when there is no run to read, which the
    callers treat as "nothing to add" rather than as a failure.
    """
    try:
        dirs = sorted(
            (os.path.join(OUTPUT_DIR, d) for d in os.listdir(OUTPUT_DIR)),
            key=os.path.getmtime, reverse=True)
        with open(os.path.join(dirs[0], "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        return (meta.get("company") or "", meta.get("role") or "",
                meta.get("salary") or "", meta.get("location") or "")
    except (OSError, ValueError, IndexError):
        return "", "", "", ""


def _company_in(company, text):
    """True when the company's name appears in text as whole words."""
    tokens = re.findall(r"[a-z0-9]+", (company or "").lower())
    if not tokens or len("".join(tokens)) < 3:
        return False
    pattern = (r"(?<![a-z0-9])" + r"[\W_]*".join(map(re.escape, tokens))
               + r"(?![a-z0-9])")
    return bool(re.search(pattern, (text or "").lower()))


# Job boards that put the employer first in the path:
# job-boards.greenhouse.io/notion/jobs/123, jobs.ashbyhq.com/notion/...
_BOARD_HOSTS = re.compile(r"greenhouse|lever\.co|ashbyhq|jobvite|smartrecruiters")
_BOARD_GENERIC = re.compile(r"^(embed|jobs|job|apply|application|job_app|"
                            r"careers|en|en-us)$", re.I)


def _company_from_url(url):
    """The employer a job board URL names, or "" when it names none."""
    from urllib.parse import parse_qs, urlparse
    try:
        u = urlparse(url or "")
    except ValueError:
        return ""
    if not _BOARD_HOSTS.search(u.netloc.lower()):
        return ""
    # Embedded Greenhouse forms name the board in ?for=
    board = (parse_qs(u.query).get("for") or [""])[0]
    if not board:
        seg = [s for s in u.path.split("/") if s]
        board = seg[0] if seg and not _BOARD_GENERIC.match(seg[0]) else ""
    board = re.sub(r"[-_]+", " ", board).strip()
    return board.title() if board else ""


def _attribution_for_page(url, title, company_hint):
    """(company, role) for answers typed on one page. See _run_for_page."""
    company, role, _ = _run_for_page(url, title, company_hint)
    return company, role


def _run_for_page(url, title, company_hint):
    """Company, role and tailoring run folder for one application page.

    Uses a tailoring run only when its company is named by the page
    itself, the same rule /api/applied follows, so a run for one employer
    never labels answers sent to another. Falls back to the employer a
    job board URL names, with no role. Returns ("", "") when neither is
    certain: an answer with no company is better than a wrong one. The
    folder is "" unless a run matched.
    """
    page = " ".join(x for x in (url, title, company_hint) if x)
    if not page.strip():
        return "", "", ""
    try:
        dirs = sorted(
            (os.path.join(OUTPUT_DIR, d) for d in os.listdir(OUTPUT_DIR)),
            key=os.path.getmtime, reverse=True)
    except OSError:
        dirs = []
    for d in dirs:
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        company = (meta.get("company") or "").strip()
        if company and _company_in(company, page):
            return company, (meta.get("role") or "").strip(), d
    return _company_from_url(url), "", ""


@app.post("/api/answers/capture-batch")
def api_answers_capture_batch():
    """The extension sends the free-text answers you typed on a form."""
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "no answers on that page"}), 400
    return jsonify(_learn_items(items, data))


@app.post("/api/answers/learn")
def api_answers_learn():
    """What you sent in the fields Verbatim drafted or inserted into.

    Sent by the extension when you submit. Each one becomes a new version
    in its group, and lessons from how you edited the draft are written in
    the background. The best answer is never touched here.
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "nothing to learn from"}), 400
    return jsonify(_learn_items(items, data))


def _groups_for(items):
    """The group for each item that does not already name one.

    Fields Verbatim helped with know their group. The rest (typed by the user,
    or by another tool) are matched together in one model call. Returns a
    list in item order: a group title, "guarded" for questions that stay
    out of the repo, or None to let capture() decide.
    """
    out = [None] * len(items)
    ask = [i for i, it in enumerate(items)
           if not (it.get("inserted") or it.get("group"))
           and len((it.get("answer") or "").strip()) >= 30]
    if len(ask) < 2:
        return out  # one question: capture() matches it the usual way
    try:
        found = answers_mod.match_many([items[i].get("question") for i in ask])
    except answers_mod.AnswersError as e:
        print(f"grouping the form failed, matching one by one: {e}",
              file=sys.stderr, flush=True)
        return out
    for i, m in zip(ask, found):
        if m["category"] != "normal":
            out[i] = "guarded"
        elif m["entry"]:
            out[i] = m["entry"]["question"]
        else:
            out[i] = m.get("title") or None
    return out


def _learn_items(items, data):
    """Record each answer as a version, then draw lessons off the request."""
    # The page the answers came from decides who they were for, never the
    # newest tailoring run on its own: that credited every saved answer to
    # whichever company was tailored for last.
    url = (data.get("url") or "").strip()
    company, role, run_dir = _run_for_page(
        url, (data.get("title") or "").strip(),
        (data.get("company") or "").strip())
    jd = _jd_snapshot(run_dir, data.get("page_text"), company, url)
    items = [i for i in items[:12] if isinstance(i, dict)]
    groups = _groups_for(items)
    results, lessons = [], []
    for item, matched in zip(items, groups):
        extra = {}
        if item.get("inserted") or item.get("group"):
            extra = {"inserted": (item.get("inserted") or "").strip(),
                     "group": (item.get("group") or "").strip() or None,
                     "jd": jd,
                     "inserted_from": str(item.get("from") or "")[:20]}
        elif matched == "guarded":
            results.append({"saved": False, "question":
                            (item.get("question") or "")[:80], "reason":
                            "authorization, EEO and salary answers stay "
                            "out of the repo"})
            continue
        elif matched:
            # Answers the user or another tool typed: grouped in one call for the
            # whole form, and kept with the posting like the user's own drafts.
            extra = {"group": matched, "jd": jd}
        if item.get("typed") is True and not extra.get("inserted"):
            # The extension saw the user type it: a sample of the user's own writing.
            extra["typed"] = True
        try:
            r = answers_mod.capture(item.get("question"), item.get("answer"),
                                    company=company, role=role, **extra)
        except answers_mod.AnswersError as e:
            r = {"saved": False, "question": (item.get("question") or "")[:80],
                 "reason": str(e)}
        results.append(r)
        if r.get("saved") and extra.get("inserted") and \
                r.get("outcome") != "unchanged":
            lessons.append((r["group"], extra["inserted"],
                            (item.get("answer") or "").strip()))
    fresh = [r["group"] for r in results if r.get("saved")
             and r.get("outcome") in ("created", "added", "replaced")]
    if lessons or fresh:
        threading.Thread(target=_lessons_job, args=(lessons, company, fresh),
                         daemon=True).start()
    # Short fields on the same form: values matching the user's profile teach this
    # site's map, the rest wait in pending_facts.json for the user to look at.
    # Never writes profile.json.
    harvested = {"learned": 0, "queued": 0}
    if data.get("fields") and data.get("domain"):
        try:
            harvested = applied_mod.harvest(data.get("domain"),
                                            data.get("fields"), load_profile())
        except Exception as e:  # learning must not cost the answers
            print(f"harvest failed: {type(e).__name__}: {e}", file=sys.stderr,
                  flush=True)
    covered = sum(1 for r in results if r.get("outcome") == "enough")
    saved = sum(1 for r in results if r["saved"]) - covered
    return {"results": results, "saved": saved, "already_covered": covered,
            "skipped": len(results) - saved - covered,
            "fields_learned": harvested["learned"],
            "facts_queued": harvested["queued"],
            "lessons_pending": len(lessons),
            "recorded_under": ", ".join(x for x in (company, role) if x)}


def _lessons_job(lessons, company, fresh=()):
    """One model call per edited field. A failure costs only that lesson,
    and is written to server.log so a run of them gets noticed. Then new
    short and long versions are proposed for each group that just got a
    new version; they wait in the Answers view for the user's approval."""
    for group, draft, final in lessons:
        try:
            answers_mod.learn_lessons(group, draft, final, company=company)
        except Exception as e:
            print(f"lessons failed for {group!r}: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
    for group in dict.fromkeys(fresh):
        try:
            _store_length_proposal(answers_mod.propose_lengths(group))
        except Exception as e:
            print(f"lengths failed for {group!r}: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)


JDS_DIR = os.path.join(BASE_DIR, "jds")


def _watch_board(company, url):
    """A company the user applies to gets its board watched from then on."""
    try:
        from jobs import boards as boards_mod
        if boards_mod.add_extra(company, url):
            print(f"watching the board for {company} from now on", flush=True)
    except Exception as e:
        print(f"could not add the board for {company}: {e}", file=sys.stderr, flush=True)


def _jd_snapshot(run_dir, page_text, company, url):
    """A link to the posting these answers were written against.

    The tailoring run's own jd.txt when one matched this page, otherwise
    the text of the page itself, saved under jds/. "" when there is
    neither.
    """
    text = ""
    if run_dir:
        try:
            with open(os.path.join(run_dir, "jd.txt"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
    if not text:
        text = (page_text or "").strip()
        if len(text) < 200:
            return ""
        if url:
            text = f"Source: {url}\n\n{text}"
    name = _slug(company, "") + ".txt"
    os.makedirs(JDS_DIR, exist_ok=True)
    path = os.path.join(JDS_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            same = f.read() == text
    except OSError:
        same = False
    n = 2
    while os.path.exists(path) and not same:
        name = f"{_slug(company, '')}-{n}.txt"
        path = os.path.join(JDS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                same = f.read() == text
        except OSError:
            same = False
        n += 1
    if not same:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text[:jd_fetch.MAX_CHARS])
    # PUBLIC_BASE is how other machines reach this server, when it runs on
    # another computer. The link is saved with the application, so set it
    # if you open Verbatim from more than one device.
    base = (os.environ.get("PUBLIC_BASE") or f"http://localhost:{PORT}").rstrip("/")
    return f"{base}/jds/{name}"


@app.get("/jds/<name>")
def get_jd_snapshot(name):
    if not re.fullmatch(r"[a-z0-9-]+\.txt", name):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(JDS_DIR, name, mimetype="text/plain")


def _jd_for_page(url, title, company_hint, page_text):
    """The posting to draft against: the latest capture when it is from
    this page or names this page's company, else the page's own text."""
    try:
        with open(CAPTURE_PATH, encoding="utf-8") as f:
            cap = json.load(f)
    except (OSError, ValueError):
        cap = {}
    company, role, run_dir = _run_for_page(url, title, company_hint)
    if run_dir:
        try:
            with open(os.path.join(run_dir, "jd.txt"), encoding="utf-8") as f:
                return f.read(), company, role
        except OSError:
            pass
    text = cap.get("text") or ""
    fresh = time.time() - cap.get("ts", 0) < 6 * 3600
    from urllib.parse import urlparse
    same_page = cap.get("url") and urlparse(cap["url"]).path.split("/apply")[0] \
        == urlparse(url or "").path.split("/apply")[0]
    if text and fresh and (same_page or (company and _company_in(company, text))):
        return text, company, role
    return (page_text or "").strip(), company, role


_FIELD_CACHE = {}  # question -> (group title or None, kind, title, ts)


@app.post("/api/answers/field")
def api_answers_field():
    """What the answer card shows when you click into a question field.

    The group this question belongs to and its best answer. Matching is a
    model call, so a question seen in the last hour is answered from
    memory. Nothing is filled: the card offers, you click.
    """
    data = request.get_json(silent=True) or {}
    q = answers_mod.clean_question(data.get("question"))
    if not q:
        return jsonify({"error": "question required"}), 400
    guarded = answers_mod.classify_guarded(q)
    if guarded:
        return jsonify({"category": guarded, "group": None})
    key = q.lower()
    hit = _FIELD_CACHE.get(key)
    try:
        if hit and time.time() - hit[3] < 3600:
            title, kind, new_title = hit[0], hit[1], hit[2]
        else:
            category, title, kind, new_title = answers_mod.group_for(q)
            if category != "normal":
                return jsonify({"category": category, "group": None})
            _FIELD_CACHE[key] = (title, kind, new_title, time.time())
        g = answers_mod.find_group(
            answers_mod.parse_repo(answers_mod.read_repo()), title) \
            if title else None
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502
    out = {"category": "normal", "kind": kind,
           "title": (g or {}).get("question") or title or new_title or q,
           "group": None}
    if g:
        # The card offers the user's own words only. An answer about another
        # company, or one a tool wrote, is never offered to put in as it
        # is: the card drafts this one from the posting instead.
        mine = answers_mod.for_prompts(g) or {}
        if answers_mod.company_specific(q, kind) or \
                not answers_mod.counts_as_own(g.get("best_source")):
            mine = {}
        out["group"] = {"title": g["question"], "best": mine.get("answer", ""),
                        "short": mine.get("short", ""), "long": mine.get("long", ""),
                        "use": answers_mod.length_for(data.get("limit"),
                                                      data.get("field")),
                        "kind": g["kind"], "lessons": len(g["lessons"]),
                        "versions": len(answers_mod.sent_versions(g)),
                        "used": g["used"][-3:]}
    out["wants_facts"] = answers_mod.wants_facts(q) or kind == "cover note"
    out["mic"] = dictate.available()
    ready = predraft.take((data.get("url") or "").strip(), q)
    if ready:
        out["ready"] = ready
    return jsonify(out)


@app.get("/api/answers/sources")
def api_answers_sources():
    """What the drafts can see right now, from this running server: the
    voice note, the interview notes, the user's reasons, and how many stored
    answers count as the user's. vault says where notes reads came from: the
    notes folder itself, or a snapshot copy when macOS refuses it."""
    voice = answers_mod.load_voice_note()
    chunks = interviewnotes.load(answers_mod.VAULT_DIR)
    out = {"voice_note_chars": len(voice), "interview_sections": len(chunks),
           "motivation_chars": len(answers_mod.load_motivation()),
           "vault": vaultio.status()}
    try:
        live = answers_mod._get_note()
        out["answer_note"] = "vault"
    except (answers_mod.ObsidianDown, answers_mod.AnswersError):
        live = None
        out["answer_note"] = "cache (the vault note cannot be read)"
    out["queued_writes"] = len(answers_mod._read_queue())
    try:
        groups = answers_mod.parse_repo(live or answers_mod.read_repo())
    except answers_mod.AnswersError:
        groups = []
    versions = [v for g in groups for v in answers_mod.sent_versions(g)]
    out["versions"] = len(versions)
    out["versions_own"] = sum(answers_mod.counts_as_own(v.get("source"))
                              for v in versions)
    out["stories"] = len(answers_mod.list_stories())
    return jsonify(out)


@app.post("/api/answers/dictate")
def api_answers_dictate():
    """An answer the user said out loud, from the field card's mic. Turned into
    text on this machine, then tidied (punctuation, filler, fit to the
    limit) without adding words. Nothing is inserted: the card shows it."""
    import base64
    data = request.get_json(silent=True) or {}
    try:
        audio = base64.b64decode(str(data.get("audio") or ""), validate=True)
    except ValueError:
        return jsonify({"error": "that recording did not come through"}), 400
    limit = data.get("limit") if isinstance(data.get("limit"), dict) else {}
    try:
        return jsonify(dictate.say(
            audio, str(data.get("mime") or ""),
            answers_mod.clean_question(data.get("question")), limit,
            tidy_it=data.get("tidy") is not False))
    except dictate.DictateError as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/answers/draft-field")
def api_answers_draft_field():
    """A draft for one field, on your click. Asks for one line when the
    answer needs something only you know."""
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_draft_for(data))
    except NoAI as e:
        return jsonify({"error": "This posting asks applicants not to use "
                        "AI. Drafting is off for this page unless you "
                        "choose Draft anyway.", "no_ai": e.quotes}), 409
    except (answers_mod.AnswersError, tailor_mod.TailorError) as e:
        return jsonify({"error": str(e)}), 502


class NoAI(Exception):
    def __init__(self, quotes):
        super().__init__("posting asks for no AI")
        self.quotes = quotes


def _posting_for(data):
    """(clean posting, company, role, checks) for a draft request.

    The posting is the one _jd_for_page picks, with hidden text and lines
    addressed to an AI taken out. checks is postcheck.check() over the
    posting and the form, so a no-AI line is caught here whatever the
    panel did.
    """
    hidden = data.get("hidden") if isinstance(data.get("hidden"), list) else []
    jd, company, role = _jd_for_page(
        (data.get("url") or "").strip(), (data.get("title") or "").strip(),
        (data.get("company") or "").strip(), data.get("page_text"))
    form_text = str(data.get("form_text") or "")
    checks = postcheck.check(jd + "\n" + str(data.get("page_text") or ""),
                             form_text, hidden)
    return postcheck.clean_posting(jd, hidden), company, role, checks


def _draft_for(data, background=False):
    """One field draft from a request body. Shared by the draft route and
    the read-ahead worker."""
    q = answers_mod.clean_question(data.get("question"))
    jd, company, role, checks = _posting_for(data)
    if checks["no_ai"] and not data.get("allow_ai"):
        raise NoAI([n["quote"] for n in checks["no_ai"]])
    try:
        groups = answers_mod.parse_repo(answers_mod.read_repo())
    except answers_mod.AnswersError:
        groups = []  # the repo makes drafts better; it is not required
    title = (data.get("group") or "").strip()
    g = answers_mod.find_group(groups, title) if title else None
    # Only the user's own words feed the draft (answers.for_prompts), and answers
    # about one company never stand in as facts for another.
    past = [p for p in (answers_mod.for_prompts(x) for x in groups
                        if x is not g and not answers_mod.specific_group(x))
            if p and p.get("answer")][:12]
    outline = answers_mod.outline_for(g)
    g = answers_mod.for_prompts(g)
    story = None
    if data.get("story"):
        try:
            story = answers_mod.find_story(str(data["story"]))
        except answers_mod.AnswersError:
            story = None
        # The story the user picked, or none: never another one from the bank.
        if not story:
            raise answers_mod.AnswersError(
                "the story you picked could not be found. Pick it again")
    confirmed = postcheck.confirmed_only(
        data.get("instructions") or [],
        jd + "\n" + str(data.get("page_text") or ""),
        str(data.get("form_text") or ""), data.get("hidden") or [])
    facts = [str(f)[:300] for f in (data.get("facts") or [])
             if isinstance(f, str)][:5]
    return answers_mod.draft_field(
        q, jd, tailor_mod.load_master(), group=g,
        kind=data.get("kind") or answers_mod.classify_kind(q),
        limit=data.get("limit") or {},
        own_line=(data.get("own_line") or "").strip(),
        allow_ask=not data.get("skip_ask"),
        adapt=bool(data.get("adapt")) and bool(g),
        company=company, role=role,
        voice_note=answers_mod.load_voice_note(), past=past,
        story=story, facts=facts, instructions=confirmed,
        background=background, samples=_voice_samples(q, jd, groups),
        interview=_interview_notes(q, jd),
        motivation=answers_mod.load_motivation(), outline=outline,
        # Draft it for me picks a story itself; Revise reworks the draft
        # the user has, with the user's note.
        stories=[] if story else _stories_for_pick(),
        revise_from=str(data.get("revise_from") or "")[:6000],
        revise_note=str(data.get("revise_note") or "")[:500])


def _stories_for_pick():
    try:
        return answers_mod.list_stories()
    except answers_mod.AnswersError:
        return []


def _voice_samples(question, posting, groups=None):
    """A few short pieces of the user's own writing for the draft to sound like.
    Style only. Never stops a draft: no samples is fine."""
    try:
        if groups is None:
            try:
                groups = answers_mod.parse_repo(answers_mod.read_repo())
            except answers_mod.AnswersError:
                groups = []
        sent = [v["text"] for g in groups for v in answers_mod.own_versions(g)]
        # What the user dictated about why the user is looking reads the way the user talks.
        sent += [p for p in answers_mod.load_motivation().split("\n")
                 if len(p.split()) >= 20]
        return voicesamples.pick(question or "", posting or "",
                                 answers_mod.VAULT_DIR, answers_mod.VERSIONS_LOG,
                                 sent=sent)
    except Exception as e:
        print(f"voice samples skipped: {e}", file=sys.stderr, flush=True)
        return []


def _interview_notes(question, posting):
    """The user's interview answers that fit this question. "" when none do or the
    vault cannot be read; drafts work without them."""
    try:
        return interviewnotes.pick(question or "", posting or "",
                                   answers_mod.VAULT_DIR)
    except Exception as e:
        print(f"interview notes skipped: {e}", file=sys.stderr, flush=True)
        return ""


def _predraft_job(job):
    """One read-ahead draft. Questions on the page are matched to their
    groups in one model call the first time any of them is drafted, and
    the field card reuses those matches, so each question then costs one
    draft call."""
    q = answers_mod.clean_question(job.get("question"))
    hit = _FIELD_CACHE.get(q.lower())
    if not job.get("group") and not (hit and time.time() - hit[3] < 3600):
        page_qs = [q] + predraft.waiting_questions(job.get("key", ""))
        page_qs = [x for x in dict.fromkeys(page_qs)
                   if not _FIELD_CACHE.get(x.lower())]
        cover = [x for x in page_qs
                 if answers_mod.classify_kind(x) == answers_mod.COVER_NOTE]
        rest = [x for x in page_qs if x not in cover]
        for x in cover:
            _, title, kind, new_title = answers_mod.group_for(x)
            _FIELD_CACHE[x.lower()] = (title, kind, new_title, time.time())
        if rest:
            found = (answers_mod.match_many(rest) if len(rest) > 1
                     else [answers_mod.match(rest[0])])
            for x, m in zip(rest, found):
                if m["category"] != "normal":
                    continue
                e = m.get("entry")
                _FIELD_CACHE[x.lower()] = (
                    e["question"] if e else None,
                    e["kind"] if e else answers_mod.classify_kind(x),
                    "" if e else (m.get("title") or ""), time.time())
        hit = _FIELD_CACHE.get(q.lower())
    if not job.get("group") and hit and hit[0]:
        job = dict(job, group=hit[0])
    return _draft_for(job, background=True)


predraft.DRAFT = _predraft_job


@app.post("/api/posting/check")
def api_posting_check():
    """What the panel shows at the top of a page: a no-AI line, visible
    instructions for applicants, and hidden or AI-addressed text. No
    model call."""
    data = request.get_json(silent=True) or {}
    hidden = data.get("hidden") if isinstance(data.get("hidden"), list) else []
    return jsonify(postcheck.check(str(data.get("page_text") or ""),
                                   str(data.get("form_text") or ""), hidden))


@app.post("/api/answers/predraft")
def api_answers_predraft():
    """Read the form's written questions ahead: queue one background draft
    per question. Nothing is inserted; the field card shows them."""
    data = request.get_json(silent=True) or {}
    qs = [x for x in (data.get("questions") or []) if isinstance(x, dict)]
    if not qs:
        return jsonify({"queued": 0})
    hidden = data.get("hidden") if isinstance(data.get("hidden"), list) else []
    checks = postcheck.check(str(data.get("page_text") or ""),
                             str(data.get("form_text") or ""), hidden)
    page = (data.get("url") or "").strip()
    if checks["no_ai"] and not data.get("allow_ai"):
        predraft.cancel(page)
        return jsonify({"queued": 0, "skipped": "no_ai",
                        "no_ai": [n["quote"] for n in checks["no_ai"]]})
    shared = {k: data.get(k) for k in ("url", "title", "company", "page_text",
                                       "form_text", "hidden", "allow_ai")}
    jobs = []
    for x in qs[:predraft.MAX_QUEUE]:
        jobs.append(dict(shared, question=x.get("question"),
                         kind=x.get("kind"), limit=x.get("limit") or {},
                         group=x.get("group") or "", skip_ask=False))
    keys = predraft.enqueue(page, jobs)
    return jsonify({"queued": len(keys)})


@app.post("/api/answers/predraft/status")
def api_answers_predraft_status():
    data = request.get_json(silent=True) or {}
    return jsonify({"drafts": predraft.status(
        (data.get("url") or "").strip(),
        [str(q) for q in data.get("questions") or []])})


_VOICE_NOTE = {"ts": 0, "text": ""}


def _voice_note():
    """The voice note, read at most once a minute: the check runs as the user
    types."""
    if time.time() - _VOICE_NOTE["ts"] > 60:
        _VOICE_NOTE.update(ts=time.time(), text=answers_mod.load_voice_note())
    return _VOICE_NOTE["text"]


@app.post("/api/answers/check")
def api_answers_check():
    """Voice hints and the skim line for the text in a field. No model
    call, so the card can ask on every pause in the user's typing."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")[:8000]
    return jsonify(voicecheck.check(text, str(data.get("question") or ""),
                                    str(data.get("company") or ""),
                                    _voice_note()))


@app.post("/api/answers/facts")
def api_answers_facts():
    """3 to 5 facts about the company and role from the posting, to pick
    from for a why-this-company answer. Facts about the company only."""
    data = request.get_json(silent=True) or {}
    jd, company, role, checks = _posting_for(data)
    try:
        facts = answers_mod.pick_facts(jd, company=company, role=role)
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"facts": facts, "company": company})


@app.get("/api/answers/stories")
def api_answers_stories():
    try:
        return jsonify({"stories": answers_mod.list_stories(),
                        "file": answers_mod.STORY_FILE})
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/answers/stories/build")
def api_answers_stories_build():
    """Find stories in the master file and the answers you sent. Only lines
    that check out against their source are kept."""
    try:
        return jsonify(answers_mod.build_stories(tailor_mod.load_master()))
    except (answers_mod.AnswersError, tailor_mod.TailorError) as e:
        return jsonify({"error": str(e)}), 502


LENGTHS_PATH = os.path.join(BASE_DIR, "data", "length_proposals.json")
_LENGTHS_LOCK = threading.Lock()


def _length_proposals():
    try:
        with open(LENGTHS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_length_proposals(d):
    os.makedirs(os.path.dirname(LENGTHS_PATH), exist_ok=True)
    tmp = LENGTHS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, LENGTHS_PATH)


def _store_length_proposal(p):
    with _LENGTHS_LOCK:
        d = _length_proposals()
        d[p["group"].strip().lower()] = p
        _save_length_proposals(d)


def _drop_length_proposal(group):
    with _LENGTHS_LOCK:
        d = _length_proposals()
        if d.pop(group.strip().lower(), None) is not None:
            _save_length_proposals(d)


@app.post("/api/answers/lengths")
def api_answers_lengths():
    """Propose short and long versions from your own text. Nothing is
    written to the note until you approve."""
    data = request.get_json(silent=True) or {}
    try:
        p = answers_mod.propose_lengths((data.get("group") or "").strip())
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502
    _store_length_proposal(p)
    return jsonify(p)


@app.post("/api/answers/lengths/approve")
def api_answers_lengths_approve():
    """You approved short and long versions. The only route that stores them."""
    data = request.get_json(silent=True) or {}
    group = (data.get("group") or "").strip()
    try:
        answers_mod.set_lengths(group, data.get("short"), data.get("long"))
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502
    _drop_length_proposal(group)
    weekly.mark_done(group)
    return jsonify({"ok": True})


@app.post("/api/answers/lengths/discard")
def api_answers_lengths_discard():
    data = request.get_json(silent=True) or {}
    _drop_length_proposal((data.get("group") or "").strip())
    return jsonify({"ok": True})


@app.get("/api/answers/this-week")
def api_answers_this_week():
    """This week's three groups worth sharpening."""
    try:
        return jsonify(weekly.this_week())
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502


def _outcomes():
    """Company -> [(date, status)] from the tracker, or None.

    Cached for five minutes, so the Answers page does not reread it each
    time it opens.
    """
    now = time.time()
    if _OUTCOMES.get("ts", 0) > now - 300:
        return _OUTCOMES.get("jobs")
    try:
        jobs = applied_mod.list_jobs()
    except Exception:
        jobs = None
    _OUTCOMES.update(ts=now, jobs=jobs)
    return jobs


_OUTCOMES = {}


def _outcome_for(version, jobs):
    """The tracker status for the application a version was sent with:
    same company, the row closest in date."""
    if not jobs or not version.get("company"):
        return ""
    norm = applied_mod._norm_company(version["company"])
    rows = [j for j in jobs if applied_mod._norm_company(j.get("company")) == norm]
    if not rows:
        return ""

    def gap(j):
        try:
            a = time.mktime(time.strptime(str(j.get("date"))[:10], "%Y-%m-%d"))
            b = time.mktime(time.strptime(version["date"], "%Y-%m-%d"))
            return abs(a - b)
        except ValueError:
            return float("inf")
    best = min(rows, key=gap)
    return "" if gap(best) > 45 * 86400 else (best.get("status") or "")


@app.get("/api/answers/groups")
def api_answers_groups():
    """Every question group for the Answers area, with outcomes from the
    tracker when it can be read."""
    try:
        groups = answers_mod.parse_repo(answers_mod.read_repo())
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502
    jobs = _outcomes()
    pending = _length_proposals()
    changes = answers_mod.draft_changes()
    out = []
    for g in groups:
        logged = changes.get(g["question"].strip().lower(), [])
        versions = [dict(v, outcome=_outcome_for(v, jobs),
                         changed=answers_mod.change_for(v, logged),
                         own=answers_mod.counts_as_own(v.get("source")))
                    for v in g["versions"]]
        out.append({"title": g["question"], "kind": g["kind"],
                    "best": g["answer"], "best_source": g.get("best_source", ""),
                    "best_own": answers_mod.counts_as_own(g.get("best_source")),
                    "short": g.get("short", ""),
                    "long": g.get("long", ""),
                    "lengths_pending": pending.get(g["question"].strip().lower()),
                    "lessons": g["lessons"],
                    "seen_as": g["seen_as"], "versions": versions,
                    # How much the user changed each draft the user sent, oldest first.
                    "changes": [c["changed"] for c in logged],
                    "can_refine": len(answers_mod.own_versions(g))
                    >= answers_mod.REFINE_MIN_VERSIONS})
    return jsonify({"groups": out, "outcomes": jobs is not None,
                    "refine_min": answers_mod.REFINE_MIN_VERSIONS})


@app.post("/api/answers/refine")
def api_answers_refine():
    """A proposed best answer from what you sent. Nothing is written."""
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(answers_mod.refine((data.get("group") or "").strip()))
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/answers/best")
def api_answers_best():
    """You approved a new best answer. The only route that changes one."""
    data = request.get_json(silent=True) or {}
    try:
        answers_mod.set_best((data.get("group") or "").strip(),
                             data.get("text"))
    except answers_mod.AnswersError as e:
        return jsonify({"error": str(e)}), 502
    weekly.note_best_set((data.get("group") or "").strip())
    weekly.mark_done((data.get("group") or "").strip())
    return jsonify({"ok": True})


@app.get("/api/tailor/cover-letter")
def api_tailor_cover_letter():
    """The cover letter PDF the last tailoring job made, base64, so the
    page can attach it when you click Attach it. Only that job's file:
    it was made from the posting on the page you asked from."""
    import base64
    # The letter button's own run when there is one, else a run that made both.
    job = LETTER_JOB if LETTER_JOB.get("status") == "done" else TAILOR_JOB
    if job.get("status") != "done" or job.get("mode") == "resume":
        return jsonify({"found": False, "reason": "no finished cover letter"})
    rel = job.get("payload", {}).get("files", {}).get("cover_letter_pdf", "")
    path = os.path.join(BASE_DIR, rel.lstrip("/"))
    if not rel or not os.path.exists(path):
        # The PDF is made only once the user approves the letter in Verbatim.
        return jsonify({"found": False, "reason": "approve the letter in Verbatim first"})
    with open(path, "rb") as f:
        blob = base64.b64encode(f.read()).decode()
    name = load_profile().get("name", "")
    return jsonify({"found": True, "company": job.get("company", ""),
                    "doc": {"filename": f"{name} Cover Letter.pdf".strip(),
                            "b64": blob}})


@app.get("/api/latest-docs")
def api_latest_docs():
    """The one resume the extension attaches, base64, employer-facing name.

    Deliberately not the most recent tailoring run. That picked whatever
    was generated last, which is the wrong document as soon as you tailor
    for one company and then applies to another, and it failed silently
    because the filename looked right either way. One known file cannot be
    wrong. Cover letters are not attached at all for the same reason: a
    stale one names the wrong company.
    """
    import base64
    name = load_profile().get("name", "Resume")
    # The page the user is applying on is a job with a resume made for it: that
    # one goes, only on a strict match (pretailor.match_page).
    page = (request.args.get("url") or "").strip()
    match = None
    if page:
        try:
            pre = tailored_jobs()
            # A resume the user made from a posting (the panel, the Tailor page)
            # counts too. Per posting the newest resume wins, and keeps the
            # morning run's job id so it still matches by that id.
            made = pretailor.made_folders(OUTPUT_DIR, {t["files"]["resume"] for t in pre})
            when = lambda t: os.path.getmtime(os.path.join(BASE_DIR, t["files"]["resume"].lstrip("/")))
            best = {}
            for t in pre + made:
                key = pretailor.norm_url(t["url"])
                cur = best.get(key)
                if not cur:
                    best[key] = t
                    continue
                pid = cur["id"] if not cur["id"].startswith("made:") else t["id"]
                win = t if when(t) > when(cur) else cur
                best[key] = dict(win, id=pid if not pid.startswith("made:") else win["id"])
            hit = pretailor.match_page(page, request.args.get("title", ""), list(best.values()))
        except Exception:
            hit = None
        path = hit and os.path.join(BASE_DIR, hit["files"]["resume"].lstrip("/"))
        match = path and os.path.exists(path) and {
            "id": hit["id"], "company": hit["company"], "role": hit["role"],
            "file": hit["files"]["resume"]}
        # The user picked Standard in the panel: the standard one, saying a
        # tailored one exists so the choice stays on screen.
        if match and request.args.get("standard") == "1":
            pass
        elif match:
            with open(path, "rb") as f:
                blob = base64.b64encode(f.read()).decode()
            return jsonify({
                "found": True, "source": "tailored resume for " + hit["company"],
                "tailored": match,
                "resume": {"filename": f"{name} Resume.pdf", "b64": blob},
            })
    # RESUME_PDF in .env wins, for a computer where the profile's path
    # does not exist.
    path = os.path.expanduser(os.environ.get("RESUME_PDF") or
        load_profile().get("resume_pdf") or "").strip()
    if not path:
        return jsonify({"found": False,
                        "reason": "no resume_pdf set in profile.json"})
    if not os.path.exists(path):
        return jsonify({"found": False,
                        "reason": f"resume not found at {path}"})
    with open(path, "rb") as f:
        blob = base64.b64encode(f.read()).decode()
    return jsonify({
        "found": True,
        "source": os.path.basename(path),
        "tailored_available": match or None,
        "resume": {"filename": f"{name} Resume.pdf", "b64": blob},
    })


# Light, dark or follow the system. Kept here rather than in the browser so
# the page and the extension's panel always agree.
UI_PATH = os.path.join(BASE_DIR, "data", "ui.json")
THEMES = ("system", "light", "dark")


@app.get("/api/ui/theme")
def api_ui_theme():
    try:
        with open(UI_PATH, encoding="utf-8") as f:
            theme = json.load(f).get("theme")
    except (OSError, ValueError):
        theme = None
    return jsonify({"theme": theme if theme in THEMES else "system"})


@app.post("/api/ui/theme")
def api_ui_theme_set():
    theme = (request.get_json(silent=True) or {}).get("theme")
    if theme not in THEMES:
        return jsonify({"error": "theme is system, light or dark"}), 400
    os.makedirs(os.path.dirname(UI_PATH), exist_ok=True)
    with open(UI_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"theme": theme}, f)
    os.replace(UI_PATH + ".tmp", UI_PATH)
    return jsonify({"theme": theme})


@app.get("/api/extension/version")
def api_extension_version():
    """The version sitting in extension/manifest.json on disk.

    The extension compares this against the version it is running. They
    differ exactly when the files changed and the extension has not been
    reloaded, which is the state that used to mean a trip to arc://extensions
    to hunt for the refresh icon.
    """
    try:
        with open(os.path.join(BASE_DIR, "extension", "manifest.json"),
                  encoding="utf-8") as f:
            return jsonify({"version": json.load(f).get("version", "")})
    except (OSError, ValueError) as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/jobs/check")
def api_jobs_check():
    """Have you applied to this already?

    Checked when a job description is pasted, not when it is logged.
    Pasting a posting is research, so this reports and never writes: a row
    only appears in the tracker when Log it is pressed.
    """
    data = request.get_json(silent=True) or {}
    try:
        found = applied_mod.check_duplicate(
            (data.get("company") or "").strip(),
            (data.get("url") or "").strip())
    except applied_mod.AppliedError as e:
        # the tracker being unreachable must not block tailoring
        return jsonify({"available": False, "error": str(e)})
    return jsonify({
        "available": True,
        "posting": found["posting"],
        "company": found["company"],
    })


# ── Job watcher (jobs/watch.py). The page reads and acts on the stored
#    list; nothing here polls boards or calls the model.

@app.get("/api/watch/jobs")
def api_watch_jobs():
    view = request.args.get("view", "all")
    if view not in ("new", "all", "applied", "dismissed"):
        view = "all"
    jobs = watch_mod.list_jobs(view)
    made = {t["id"]: t["files"] for t in tailored_jobs()}
    for j in jobs:
        j["pretailored"] = made.get(j["id"])
    from jobs.common import load_config
    cities = [c for c in (load_config().get("cities") or []) if isinstance(c, str)]
    return jsonify({"jobs": jobs, "stats": watch_mod.stats(), "prefs": watch_mod.prefs(),
                    "cities": cities})


def tailored_jobs():
    def lookup(jid):
        row = watch_mod.job_row(jid)
        return row and (row["url"], row["company"], row["title"])
    return pretailor.tailored_jobs(lookup, OUTPUT_DIR)


@app.get("/api/watch/shortlist")
def api_watch_shortlist():
    """The morning shortlist, for scripts or other tools. Read only."""
    try:
        n = int(request.args.get("n", watch_mod.SHORT_MAX))
    except ValueError:
        n = watch_mod.SHORT_MAX
    out = watch_mod.shortlist(n)
    state = pretailor.load_state()
    # Companies that turned the user down stay (a different team is a fresh
    # chance); the last rejection is shown as context.
    rejected = {}
    try:
        for r in tracker_mod.list_all():
            if r.get("status") == tracker_mod.REJECTED:
                key = _company_key(r.get("company"))
                when = r.get("rejected_on") or r.get("date") or ""
                if key and when >= rejected.get(key, ("", ""))[0]:
                    rejected[key] = (when, r.get("role") or "")
    except Exception:
        pass
    for j in out["jobs"]:
        j["pretailored"] = state.get(j["id"])
        hit = rejected.get(_company_key(j["company"]))
        j["rejected_note"] = ("Rejected %s on %s" % (hit[1] or "a role", hit[0])).replace("  ", " ") if hit else None
    return jsonify(out)


def _company_key(name):
    name = re.sub(r",?\s+(inc|llc|ltd|corp|corporation|co)\.?$", "", (name or "").strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ── Morning pre-tailoring (pretailor.py): the top shortlist
#    jobs with a stored JD get a resume and letter made ahead, checked, and
#    left for the user. launchd calls the run route early each morning.

def _pretailor_run():
    def tailor(text, company, role):
        return tailor_mod.tailor(text, company=company, role=role,
                                 answers=answers_mod.cover_letter_context())

    def save(text, url, company, role, result):
        return _save_tailoring(text, url, company, role, result)

    try:
        done = pretailor.run(watch_mod.shortlist, watch_mod.job_text, tailor, save,
                             OUTPUT_DIR, log=lambda m: print(m, flush=True))
        pretailor.RUN.update({"status": "done", "done": done,
                              "finished": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception as e:
        pretailor.RUN.update({"status": "error", "error": str(e)[:300]})


@app.post("/api/pretailor/run")
def api_pretailor_run():
    with pretailor._lock:
        if pretailor.RUN.get("status") == "running":
            return jsonify({"ok": True, "already_running": True})
        pretailor.RUN.clear()
        pretailor.RUN.update({"status": "running",
                              "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    threading.Thread(target=_pretailor_run, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/pretailor")
def api_pretailor():
    return jsonify({"run": pretailor.RUN, "jobs": pretailor.load_state()})


@app.post("/api/watch/add")
def api_watch_add():
    """A posting the user found themselves: {url, text}. The job comes back at once;
    its fit is worked out in the background and shows on the next load."""
    data = request.get_json(silent=True) or {}
    try:
        jid, existed = watch_mod.add_job(data.get("url", ""), data.get("text", ""),
                                         meta=tailor_mod.extract_meta)
    except watch_mod.AddError as e:
        return jsonify({"error": str(e)}), 400
    job = watch_mod.view_one(jid)
    if job and not job.get("base_tier"):
        def _score():
            try:
                watch_mod.score_one(jid)
            except Exception as e:
                print(f"scoring {jid} failed: {e}", file=sys.stderr, flush=True)
        threading.Thread(target=_score, daemon=True).start()
    return jsonify({"job": job, "existed": existed})


@app.post("/api/watch/status")
def api_watch_status():
    data = request.get_json(silent=True) or {}
    if tracker_mod.live() and data.get("status") in ("applied", "seen", "new"):
        return _watch_status_tracked(data)
    try:
        ok = watch_mod.set_status(data.get("id", ""), data.get("status", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    out = {"ok": ok}
    # Applied here means applied: the tracker gets it too, so it is never
    # logged twice by hand. A tracker problem is reported, never a failed
    # status. "Already applied" is a past application: its date is unknown,
    # so it is not added to the tracker (tracked: false) rather than
    # stamped today.
    if ok and data.get("status") == "applied" and data.get("tracked", True):
        out["tracked"] = _track_watched(data.get("id", ""), "Applied")
    return jsonify(out)


def _watch_status_tracked(data):
    """Once the tracker is live, applied lives there and nowhere else: Mark
    applied adds or moves the posting's row (an already-applied one with no
    date), and undoing it takes back only a row Verbatim added."""
    job_id = data.get("id", "")
    if not watch_mod.job_row(job_id):
        return jsonify({"ok": False})
    if data["status"] == "applied":
        r = _track_watched(job_id, "Applied", day=None if data.get("tracked", True) else "")
        return jsonify({"ok": "error" not in r, "tracked": r})
    if job_id in tracker_mod.job_statuses():
        r = tracker_mod.untrack(job_id)
        if not r["ok"]:
            return jsonify(r), 409
    return jsonify({"ok": watch_mod.set_status(job_id, data["status"])})


def _track_watched(job_id, status, day=None):
    j = watch_mod.job_row(job_id)
    if not j:
        return {"error": "that job is not in the watch list"}
    try:
        return applied_mod.track(j["company"], j["url"], j["title"],
                                 salary=j["salary"], location=j["location"],
                                 status=status, job_id=job_id, day=day)
    except applied_mod.AppliedError as e:
        return {"error": str(e)}
    except Exception as e:  # tracker surprises
        return {"error": f"could not update the tracker ({type(e).__name__})"}


@app.post("/api/watch/tracked")
def api_watch_tracked():
    """Save button: add this job to the tracker as Saved."""
    data = request.get_json(silent=True) or {}
    r = _track_watched(data.get("id", ""), "Saved")
    return jsonify(r), (502 if "error" in r else 200)


@app.get("/api/watch/tracked")
def api_watch_tracked_status():
    """{job id: tracker status} for watched jobs already in the tracker."""
    rows = watch_mod.list_jobs("all") + watch_mod.list_jobs("applied")
    try:
        by_url = applied_mod.statuses([j["url"] for j in rows])
    except applied_mod.AppliedError as e:
        return jsonify({"available": False, "error": str(e), "by_id": {}})
    except Exception as e:
        return jsonify({"available": False, "by_id": {},
                        "error": f"could not read the tracker ({type(e).__name__})"})
    return jsonify({"available": True,
                    "by_id": {j["id"]: by_url[j["url"]] for j in rows
                              if j["url"] in by_url}})


@app.post("/api/watch/mark")
def api_watch_mark():
    data = request.get_json(silent=True) or {}
    try:
        ok = watch_mod.mark(data.get("id", ""), data.get("what", ""),
                            bool(data.get("on", True)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": ok})


@app.post("/api/watch/prefs")
def api_watch_prefs():
    data = request.get_json(silent=True) or {}
    lines = data.get("lines")
    if not isinstance(lines, list):
        return jsonify({"error": "lines must be a list"}), 400
    return jsonify({"prefs": watch_mod.save_prefs(lines)})


@app.post("/api/applied")
def api_applied():
    """You pressed Log it after submitting an application.

    Records the application in the tracker and learns from what you sent.
    The two are reported separately: an unreachable tracker must not cost
    the learning, and a learning problem must not lose the tracker row.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company required"}), 400

    duplicate = None
    try:
        found = applied_mod.check_duplicate(company, data.get("url") or "")
        if found["posting"]:
            duplicate = found["posting"]
    except applied_mod.AppliedError:
        pass  # advisory only; never block the log

    recorded, record_error = False, None
    if duplicate and duplicate.get("status") == "Saved":
        # Saved when the user tailored for it; applying moves that same row on.
        try:
            recorded = applied_mod.promote_saved(duplicate)
        except (applied_mod.AppliedError, Exception) as e:
            record_error = str(e)
    elif duplicate and not data.get("force"):
        record_error = (f"already logged on {duplicate.get('date')} "
                        f"as {duplicate.get('status')}; not added again")
    else:
        # The role cannot come from the page: this fires on the confirmation
        # screen, where the heading is "Thank you". It comes from the last
        # tailoring instead, which named the role from the posting itself.
        # Only used when the company matches, so a stale run from a different
        # employer cannot label this row.
        role = (data.get("role") or "").strip()
        salary, location = "", ""
        meta_company, meta_role, meta_salary, meta_location = \
            _latest_output_meta()
        # Only when the run was for this employer. A stale run from a
        # different company must not label this row, and a wrong salary in
        # the tracker is worse than an empty one.
        if meta_company and \
                meta_company.strip().lower() == company.strip().lower():
            role = role or meta_role
            salary, location = meta_salary, meta_location
        try:
            recorded = applied_mod.record_application(
                company, data.get("url") or "", role,
                salary=salary, location=location)
        except applied_mod.AppliedError as e:
            record_error = str(e)
        _watch_board(company, data.get("url") or "")

    learning = applied_mod.harvest(data.get("domain"), data.get("fields"),
                                   load_profile())
    saved = applied_mod.save_answers(data.get("answers"), company=company)

    return jsonify({"recorded": recorded, "record_error": record_error,
                    "duplicate": duplicate,
                    "learned": learning["learned"],
                    "queued": learning["queued"], "answers_saved": saved})


@app.get("/api/autofill/learned")
def api_autofill_learned():
    out = autofill_mod.learned()
    out["pending"] = applied_mod.pending()
    return jsonify(out)


@app.get("/test/<name>")
def get_test_page(name):
    """Replica ATS forms for extension testing. Local only, like the rest.

    With ?harness=1 the page comes back with the real content script and a
    stub of the chrome.runtime messaging it normally talks to, so headless
    Chrome runs exactly the filling code the extension runs, with no
    browser extension installed. Same origin as this server, so the stub
    can just fetch the same routes the service worker would.
    """
    if not re.fullmatch(r"[a-z0-9-]+(\.html)?", name):
        return jsonify({"error": "not found"}), 404
    if not name.endswith(".html"):
        name += ".html"
    if request.args.get("harness") != "1":
        return send_from_directory(os.path.join(BASE_DIR, "tests", "ats"), name)

    path = os.path.join(BASE_DIR, "tests", "ats", name)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    with open(path, encoding="utf-8") as f:
        html = f.read()
    return Response(html.replace("</body>", HARNESS + "</body>"),
                    mimetype="text/html")


HARNESS = """
<script>
window.chrome = {
  runtime: {
    sendMessage: async (msg) => {
      const routes = {
        "plan": ["POST", "/api/autofill/plan"],
        "corrections": ["POST", "/api/autofill/correction"],
        "answers-suggest": ["POST", "/api/answers/suggest"],
        "save-answers": ["POST", "/api/answers/capture-batch"],
        "capture": ["POST", "/api/extension/capture"],
        "latest-docs": ["GET", "/api/latest-docs"],
        "answers-field": ["POST", "/api/answers/field"],
        "answers-draft-field": ["POST", "/api/answers/draft-field"],
        "answers-dictate": ["POST", "/api/answers/dictate"],
        "answers-learn": ["POST", "/api/answers/learn"],
        "tailor-start": ["POST", "/api/tailor/start"],
        "tailor-status": ["GET", "/api/tailor/status"],
        "cover-letter-doc": ["GET", "/api/tailor/cover-letter"],
        "watch-jobs": ["GET", "/api/watch/jobs?view=all"],
        "jobs-check": ["POST", "/api/jobs/check"],
        "posting-check": ["POST", "/api/posting/check"],
        "answers-check": ["POST", "/api/answers/check"],
        "answers-facts": ["POST", "/api/answers/facts"],
        "answers-stories": ["GET", "/api/answers/stories"],
        "answers-predraft": ["POST", "/api/answers/predraft"],
        "answers-predraft-status": ["POST", "/api/answers/predraft/status"],
      };
      const p = msg.payload || {};
      if (msg.type === "letter-get") routes[msg.type] = ["GET", "/api/letter/" + encodeURIComponent(p.id)];
      if (msg.type === "letter-doc") routes[msg.type] = ["GET", "/api/letter/" + encodeURIComponent(p.id) + "/doc"];
      if (/^letter-(save|revise|approve|learn)$/.test(msg.type))
        routes[msg.type] = ["POST", "/api/letter/" + encodeURIComponent(p.id) + "/" + msg.type.slice(7)];
      const r = routes[msg.type];
      if (!r) return { ok: false, error: "unknown message " + msg.type };
      const res = await fetch(r[1], r[0] === "GET" ? {} : {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(msg.payload || {}),
      });
      const data = await res.json().catch(() => ({}));
      return res.ok ? { ok: true, data } : { ok: false, error: data.error };
    },
    onMessage: { addListener: () => {} },
    getURL: (path) => "/test/" + path,
  },
};
</script>
<script src="/test/content.js"></script>
"""


@app.get("/test/content.js")
def get_test_content_script():
    """The extension's content script, for harness pages only."""
    return send_from_directory(os.path.join(BASE_DIR, "extension"),
                               "content.js", mimetype="text/javascript")


@app.get("/test/fonts/<name>")
def get_test_font(name):
    """The panel's bundled fonts, for harness pages only."""
    if not re.fullmatch(r"[a-z0-9-]+\.woff2", name):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(os.path.join(BASE_DIR, "extension", "fonts"), name,
                               mimetype="font/woff2")


TEST_REPORT_PATH = os.path.join(BASE_DIR, "tests", "last_report.json")


@app.post("/api/test/report")
def api_test_report():
    """Replica test pages post their fill results here for the runner."""
    with open(TEST_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(request.get_json(silent=True) or {}, f)
    return jsonify({"ok": True})


@app.get("/api/test/report")
def api_test_report_get():
    try:
        with open(TEST_REPORT_PATH, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (OSError, ValueError):
        return jsonify({})


# ── Prep: Cue decks for jobs at an interview stage (prep.py)

@app.get("/api/prep")
def api_prep():
    try:
        return jsonify(prep_mod.rows())
    except applied_mod.AppliedError as e:
        return jsonify({"jobs": [], "error": str(e)})
    except Exception as e:  # tracker surprises
        return jsonify({"jobs": [], "error": f"could not load jobs ({type(e).__name__})"})


@app.get("/api/prep/pipeline")
def api_prep_pipeline():
    try:
        return jsonify(prep_mod.pipeline())
    except applied_mod.AppliedError as e:
        return jsonify({"available": False, "error": str(e)})
    except Exception as e:  # tracker surprises
        return jsonify({"available": False,
                        "error": f"could not load jobs ({type(e).__name__})"})


# Deleting from Made: the folder moves to output/.trash
# and comes back on Undo; the trash is emptied after 7 days. Tracker rows
# are never touched. The board and the extension see the PDF is gone and
# fall back to Tailor resume and the standard resume.
TRASH_DIR = os.path.join(OUTPUT_DIR, ".trash")
TRASH_DAYS = 7


def _purge_trash():
    try:
        names = os.listdir(TRASH_DIR)
    except OSError:
        return
    cutoff = time.time() - TRASH_DAYS * 86400
    for name in names:
        d = os.path.join(TRASH_DIR, name)
        try:
            with open(os.path.join(d, ".deleted_at"), encoding="utf-8") as f:
                when = float(f.read().strip())
        except (OSError, ValueError):
            when = os.path.getmtime(d)
        if when < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _made_uses():
    """{out_id: {job_id, applied, used}} for the Made list. job_id is the
    watched posting the folder was made for (pre-tailoring, else the same
    posting link). applied: a tracker row already has it. used: what the
    delete confirm warns about."""
    state = pretailor.load_state()
    jobs = {e["out_id"]: jid for jid, e in state.items() if e.get("out_id")}
    try:
        rows = tracker_mod.list_all()
    except Exception:
        rows = []
    by_url = {}
    for jid, _, _, url, _ in tracker_mod._watched():
        if url:
            by_url.setdefault(pretailor.norm_url(url), jid)
    out = {}
    for name in os.listdir(OUTPUT_DIR) if os.path.isdir(OUTPUT_DIR) else []:
        try:
            with open(os.path.join(OUTPUT_DIR, name, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        src = pretailor.norm_url(meta.get("source_url") or "")
        jid = jobs.get(name) or (src and by_url.get(src)) or ""
        info = {"job_id": jid, "applied": False, "used": ""}
        for r in rows:
            if (jid and r.get("job_id") == jid) or (src and r.get("link") and pretailor.norm_url(r["link"]) == src):
                info["applied"] = True
                info["used"] = "This was sent with your %s application." % (r.get("company") or meta.get("company") or "")
                break
        else:
            if jobs.get(name):
                info["used"] = "It is the Tailored resume on the %s job on your board." % (
                    state[jid].get("company") or meta.get("company") or "")
        out[name] = info
    return out


@app.post("/api/tailor/delete")
def api_tailor_delete():
    out_id = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    src = os.path.join(OUTPUT_DIR, out_id)
    if not re.fullmatch(r"[a-z0-9-]+", out_id) or not os.path.isdir(src):
        return jsonify({"error": "not found"}), 404
    os.makedirs(TRASH_DIR, exist_ok=True)
    dst = os.path.join(TRASH_DIR, out_id)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.move(src, dst)
    with open(os.path.join(dst, ".deleted_at"), "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    return jsonify({"ok": True})


@app.post("/api/tailor/restore")
def api_tailor_restore():
    out_id = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    src = os.path.join(TRASH_DIR, out_id)
    dst = os.path.join(OUTPUT_DIR, out_id)
    if not re.fullmatch(r"[a-z0-9-]+", out_id) or not os.path.isdir(src):
        return jsonify({"error": "not in the trash"}), 404
    if os.path.exists(dst):
        return jsonify({"error": "a folder with that name is back already"}), 409
    os.remove(os.path.join(src, ".deleted_at")) if os.path.exists(os.path.join(src, ".deleted_at")) else None
    shutil.move(src, dst)
    return jsonify({"ok": True})


@app.get("/api/tailor/recent")
def api_tailor_recent():
    """The tailored resumes and letters already made, newest first."""
    _purge_trash()
    try:
        uses = _made_uses()
    except Exception:
        uses = {}
    out = []
    try:
        names = sorted(os.listdir(OUTPUT_DIR), reverse=True)
    except OSError:
        names = []
    for name in names:
        d = os.path.join(OUTPUT_DIR, name)
        if not re.fullmatch(r"[a-z0-9-]+", name) or not os.path.isdir(d):
            continue
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        has = lambda n: os.path.exists(os.path.join(d, n))
        out.append({
            "id": name, "company": meta.get("company") or "", "role": meta.get("role") or "",
            "created": meta.get("created") or name[:10], "url": meta.get("source_url") or "",
            "resume": f"/output/{name}/resume.pdf" if has("resume.pdf") else "",
            "letter": f"/output/{name}/cover-letter.pdf" if has("cover-letter.pdf") else "",
            # A letter still waiting for the user's review (or edited since its PDF).
            "letter_review": has("letter.json") and not has("cover-letter.pdf"),
            "used": uses.get(name, {}).get("used", ""),
            "job_id": uses.get(name, {}).get("job_id", ""),
            "applied": uses.get(name, {}).get("applied", False),
        })
        if len(out) >= 20:
            break
    return jsonify({"items": out})


@app.post("/api/prep/make")
def api_prep_make():
    """Make or update one job's deck in the background.

    Takes {job_id} or {company, date} (what an outside prep tool has),
    plus an optional jd: a pasted description or a posting link. Without a
    JD on file or in the request it refuses with need_jd, and nothing is
    generated. Poll GET /api/prep/status?job=<id> for the result.
    """
    data = request.get_json(silent=True) or {}
    try:
        job = prep_mod.find_job(data.get("job_id"), data.get("company"), data.get("date"))
    except applied_mod.AppliedError as e:
        return jsonify({"error": str(e)}), 502
    if not job:
        return jsonify({"error": "no job at Phone Screen, Interviewing or Onsite "
                                 "matches that in the tracker"}), 404
    jid = prep_mod.job_id(job)
    raw = (data.get("jd") or "").strip()
    if raw:
        try:
            text, _ = _resolve_jd(raw)
        except jd_fetch.JdFetchError as e:
            return jsonify({"error": str(e), "need_jd": True}), 400
        if len(text) < prep_mod.MIN_JD:
            return jsonify({"error": "That looks too short to be the whole job "
                                     "description. Paste all of it.", "need_jd": True}), 400
        prep_mod.save_pasted(jid, text)
    if not prep_mod.jd_for(job):
        return jsonify({"error": "No job description found for this job. Paste it "
                                 "to make the deck.", "need_jd": True}), 400
    started = prep_mod.start(job)
    return jsonify({"ok": True, "job": jid, "already_running": not started})


@app.get("/api/prep/status")
def api_prep_status():
    jid = request.args.get("job", "")
    run = prep_mod.RUNS.get(jid)
    if not run:
        return jsonify({"status": "idle"})
    out = {k: v for k, v in run.items() if k != "result"}
    if run.get("result"):
        out.update(run["result"])
    return jsonify(out)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "llm": llm.describe(),
                    "notes_dir": answers_mod.VAULT_DIR,
                    "obsidian_key": bool(os.environ.get("OBSIDIAN_API_KEY"))})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False)
