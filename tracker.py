"""Applications: every job the user applied to or saved, with its stage.

One record per application. The rules:

- status is one of Saved, Applied, Phone Screen, Interviewing, Onsite,
  Offer, Rejected or No response. A rejected row keeps the stage it
  reached in rejected_at.
- An Applied row with no answer after 28 days becomes No response, once:
  no_response_at stays, so moving it back to Applied by hand sticks.
- Status changes set the dates: follow_up a week after applying or the
  day after a call, last_contact on every change, rejected_on on a no.
- Moving to a call stage leaves a prep request; a call that ends leaves a
  debrief request. An outside tool can poll /api/jobs/prep, start its own
  prep session and claim the request, so each runs once.

Rows live in data/tracker.sqlite (TRACKER_DB for tests).
"""

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

bp = Blueprint("tracker", __name__)

STAGES = ["Applied", "Phone Screen", "Interviewing", "Onsite", "Offer"]
SAVED = "Saved"
REJECTED = "Rejected"
NO_RESPONSE = "No response"
STATUSES = [SAVED] + STAGES + [REJECTED, NO_RESPONSE]
CALL_STAGES = ("Phone Screen", "Interviewing", "Onsite")
CLOSED = (REJECTED, "Offer", NO_RESPONSE)
NO_RESPONSE_DAYS = 28
SOURCES = ["Cold", "Referral", "Recruiter", "Network"]
# Onsite shares Interviewing's prep pass.
PREP_TIERS = {"Phone Screen": "screen", "Interviewing": "interview", "Onsite": "interview"}

FIELDS = ("company", "role", "date", "status", "link", "salary", "location",
          "source", "referral", "follow_up", "last_contact", "no_response_at",
          "rejected_at", "rejected_on", "job_id")
EDITABLE = ("company", "role", "date", "status", "link", "salary", "location",
            "source", "referral", "follow_up")


class TrackerError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def db_path():
    return os.environ.get("TRACKER_DB") or os.path.join(DATA_DIR, "tracker.sqlite")


