"""What happens once you actually submit an application.

Two jobs, both triggered by you pressing Log it. Nothing here watches for
a submit and acts on its own, and nothing here submits anything.

1. Record it in the applications tracker (tracker.py).
   Company, role, pay, location, date, link, status Applied. Everything
   beyond the company comes from the last tailoring run, and only when its
   company matches this one, because this fires on a confirmation page
   where the heading says "Thank you" rather than the job title.

2. Learn from what you actually sent. Three channels, in order of how much
   trust each one deserves:

   - A value that exactly matches something already in profile.json says
     what that field was asking for, with no guessing at all. That
     mapping is written into the autofill map for the domain.
   - Long free text goes to the answer repo through answers.py, which
     already guards the categories that must never be stored.
   - Anything else short and unrecognised is queued in pending_facts.json
     for you to look at. Nothing here ever writes profile.json: a fact you
     did not put there yourself is a fact the toolkit should not be
     filling in verbatim on a real application.

Authorization, EEO and salary values never enter any of the three.
"""

import json
import os
import re
import threading
from datetime import date

import answers as answers_mod
import autofill as autofill_mod
import tracker as tracker_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_PATH = os.path.join(BASE_DIR, "pending_facts.json")

_LOCK = threading.RLock()

# Never learned, never queued, never sent to the answer repo. Same
# categories the rest of the toolkit refuses, checked on the label and on
# the value, since either one can give the category away.
_GUARDED_LABEL = re.compile(
    r"race|ethnicit|gender|disabilit|veteran|salary|compensation|"
    r"desired pay|pay expectation|sponsor|visa|work authori|"
    r"authorized to work|immigration|orientation|transgender|lgbtq", re.I)


class AppliedError(Exception):
    pass