def db():
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    con = sqlite3.connect(db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company TEXT NOT NULL, role TEXT DEFAULT '', date TEXT, status TEXT,
        link TEXT DEFAULT '', salary TEXT DEFAULT '', location TEXT DEFAULT '',
        source TEXT, referral TEXT, follow_up TEXT, last_contact TEXT,
        no_response_at TEXT, rejected_at TEXT, rejected_on TEXT,
        prep TEXT DEFAULT '{}',
        origin TEXT DEFAULT 'verbatim',   -- where the row came from
        added TEXT, updated TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS applications_date ON applications(date)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
    if "job_id" not in cols:
        # The watched posting this application is for, when there is one.
        con.execute("ALTER TABLE applications ADD COLUMN job_id TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return con


def live():
    """The tracker is always on. Kept as a function because the server and
    the job watcher ask before writing to it."""
    return True


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _plus_days(day, n):
    try:
        base = date.fromisoformat(str(day)[:10])
    except ValueError:
        base = date.today()
    return (base + timedelta(days=n)).isoformat()


def _stage(value):
    """The stage a rejected job reached; anything unknown is Applied."""
    value = (value or "").strip()
    return value if value in STAGES else STAGES[0]


# ── Salary, written as '$140K – $170K', currency kept.

_SALARY_CODES = {
    "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥", "INR": "₹",
    "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "SGD": "S$", "HKD": "HK$",
    "BRL": "R$", "MXN": "MX$",
    # No widely read symbol, so the code stays and does the job itself.
    "CHF": "CHF ", "SEK": "SEK ", "NOK": "NOK ", "DKK": "DKK ",
    "PLN": "PLN ", "CZK": "CZK ", "ILS": "₪", "ZAR": "R",
}

# Qualified dollars, checked before the bare symbol for the same reason.
_SALARY_PREFIXES = [("US$", "$"), ("C$", "CA$"), ("CA$", "CA$"),
                    ("A$", "A$"), ("AU$", "A$"), ("NZ$", "NZ$"),
                    ("S$", "S$"), ("HK$", "HK$"), ("R$", "R$")]

_SALARY_SYMBOLS = [("£", "£"), ("€", "€"), ("¥", "¥"), ("₹", "₹"), ("₪", "₪")]


def salary_currency(s):
    """The currency a salary string is written in, as the prefix to print.

    Falls back to a plain dollar sign, which is what this always assumed and
    is right for most US postings. Returns the prefix and the text with
    the currency taken out, so the number parsing below sees digits only.
    """
    upper = s.upper()
    for code, prefix in _SALARY_CODES.items():
        if re.search(rf"\b{code}\b", upper):
            return prefix, re.sub(rf"(?i)\b{code}\b", " ", s)
    for token, prefix in _SALARY_PREFIXES:
        if token in upper:
            return prefix, re.sub(re.escape(token), " ", s, flags=re.I)
    for symbol, prefix in _SALARY_SYMBOLS:
        if symbol in s:
            return prefix, s.replace(symbol, " ")
    return "$", s


def normalize_salary(raw):
    """Standardize messy salary-range strings to canonical '$140K – $170K'.

    Handles K/k notation, 'to'/'-'/'–' separators, commas, cents, and a
    trailing K that applies to the whole range (e.g. '$155-185K' ->
    '$155K – $185K'). Values are rounded to the nearest thousand.
    Unparseable input is returned unchanged.

    The currency is kept rather than assumed: a London posting used to land
    as '$70K – $90K' because the dollar sign was hardcoded, which is a wrong
    number rather than a missing one. Anything with no currency in it is
    still read as dollars.
    """
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    if not s:
        return s
    has_k = "k" in s.lower()
    prefix, cleaned = salary_currency(s)
    cleaned = cleaned.replace("$", "").replace(",", "")
    nums = []
    for num_str, k_flag in re.findall(r"(\d+(?:\.\d+)?)\s*([kK])?", cleaned):
        if not num_str:
            continue
        val = float(num_str)
        if k_flag or (has_k and val < 1000):
            val *= 1000
        nums.append(val)
    if not nums:
        return s  # couldn't parse, so leave it as the user typed it
    fmt = lambda v: f"{prefix}{round(v / 1000):,}K"
    if len(nums) == 1:
        return fmt(nums[0])
    return f"{fmt(nums[0])} – {fmt(nums[1])}"




# ── The rules

def _row(r):
    d = {k: r[k] for k in r.keys()}
    try:
        d["prep"] = json.loads(d.get("prep") or "{}")
    except ValueError:
        d["prep"] = {}
    return d


def sweep(con, today=None):
    """Applied rows older than 28 days become No response, once."""
    today = today or date.today()
    cutoff = (today - timedelta(days=NO_RESPONSE_DAYS)).isoformat()
    cur = con.execute(
        "UPDATE applications SET status=?, no_response_at=?, follow_up=NULL, updated=? "
        "WHERE status='Applied' AND no_response_at IS NULL AND COALESCE(date,'9999') < ?",
        (NO_RESPONSE, today.isoformat(), _now(), cutoff))
    return cur.rowcount


def stamp_status(job, was, today=None):
    """Dates that follow from the status. Runs on add and on every update."""
    today = (today or date.today()).isoformat()
    status = job.get("status")
    if status == was:
        return
    if was is not None:
        job["last_contact"] = today
    if status == "Applied":
        if was == SAVED:
            job["date"] = today
        job["follow_up"] = _plus_days(job.get("date") or today, 7)
    elif status in CALL_STAGES:
        job["follow_up"] = _plus_days(today, 1)       # thank-you note the next day
    else:
        job["follow_up"] = None                       # Saved, Offer, Rejected, No response
    if status == REJECTED:
        job["rejected_on"] = today


def mark_prep(job, was):
    """A call stage asks for prep; a call that ends asks for a debrief."""
    prep = job.setdefault("prep", {})
    ended = job.get("status") in CLOSED
    if was in PREP_TIERS and (ended or (was, job.get("status")) == ("Interviewing", "Onsite")):
        key = "debrief:" + was
        if key not in prep:
            prep[key] = {"stage": was, "outcome": job["status"], "requested": _now()}
    tier = PREP_TIERS.get(job.get("status"))
    if tier and job.get("status") != was and tier not in prep:
        prep[tier] = {"stage": job["status"], "requested": _now()}


def _save(con, job):
    job["updated"] = _now()
    cols = list(FIELDS) + ["prep", "updated"]
    vals = [job.get(k) for k in FIELDS] + [json.dumps(job.get("prep") or {}), job["updated"]]
    if job.get("id"):
        con.execute("UPDATE applications SET %s WHERE id=?" % ", ".join(c + "=?" for c in cols),
                    vals + [job["id"]])
        return job["id"]
    cols += ["origin", "added"]
    vals += [job.get("origin") or "verbatim", job.get("added") or job["updated"]]
    cur = con.execute("INSERT INTO applications (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))), vals)
    return cur.lastrowid


def _clean(data):
    out = {}
    for k in EDITABLE:
        if k in data:
            v = data[k]
            v = v.strip() if isinstance(v, str) else v
            out[k] = normalize_salary(v) if k == "salary" else v
    if out.get("status") is not None and out["status"] not in STATUSES:
        raise TrackerError("unknown status: %s" % out["status"])
    return out


def list_all():
    con = db()
    with con:
        sweep(con)
    rows = [_row(r) for r in con.execute(
        "SELECT * FROM applications ORDER BY COALESCE(date,'') DESC, id DESC")]
    con.close()
    return rows


def get(app_id):
    con = db()
    r = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    con.close()
    return _row(r) if r else None


def add(data):
    job = _clean(data)
    if not (job.get("company") or "").strip():
        raise TrackerError("company required")
    job.setdefault("status", "Applied")
    job.setdefault("date", date.today().isoformat())
    for k in ("role", "link", "salary", "location"):
        job[k] = job.get(k) or ""
    # Nearly every application is cold, so that is the default;
    # referrals and recruiters get marked by hand.
    job["source"] = job.get("source") or "Cold"
    if not job.get("referral"):
        job.pop("referral", None)
    if job["status"] == REJECTED:
        job["rejected_at"] = _stage(data.get("rejected_at"))
    job["prep"] = {}
    mark_prep(job, None)
    stamp_status(job, None)
    con = db()
    with con:
        job["id"] = _save(con, job)
    con.close()
    return job


def update(app_id, data):
    con = db()
    with con:
        r = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not r:
            raise TrackerError("no such application", 404)
        job = _row(r)
        was = job.get("status")
        job.update(_clean(data))
        # Rejected rows keep the stage they reached; anything back on the
        # board drops it. An explicit rejected_at wins, else the stage it sat at.
        if job.get("status") == REJECTED:
            if data.get("rejected_at"):
                job["rejected_at"] = _stage(data["rejected_at"])
            elif not job.get("rejected_at"):
                job["rejected_at"] = _stage(was)
        else:
            job["rejected_at"] = None
        mark_prep(job, was)
        stamp_status(job, was)
        for k in ("source", "referral", "follow_up"):
            if job.get(k) == "":
                job[k] = None
        _save(con, job)
    con.close()
    return job


# ── Watched postings. A row belongs to a posting when the link is the same
#    posting (applied._job_key), else when company and role match exactly.

def _jobs_db():
    from jobs import watch
    return watch.DB_PATH


def _watched():
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _jobs_db(), uri=True, timeout=30)
    except sqlite3.Error:
        return []
    try:
        return [tuple(r) for r in con.execute("SELECT id, company, title, url, status FROM jobs")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _match(job, watched_by_key, watched_by_cr):
    from applied import _job_key, _norm_company
    k = _job_key(job.get("link"))
    if k and k in watched_by_key:
        return watched_by_key[k]
    return watched_by_cr.get((_norm_company(job.get("company")), (job.get("role") or "").strip().lower()))


def _watched_maps(watched=None):
    from applied import _job_key, _norm_company
    by_key, by_cr = {}, {}
    for jid, company, title, url, _ in (watched if watched is not None else _watched()):
        k = _job_key(url)
        if k:
            by_key.setdefault(k, jid)
        by_cr.setdefault((_norm_company(company), (title or "").strip().lower()), jid)
    return by_key, by_cr


def link_watched(watched=None):
    """Give rows with no posting yet their watched posting. Returns how many."""
    by_key, by_cr = _watched_maps(watched)
    con = db()
    n = 0
    with con:
        for r in con.execute("SELECT * FROM applications WHERE job_id IS NULL").fetchall():
            jid = _match(dict(r), by_key, by_cr)
            if jid:
                con.execute("UPDATE applications SET job_id=? WHERE id=?", (jid, r["id"]))
                n += 1
    con.close()
    return n


def job_statuses():
    """{watched job id: tracker status} for linked rows, furthest along wins."""
    order = {s: i for i, s in enumerate(STATUSES)}
    out = {}
    con = db()
    for jid, st in con.execute("SELECT job_id, status FROM applications WHERE job_id IS NOT NULL"):
        if jid not in out or order.get(st, 0) > order.get(out[jid], 0):
            out[jid] = st
    con.close()
    return out


def find(link="", company="", job_id=None, role=""):
    """The row for this posting: by watched id, else by the same posting
    link, else by the same company and role (the link fallback rows
    need, since many were logged without one)."""
    from applied import _job_key, _norm_company
    k = _job_key(link)
    con = db()
    rows = [_row(r) for r in con.execute("SELECT * FROM applications ORDER BY id")]
    con.close()
    for r in rows:
        if job_id and r.get("job_id") == job_id:
            return r
    for r in rows:
        if k and _job_key(r.get("link")) == k:
            return r
    role = (role or "").strip().lower()
    if role:
        for r in rows:
            if _norm_company(r.get("company")) == _norm_company(company) \
                    and (r.get("role") or "").strip().lower() == role:
                return r
    return None


def track(company, url="", role="", salary="", location="", status="Applied", job_id=None, day=None):
    """Saved or Applied for one posting, at most one row. A Saved row moves on
    to Applied; any later stage is left alone. day=None with Applied means
    today; pass day="" for an application whose date is not known.
    Returns {status, added, id}."""
    found = find(url, company, job_id, role)
    if found:
        if job_id and not found.get("job_id"):
            con = db()
            with con:
                con.execute("UPDATE applications SET job_id=? WHERE id=?", (job_id, found["id"]))
            con.close()
        if status == "Applied" and found.get("status") == SAVED:
            update(found["id"], {"status": "Applied"})
            if day == "" and found.get("date"):
                # Already applied on a day we do not know: the Saved row's
                # date is closer than today.
                update(found["id"], {"date": found["date"], "follow_up": _plus_days(found["date"], 7)})
            return {"status": "Applied", "added": False, "id": found["id"]}
        return {"status": found.get("status") or SAVED, "added": False, "id": found["id"]}
    data = {"company": company, "role": role or "", "link": url or "", "salary": salary or "",
            "location": location or "", "status": status}
    if day is not None:
        data["date"] = day or None
    row = add(data)
    if not job_id:
        by_key, by_cr = _watched_maps()
        job_id = _match(row, by_key, by_cr)
    if job_id:
        con = db()
        with con:
            con.execute("UPDATE applications SET job_id=? WHERE id=?", (job_id, row["id"]))
        con.close()
    return {"status": row["status"], "added": True, "id": row["id"]}


def untrack(job_id):
    """Undo a Mark applied on a watched job. Only a row Verbatim added and
    that never moved past Applied is removed; anything further stays and
    the answer says where it is."""
    con = db()
    rows = [_row(r) for r in con.execute("SELECT * FROM applications WHERE job_id=?", (job_id,))]
    con.close()
    for r in rows:
        if r.get("origin") != "verbatim" or r.get("status") not in ("Applied", SAVED):
            return {"ok": False, "status": r.get("status"),
                    "error": "it is at %s in your applications; change it there" % r.get("status")}
    for r in rows:
        remove(r["id"])
    return {"ok": True, "removed": len(rows)}


def remove(app_id):
    con = db()
    with con:
        n = con.execute("DELETE FROM applications WHERE id=?", (app_id,)).rowcount
    con.close()
    return n


def prep_pending():
    """Prep requests nobody has started yet, oldest first."""
    out = []
    for job in list_all():
        for tier, p in (job.get("prep") or {}).items():
            if job.get("status") in CLOSED and not tier.startswith("debrief"):
                continue
            if not p.get("started"):
                out.append({"tier": tier, "stage": p.get("stage"), "outcome": p.get("outcome", ""),
                            "requested": p.get("requested"),
                            **{k: job.get(k) or "" for k in ("company", "role", "date", "link", "location", "salary")}})
    out.sort(key=lambda p: p["requested"] or "")
    return out


def prep_claim(company, day, tier):
    """An outside prep tool started the session for this job and tier. True when claimed,
    False when it already was, None when there is no such request."""
    con = db()
    try:
        with con:
            for r in con.execute("SELECT * FROM applications WHERE company=? AND date=?", (company, day)).fetchall():
                job = _row(r)
                p = job["prep"].get(tier)
                if p:
                    if p.get("started"):
                        return False
                    p["started"] = _now()
                    con.execute("UPDATE applications SET prep=? WHERE id=?", (json.dumps(job["prep"]), job["id"]))
                    return True
        return None
    finally:
        con.close()


# ── Routes

def _fail(e):
    return jsonify({"error": str(e)}), getattr(e, "status", 400)


@bp.route("/api/applications", methods=["GET"])
def api_list():
    """Every row, newest first. A row linked to a watched posting also gets
    that posting's title and link, so a row logged with no role
    still says what the job was."""
    rows = list_all()
    posted = {jid: (title, url) for jid, _, title, url, _ in _watched()}
    made = _tailored({jid: (url, co, title) for jid, co, title, url, _ in _watched()})
    for r in rows:
        title, url = posted.get(r.get("job_id"), ("", ""))
        r["posting_title"], r["posting_url"] = title or "", url or ""
        r["tailored"] = made(r)
    return jsonify(rows)


def _tailored(watched):
    """A row's pre-tailored resume, so it stays reachable after the user applies.
    By the row's watched posting, else the same posting
    link, else the same company and role."""
    try:
        import pretailor
        jobs = pretailor.tailored_jobs(watched.get, os.path.join(BASE_DIR, "output"))
        jobs += [dict(t, id="") for t in pretailor.made_folders(
            os.path.join(BASE_DIR, "output"), {t["files"]["resume"] for t in jobs})]
    except Exception:
        jobs = []
    key = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())

    def find(r):
        for t in jobs:
            if r.get("job_id") and t["id"] and r["job_id"] == t["id"]:
                return t["files"]
            if r.get("link") and pretailor.norm_url(r["link"]) == pretailor.norm_url(t["url"]):
                return t["files"]
            if (pretailor.company_key(r.get("company")) == pretailor.company_key(t["company"])
                    and key(r.get("role")) and key(r.get("role")) == key(t["role"])):
                return t["files"]
        return None
    return find


@bp.route("/api/applications/stats", methods=["GET"])
def api_stats():
    """Numbers for the Stats page (stats.py)."""
    import stats
    return jsonify(stats.compute(list_all()))


@bp.route("/api/applications/add", methods=["POST"])
def api_add():
    try:
        row = add(request.get_json(silent=True) or {})
    except TrackerError as e:
        return _fail(e)
    # A pasted link that is a watched posting takes it off the job lists.
    link_watched()
    return jsonify({"ok": True, "application": get(row["id"])})


@bp.route("/api/applications/update", methods=["POST"])
def api_update():
    data = request.get_json(silent=True) or {}
    try:
        row = update(int(data.get("id") or 0), data)
    except (TrackerError, ValueError) as e:
        return _fail(e)
    if "link" in data or "company" in data or "role" in data:
        link_watched()
    return jsonify({"ok": True, "application": get(row["id"])})


@bp.route("/api/applications/remove", methods=["POST"])
def api_remove():
    data = request.get_json(silent=True) or {}
    try:
        n = remove(int(data.get("id") or 0))
    except ValueError as e:
        return _fail(e)
    return (jsonify({"ok": True}) if n else (jsonify({"error": "no such application"}), 404))


@bp.route("/api/applications/prefill", methods=["POST"])
def api_prefill():
    import prefill
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return jsonify({"error": "bad url"}), 400
    return jsonify(prefill.prefill(url))


@bp.route("/api/jobs/prep", methods=["GET"])
def api_prep():
    return jsonify(prep_pending())


@bp.route("/api/jobs/prep/claim", methods=["POST"])
def api_prep_claim():
    data = request.get_json(silent=True) or {}
    ok = prep_claim(data.get("company"), data.get("date"), data.get("tier"))
    if ok is None:
        return jsonify({"ok": False, "error": "No such request"}), 404
    return jsonify({"ok": True} if ok else {"ok": False, "error": "Already started"})