def _norm_company(name):
    """Company names for comparison: case, punctuation and suffixes vary."""
    n = (name or "").strip().lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\b(inc|llc|ltd|corp|co|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _job_key(url):
    """A posting's identity, ignoring the noise around it.

    The same posting arrives as boards.greenhouse.io/x/jobs/123, as
    job-boards.greenhouse.io/x/jobs/123, as a company careers page with
    ?gh_jid=123, as Greenhouse's embedded form with ?token=123, and with
    ?gh_src= tracking on the end. A long id (7 digits or more, or a UUID
    as Ashby and Lever use, also in ?ashby_jid=) names one posting whatever
    site shows it, so it is the key on its own. Anything shorter keeps its
    host: the host without its subdomain plus the last digits in the path.
    """
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^https?://", "", u).split("#")[0]
    path, _, query = u.partition("?")
    uuid = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", path)
    m = re.search(r"(?:^|&)ashby_jid=([0-9a-f-]{36})", query)
    if uuid or m:
        return "uuid/" + (uuid[-1] if uuid else m.group(1))
    m = re.search(r"(?:^|&)gh_jid=(\d{4,})", query) or \
        ("greenhouse.io/embed" in path and re.search(r"(?:^|&)token=(\d{4,})", query))
    if m:
        return "id/" + m.group(1)
    u = path.rstrip("/")
    if not u:
        return ""
    host, _, path = u.partition("/")
    host = re.sub(r"^(www|boards|job-boards|jobs|apply|careers)\.", "", host)
    ids = re.findall(r"\d{4,}", path)
    if ids and len(ids[-1]) >= 7:
        return "id/" + ids[-1]
    return f"{host}/{ids[-1]}" if ids else f"{host}/{path}"


def list_jobs():
    """Every row in the tracker (tracker.py)."""
    return tracker_mod.list_all()


def check_duplicate(company="", url="", jobs=None):
    """Have you applied to this already?

    Returns {"posting": row|None, "company": [rows]}. A posting match is
    the same job id on the same board and is definite. A company match is
    a softer signal: it matches on company alone, so two roles at one
    employer look identical to it even though the tracker now records a
    role.
    """
    if jobs is None:
        jobs = list_jobs()
    key = _job_key(url)
    norm = _norm_company(company)

    posting = None
    if key:
        for job in jobs:
            if _job_key(job.get("link")) == key:
                posting = job
                break

    company_hits = []
    if norm:
        for job in jobs:
            if _norm_company(job.get("company")) == norm and job is not posting:
                company_hits.append(job)

    return {"posting": posting, "company": company_hits}


def record_application(company, url="", role="", salary="", location=""):
    """Add one Applied row to the tracker. The caller has already checked for this
    posting, and force means the user applied again, so this always adds."""
    company = (company or "").strip()
    if not company:
        raise AppliedError("company required")
    tracker_mod.add({"company": company, "role": role or "", "link": url or "",
                     "salary": salary or "", "location": location or "", "status": "Applied"})
    tracker_mod.link_watched()
    return True


def save_application(company, url="", role="", salary="", location=""):
    """Add a Saved row when the user tailors for a job the user has not applied to yet.
    Nothing is added when the posting is already in the tracker. Returns
    True when a row was added."""
    company = (company or "").strip()
    if not company:
        return False
    return tracker_mod.track(company, url, role, salary, location, status="Saved")["added"]


def track(company, url="", role="", salary="", location="", status="Saved", job_id=None, day=None):
    """Put a watched job in the tracker as Saved or Applied, at most once.
    Returns the posting's status afterwards and whether a row was added."""
    if status not in ("Saved", "Applied"):
        raise AppliedError("status must be Saved or Applied")
    r = tracker_mod.track(company, url, role, salary, location, status=status, job_id=job_id, day=day)
    return {"status": r["status"], "added": r["added"]}


def statuses(urls):
    """{url: tracker status} for the postings already in the tracker."""
    by_key = {}
    for job in list_jobs():
        k = _job_key(job.get("link"))
        if k:
            by_key.setdefault(k, job.get("status") or "Saved")
    return {u: by_key[_job_key(u)] for u in urls if _job_key(u) in by_key}


def promote_saved(row):
    """Move the user's Saved row for this posting to Applied, which stamps today
    as the applied date. Returns True when it moved."""
    if row.get("id") and row.get("status") == "Saved":
        tracker_mod.update(row["id"], {"status": "Applied"})
        return True
    return False


def _profile_lookup(profile):
    """Every value the toolkit already knows, mapped back to its key.

    Reversed on purpose: filling asks "what goes in this field", learning
    asks "what field was this". A value you typed that matches one of your
    own stored facts identifies the field with no model call and no guess.
    """
    out = {}
    for key, value in autofill_mod._values(profile).items():
        v = (value or "").strip().lower()
        if len(v) >= 3 and key not in ("eeo_gender", "eeo_race",
                                       "eeo_veteran", "eeo_disability",
                                       "authorized_to_work",
                                       "requires_sponsorship",
                                       "work_authorization_details"):
            out.setdefault(v, key)
    for row, entry in enumerate(profile.get("employment_history") or []):
        for field, key in (("company", "employer"), ("title", "job_title"),
                           ("location", "employer_location")):
            v = (entry.get(field) or "").strip().lower()
            if len(v) >= 3:
                out.setdefault(v, key)
    for entry in profile.get("education") or []:
        for field in ("school", "degree", "degree_type", "discipline"):
            v = (entry.get(field) or "").strip().lower()
            if len(v) >= 3:
                out.setdefault(v, field)
    return out


def _read_pending():
    return autofill_mod._read_json(PENDING_PATH, [])


def harvest(domain, fields, profile):
    """Learn from the values on a submitted form.

    fields: [{signature, label, value}] as the page had them at submit.
    Returns counts, and never raises for one bad field.
    """
    domain = (domain or "").strip().lower()
    if not domain or not isinstance(fields, list):
        return {"learned": 0, "queued": 0}

    known = _profile_lookup(profile)
    learned, queued = {}, []
    seen_sigs = {}
    for f in fields:
        sig = (f.get("signature") or "").strip()
        for s in (sig,):
            seen_sigs[s] = seen_sigs.get(s, 0) + 1

    for f in fields:
        sig = (f.get("signature") or "").strip()
        label = (f.get("label") or "").strip()
        value = (f.get("value") or "").strip()
        if not sig or not value or len(value) > 200:
            continue
        if _GUARDED_LABEL.search(label) or _GUARDED_LABEL.search(value):
            continue
        if seen_sigs.get(sig, 0) > 1:
            continue  # ambiguous on this page, same rule as filling
        key = known.get(value.lower())
        if key:
            learned[sig] = key
        elif len(value) < 60:
            queued.append({"domain": domain, "signature": sig,
                           "label": label[:120], "value": value,
                           "seen": date.today().strftime("%Y-%m-%d")})

    if learned:
        with _LOCK:
            all_maps = autofill_mod._read_json(autofill_mod.MAP_PATH, {})
            site = all_maps.setdefault(domain, {})
            for sig, key in learned.items():
                site[sig] = key
            autofill_mod._write_json_atomic(autofill_mod.MAP_PATH, all_maps)

    if queued:
        with _LOCK:
            pending = _read_pending()
            have = {(p.get("domain"), p.get("signature"), p.get("value"))
                    for p in pending}
            fresh = [q for q in queued
                     if (q["domain"], q["signature"], q["value"]) not in have]
            if fresh:
                autofill_mod._write_json_atomic(PENDING_PATH,
                                                (pending + fresh)[-200:])
            queued = fresh

    return {"learned": len(learned), "queued": len(queued)}


def save_answers(items, company=""):
    """Free-text answers into the repo. Guarded categories are refused
    inside answers.capture, so this only counts what got through."""
    saved = 0
    for item in (items or [])[:12]:
        question = (item.get("question") or "").strip()
        answer = (item.get("answer") or "").strip()
        if not question or len(answer) < 30:
            continue
        try:
            result = answers_mod.capture(question, answer, company=company,
                                         role="")
            saved += 1 if result.get("saved") else 0
        except answers_mod.AnswersError:
            continue  # Obsidian closed, or a guarded question; neither is fatal
    return saved


def pending():
    """What is waiting for you to look at, newest first."""
    return list(reversed(_read_pending()))
