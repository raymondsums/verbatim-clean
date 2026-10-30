"""The hourly job watch.

Fetches every mapped board, keeps the roles and places set in
jobs/config.json (copy jobs/config.example.json), sorts each into a fit
tier (jobs/fit.py), and tells the user about new
Strong matches.

    python3 -m jobs.watch           one run (what the LaunchAgent does)
    python3 -m jobs.watch --list    print the best open jobs
    python3 -m jobs.watch --quiet   one run, no notifications

The first time a board is fetched, everything on it is saved as baseline:
status "seen", baseline 1. Those are real open jobs and stay listed, but
they are not news. Only jobs that appear on a later run count as new.

Each open job is scored once (background fit through the Claude CLI) and
the result is stored. Runs never overlap: a second run while one is still
scoring exits at once.

Functions for the server: list_jobs(), set_status(), mark(), stats(),
job_text(), prefs(), save_prefs().
"""

import fcntl
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from jobs import bigco
from jobs import discover
from jobs import enterprise
from jobs import feeds
from jobs import boards as boards_mod
from jobs import fit
from jobs import track_record
from jobs import hn
from jobs import yc
from jobs.common import (DATA_DIR, DB_PATH, JOBS_DIR, UA, is_contract, is_defense,
                         is_wanted_title, load_config, seniority)

STATUSES = ("new", "seen", "dismissed", "applied")
MARKS = ("opened", "starred", "tailored")
WORKERS = 4          # modest on purpose: this runs on your own computer
TIMEOUT = 20
TEXT_CHARS = 12000
NEW_DAYS = 14        # a job stays under New this long after it appeared
LOCK_PATH = os.path.join(DATA_DIR, "watch.lock")

_local = threading.local()


def session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers["User-Agent"] = UA
        _local.s = s
    return _local.s


# What a missing key in config.json means.
DEFAULTS = {
    "roles": [],
    "exclude": [],
    "cities": [],
    "allow_remote": True,
    "remote_country": "United States",
    "notify_mac": True,
    "skip_defense": False,
}


def config():
    raw = load_config()
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    # Older configs called it allow_us_remote.
    if "allow_us_remote" in raw and "allow_remote" not in raw:
        cfg["allow_remote"] = raw["allow_us_remote"]
    return cfg


# ── Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,          -- ats:slug:posting id
    ats TEXT, slug TEXT,
    employer TEXT,                -- normalized company key
    company TEXT, title TEXT, url TEXT, location TEXT,
    seniority TEXT,
    posted TEXT,                  -- from the board, when it gives one
    updated TEXT,
    first_seen TEXT, last_seen TEXT,
    closed TEXT,                  -- set when it drops off the board
    status TEXT DEFAULT 'new',
    baseline INTEGER DEFAULT 0,
    text TEXT,                    -- posting text, plain, for scoring and tailoring
    opened TEXT, starred INTEGER DEFAULT 0, tailored TEXT,
    tier TEXT,                    -- background fit, stored once: strong, look, stretch
    reason TEXT, gaps TEXT, about TEXT, tags TEXT,
    scored TEXT, score_tries INTEGER DEFAULT 0, score_error TEXT
);
CREATE TABLE IF NOT EXISTS boards (
    key TEXT PRIMARY KEY,         -- ats:slug
    first_polled TEXT, last_ok TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    started TEXT, seconds REAL, boards INTEGER, failed INTEGER,
    matched INTEGER, new INTEGER, scored INTEGER DEFAULT 0,
    score_seconds REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, closed);
"""


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:  # added after the first runs: 0 to 100 inside the tier's band
        con.execute("ALTER TABLE jobs ADD COLUMN score INTEGER")
    except sqlite3.OperationalError:
        pass
    try:  # when the user added the job themselves; the title and place filters skip it
        con.execute("ALTER TABLE jobs ADD COLUMN added TEXT")
    except sqlite3.OperationalError:
        pass
    try:  # runs in a row a careers site came back empty or failing
        con.execute("ALTER TABLE boards ADD COLUMN quiet INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:  # the board's employment type, where it gives one (for is_contract)
        con.execute("ALTER TABLE jobs ADD COLUMN emptype TEXT")
    except sqlite3.OperationalError:
        pass
    return con


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _plain(raw):
    t = html.unescape(raw or "")
    t = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


# ── Location
#
# A job counts when its location names one of "cities", or when it is
# remote and "allow_remote" is on and it can be done from "remote_country".
# For the United States the checks below read US wording ("US", "USA",
# "North America") and skip postings that name only somewhere else. For any
# other country the location or title must name that country, or say
# anywhere or worldwide, or just say remote.

_US_WORDS = re.compile(r"\b(us|u\.s\.|usa|united states|north america|americas|anywhere)\b", re.I)
_US_NAMED = re.compile(r"\b(us|u\.s\.|usa|united states|north america)\b", re.I)
_NON_US = re.compile(
    r"\b(canada|uk|united kingdom|london|ireland|dublin|germany|berlin|france|paris|"
    r"spain|portugal|netherlands|amsterdam|poland|india|bangalore|bengaluru|"
    r"singapore|japan|tokyo|australia|sydney|brazil|mexico|argentina|colombia|"
    r"emea|apac|latam|south america|europe|israel|tel aviv|korea|seoul|china|toronto|vancouver)\b", re.I)
_ANYWHERE = re.compile(r"\b(anywhere|worldwide|global|globally)\b", re.I)


def _is_us(country):
    return re.fullmatch(r"\s*(us|usa|u\.s\.?|united states( of america)?)\s*",
                        country or "", re.I) is not None


def location_ok(loc, remote_flag, cfg, title=""):
    l = loc or ""
    for city in cfg.get("cities") or []:
        if re.search(r"\b%s\b" % re.escape(city.lower()), l.lower()):
            return True
    # "Remote" or "Remote Eligible" in the title counts as remote; where
    # it is remote from is read off the title as well as the location.
    t = title or ""
    remote = remote_flag or bool(re.search(r"\bremote\b", l + " " + t, re.I))
    allow = cfg.get("allow_remote", cfg.get("allow_us_remote", True))
    country = cfg.get("remote_country") or "United States"
    if remote and allow and _is_us(country):
        # A title that names only somewhere else ("anywhere in South America")
        # outranks a loose "Anywhere" in the location.
        elsewhere = _NON_US.search(t) and not _US_NAMED.search(t)
        if not elsewhere:
            if _US_WORDS.search(l):
                return True
            if not _NON_US.search(l + " " + t):
                return True
    elif remote and allow:
        both = l + " " + t
        if re.search(r"\b%s\b" % re.escape(country.lower()), both.lower()) or _ANYWHERE.search(both):
            return True
        if not re.sub(r"\bremote\b|[^a-z]", "", l.lower()):
            return True  # just "Remote", nowhere named
    # "US / Canada" is a US posting that also takes Canada.
    if cfg.get("count_bare_united_states") and re.fullmatch(
            r"\s*(us|usa|u\.s\.|united states( of america)?)(\s*(/|&|,|or|and)\s*canada)?\s*",
            l, re.I):
        return True
    return False


# ── Fetching. Each returns plain dicts:
#    {pid, title, url, location, remote, posted, updated, text}
#    text may be None when the board list does not carry descriptions;
#    those are fetched one by one, only for new matching jobs.

def _get(url):
    r = session().get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(slug):
    data = _get("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug)
    out = []
    for j in data.get("jobs") or []:
        out.append({"pid": str(j["id"]), "title": j.get("title") or "",
                    "url": j.get("absolute_url") or "",
                    "location": (j.get("location") or {}).get("name") or "",
                    "remote": False,
                    "posted": (j.get("first_published") or "")[:19],
                    "updated": (j.get("updated_at") or "")[:19],
                    "text": None})
    return out


def greenhouse_text(slug, pid):
    j = _get("https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s" % (slug, pid))
    return j.get("content") or ""


def fetch_lever(slug):
    data = _get("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        ts = j.get("createdAt")
        posted = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts / 1000)) if ts else ""
        text = " ".join([j.get("descriptionPlain") or "", j.get("additionalPlain") or ""] +
                        [" ".join(str(x.get("content") or "") for x in j.get("lists") or [])])
        out.append({"pid": j["id"], "title": j.get("text") or "",
                    "url": j.get("hostedUrl") or "",
                    "location": " / ".join(l for l in locs if l),
                    "remote": (j.get("workplaceType") or "") == "remote",
                    "posted": posted, "updated": "", "text": text,
                    "emptype": cats.get("commitment") or ""})
    return out


def fetch_ashby(slug):
    data = _get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    out = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location") or "" for s in
                                            j.get("secondaryLocations") or []]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
        loc = " / ".join(l for l in locs if l)
        if country and country.lower() not in loc.lower():
            loc += " (%s)" % country
        out.append({"pid": j["id"], "title": j.get("title") or "",
                    "url": j.get("jobUrl") or "",
                    "location": loc,
                    # isRemote is true for hybrid roles too; workplaceType
                    # says which, so it wins when the board fills it in.
                    "remote": ((j.get("workplaceType") or "").lower() == "remote"
                               if j.get("workplaceType") else bool(j.get("isRemote"))),
                    "posted": (j.get("publishedAt") or "")[:19],
                    "updated": "", "text": j.get("descriptionPlain") or "",
                    "emptype": j.get("employmentType") or ""})
    return out


# Workday has a keyless JSON feed too, under each careers site. Its slug is
# "tenant|wdN|site", the three parts of tenant.wdN.myworkdayjobs.com/site.
# The list is searched for "design" so a big company's thousands of jobs
# are not paged through. Lists carry no description, and "3 Locations"
# instead of the places, so both come from the posting itself.
_WD_DAYS = re.compile(r"(\d+)\+? days? ago", re.I)


def _workday_base(slug):
    tenant, wd, site = slug.split("|")
    return "https://%s.%s.myworkdayjobs.com" % (tenant, wd), tenant, site


def _workday_posted(text):
    t = (text or "").lower()
    days = 0 if "today" in t else 1 if "yesterday" in t else None
    m = _WD_DAYS.search(t)
    if m:
        days = int(m.group(1))
    if days is None:
        return ""
    return time.strftime("%Y-%m-%dT00:00:00", time.gmtime(time.time() - days * 86400))


def _workday_job(slug, path):
    host, tenant, site = _workday_base(slug)
    return _get("%s/wday/cxs/%s/%s%s" % (host, tenant, site, path)).get("jobPostingInfo") or {}


def _workday_search():
    """Workday's search box takes one phrase: the first role in config.json,
    else "design" for the example design search."""
    roles = [r for r in config().get("roles") or [] if isinstance(r, str) and r.strip()]
    return roles[0] if roles else "design"


def fetch_workday(slug):
    host, tenant, site = _workday_base(slug)
    # Only the first page carries the total; later pages say 0.
    out, offset, total = [], 0, None
    while offset < 200:
        r = session().post("%s/wday/cxs/%s/%s/jobs" % (host, tenant, site),
                           json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                 "searchText": _workday_search()}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        page = data.get("jobPostings") or []
        for j in page:
            path = j.get("externalPath") or ""
            if not path:
                continue
            loc = j.get("locationsText") or ""
            remote = "remote" in loc.lower()
            if re.match(r"\d+ locations", loc, re.I) and is_wanted_title(j.get("title") or ""):
                try:
                    info = _workday_job(slug, path)
                    locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
                    loc = " / ".join(l for l in locs if l)
                    remote = remote or "remote" in (info.get("remoteType") or "").lower()
                except Exception:
                    pass
            out.append({"pid": path, "title": j.get("title") or "",
                        "url": "%s/%s%s" % (host, site, path), "location": loc,
                        "remote": remote, "posted": _workday_posted(j.get("postedOn")),
                        "updated": "", "text": None})
        if total is None:
            total = data.get("total") or 0
        offset += 20
        if len(page) < 20 or offset >= total:
            break
    return out


def workday_text(slug, pid):
    return _workday_job(slug, pid).get("jobDescription") or ""


def fetch_workable(slug):
    data = _get("https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % slug)
    out = []
    for j in data.get("jobs") or []:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append({"pid": j.get("shortcode") or "", "title": j.get("title") or "",
                    "url": j.get("url") or "", "location": loc,
                    "remote": bool(j.get("telecommuting")),
                    "posted": ((j.get("published_on") or "") + "T00:00:00")[:19]
                    if j.get("published_on") else "",
                    "updated": "", "text": j.get("description") or "",
                    "emptype": j.get("employment_type") or ""})
    return out


# SmartRecruiters answers every company name, real or not, so the slug is
# the company identifier from jobs.smartrecruiters.com/<Identifier>. Big
# companies list thousands of jobs; only US ones are asked for (location_ok
# wants the US anyway), at most 500.
def fetch_smartrecruiters(slug):
    out, offset = [], 0
    while offset < 500:
        data = _get("https://api.smartrecruiters.com/v1/companies/%s/postings"
                    "?country=us&limit=100&offset=%d" % (slug, offset))
        page = data.get("content") or []
        for j in page:
            loc = j.get("location") or {}
            out.append({"pid": str(j.get("id") or ""), "title": j.get("name") or "",
                        "url": "https://jobs.smartrecruiters.com/%s/%s" % (slug, j.get("id")),
                        "location": loc.get("fullLocation") or "",
                        "remote": bool(loc.get("remote")),
                        "posted": (j.get("releasedDate") or "")[:19],
                        "updated": "", "text": None,
                        "emptype": (j.get("typeOfEmployment") or {}).get("label") or ""})
        offset += 100
        if len(page) < 100 or offset >= (data.get("totalFound") or 0):
            break
    return out


def smartrecruiters_text(slug, pid):
    sec = ((_get("https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (slug, pid))
            .get("jobAd") or {}).get("sections") or {})
    return "\n\n".join((sec.get(k) or {}).get("text") or "" for k in
                       ("companyDescription", "jobDescription", "qualifications",
                        "additionalInformation"))


# Rippling's list has no dates or descriptions; the description comes from
# the posting itself, for matching jobs only.
def fetch_rippling(slug):
    data = _get("https://api.rippling.com/platform/api/ats/v1/board/%s/jobs" % slug)
    out = []
    for j in data if isinstance(data, list) else []:
        loc = (j.get("workLocation") or {}).get("label") or ""
        out.append({"pid": j.get("uuid") or "", "title": j.get("name") or "",
                    "url": j.get("url") or "", "location": loc,
                    "remote": "remote" in loc.lower(),
                    "posted": "", "updated": "", "text": None})
    return out


def rippling_text(slug, pid):
    d = (_get("https://api.rippling.com/platform/api/ats/v1/board/%s/jobs/%s" % (slug, pid))
         .get("description") or {})
    return "\n\n".join(d.get(k) or "" for k in ("company", "role"))


def fetch_gem(slug):
    data = _get("https://api.gem.com/job_board/v0/%s/job_posts/" % slug)
    out = []
    for j in data if isinstance(data, list) else []:
        out.append({"pid": str(j.get("id") or ""), "title": j.get("title") or "",
                    "url": j.get("absolute_url") or "",
                    "location": (j.get("location") or {}).get("name") or "",
                    "remote": (j.get("location_type") or "") == "remote",
                    "posted": (j.get("first_published_at") or "")[:19],
                    "updated": (j.get("updated_at") or "")[:19],
                    "text": j.get("content_plain") or "",
                    "emptype": j.get("employment_type") or ""})
    return out


FETCH = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
         "workday": fetch_workday, "workable": fetch_workable,
         "smartrecruiters": fetch_smartrecruiters, "rippling": fetch_rippling,
         "gem": fetch_gem,
         "careers": lambda slug: bigco.FETCH[slug](),
         "yc": yc.fetch, "hn": hn.fetch,
         "dribbble": feeds.fetch_dribbble, "himalayas": feeds.fetch_himalayas,
         "serpapi": feeds.fetch_serpapi, "wwr": feeds.fetch_wwr, **enterprise.FETCH}
# Boards whose lists carry no description; it is fetched per matching job.
TEXT_LATER = {"greenhouse": greenhouse_text, "workday": workday_text,
              "smartrecruiters": smartrecruiters_text, "rippling": rippling_text,
              "careers": lambda slug, pid: bigco.TEXT[slug](pid) if slug in bigco.TEXT else "",
              "yc": yc.text, **enterprise.TEXT}
# A careers site that comes back empty or failing this many runs in a row
# is shown as quiet on the Jobs page: its search has probably changed.
QUIET_RUNS = 3


def _fetch_board(b):
    try:
        return b, FETCH[b["ats"]](b["slug"]), None
    except Exception as e:
        return b, None, "%s: %s" % (type(e).__name__, str(e)[:200])


# A board that answers 429 (too many requests) is left alone for a while:
# its Retry-After if it gives one, else 6 hours, doubling each time it
# happens again, up to a day. It is never retried inside a run.
COOLDOWN_PATH = os.path.join(DATA_DIR, "board_cooldown.json")
COOL_FIRST, COOL_MAX = 6 * 3600, 24 * 3600


def _cooldowns():
    try:
        with open(COOLDOWN_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cool_after(cool, key, err, retry_after=None):
    """Update one board's cooldown from its result. Returns True when changed."""
    if err and "429" in err:
        prev = cool.get(key, {}).get("wait", 0)
        wait = int(retry_after) if retry_after else min(COOL_MAX, prev * 2 if prev else COOL_FIRST)
        cool[key] = {"until": time.time() + wait, "wait": wait}
        return True
    if not err and key in cool:
        del cool[key]
        return True
    return False


# ── One run

def run(notify=True, score=True, log=print):
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("%s  another run is still going, skipped" % _now())
        return None
    try:
        return _run(notify, score, log)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _run(notify, score, log):
    t0 = time.time()
    cfg = config()
    boards = boards_mod.all_boards()
    if cfg.get("careers_sites"):
        boards += bigco.boards()
    if cfg.get("yc_jobs"):
        boards += yc.boards()
    if cfg.get("hn_jobs"):
        boards += hn.boards()
    boards += feeds.boards(cfg)
    if cfg.get("company_sites"):
        boards += enterprise.boards()
    polled = {(x["ats"], x["slug"].lower()) for x in boards}
    con = db()
    now = _now()
    matched, new_jobs, failed = 0, [], 0

    cool = _cooldowns()
    resting = [b for b in boards if cool.get("%s:%s" % (b["ats"], b["slug"]), {}).get("until", 0) > time.time()]
    if resting:
        log("  %d board(s) resting after a 429: %s" % (len(resting), ", ".join(
            "%s:%s" % (b["ats"], b["slug"]) for b in resting)))
    boards = [b for b in boards if b not in resting]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(_fetch_board, boards))
    changed = False
    for b, _, err in results:
        changed |= _cool_after(cool, "%s:%s" % (b["ats"], b["slug"]), err)
    if changed:
        with open(COOLDOWN_PATH + ".tmp", "w") as f:
            json.dump(cool, f)
        os.replace(COOLDOWN_PATH + ".tmp", COOLDOWN_PATH)

    text_jobs = []  # (row id, ats, slug, pid) needing a description fetch
    for b, postings, err in results:
        bkey = "%s:%s" % (b["ats"], b["slug"])
        known = con.execute("SELECT first_polled FROM boards WHERE key=?", (bkey,)).fetchone()
        if b["ats"] == "careers":
            con.execute("INSERT INTO boards(key, quiet) VALUES(?, 0) ON CONFLICT(key) DO NOTHING",
                        (bkey,))
            con.execute("UPDATE boards SET quiet=%s WHERE key=?"
                        % ("quiet+1" if err or not postings else "0"), (bkey,))
        if err:
            failed += 1
            con.execute("INSERT INTO boards(key, last_error) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET last_error=excluded.last_error",
                        (bkey, err))
            continue
        first_time = known is None or not known["first_polled"]
        con.execute("INSERT INTO boards(key, first_polled, last_ok, last_error) VALUES(?,?,?,'') "
                    "ON CONFLICT(key) DO UPDATE SET last_ok=excluded.last_ok, last_error=''",
                    (bkey, now, now))
        if first_time:
            con.execute("UPDATE boards SET first_polled=? WHERE key=?", (now, bkey))

        company = _pretty(b.get("board_name") or b.get("name") or b["employer"])
        live = set()
        by_hand = {r["id"] for r in con.execute(
            "SELECT id FROM jobs WHERE ats=? AND slug=? AND added IS NOT NULL",
            (b["ats"], b["slug"]))}
        for p in postings:
            jid = "%s:%s" % (bkey, p["pid"])
            if jid not in by_hand and (not is_wanted_title(p["title"], cfg) or
                                       not location_ok(p["location"], p["remote"], cfg, p["title"])):
                continue
            row = con.execute("SELECT id, text FROM jobs WHERE id=?", (jid,)).fetchone()
            if b["ats"] in feeds.DUPES and jid not in by_hand and _dupe(con, b["ats"], p, polled):
                if row:  # its company's own board has it now
                    con.execute("UPDATE jobs SET closed=? WHERE id=? AND closed IS NULL", (now, jid))
                continue
            live.add(jid)
            matched += 1
            if row:
                con.execute("UPDATE jobs SET last_seen=?, closed=NULL, title=?, location=?, "
                            "updated=?, emptype=? WHERE id=?",
                            (now, p["title"], p["location"], p["updated"],
                             p.get("emptype") or "", jid))
                if not row["text"] and b["ats"] in TEXT_LATER:
                    text_jobs.append((jid, b["ats"], b["slug"], p["pid"]))
                continue
            text = _plain(p["text"])[:TEXT_CHARS] if p["text"] else None
            con.execute(
                "INSERT INTO jobs(id, ats, slug, employer, company, title, url, location, "
                "seniority, posted, updated, first_seen, last_seen, status, baseline, text, "
                "emptype) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                # A list of many companies (YC, HN) names each posting's own.
                (jid, b["ats"], b["slug"],
                 boards_mod.normalize(p["company"]) if p.get("company") else b["employer"],
                 _pretty(p["company"]) if p.get("company") else company, p["title"], p["url"],
                 p["location"], seniority(p["title"]), p["posted"], p["updated"], now, now,
                 "seen" if first_time else "new", 1 if first_time else 0, text,
                 p.get("emptype") or ""))
            if text is None and b["ats"] in TEXT_LATER:
                text_jobs.append((jid, b["ats"], b["slug"], p["pid"]))
            if not first_time:
                new_jobs.append(jid)
        # Anything on this board we had as open and did not see now is gone.
        open_ids = [r["id"] for r in con.execute(
            "SELECT id FROM jobs WHERE ats=? AND slug=? AND closed IS NULL",
            (b["ats"], b["slug"]))]
        stale = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(time.time() - yc.STALE_DAYS * 86400))
        for jid in open_ids:
            if jid in live:
                continue
            if b["ats"] == "yc" or b["ats"] in feeds.SAMPLED:  # a sampled list: gone only once unseen for days
                con.execute("UPDATE jobs SET closed=? WHERE id=? AND last_seen < ?",
                            (now, jid, stale))
            else:
                con.execute("UPDATE jobs SET closed=? WHERE id=?", (now, jid))
    con.commit()

    # Greenhouse, Workday, SmartRecruiters and Rippling lists carry no description. Fetch them only for
    # matching jobs that do not have one yet: a handful of requests per run.
    def _text(item):
        jid, ats, slug, pid = item
        try:
            return jid, TEXT_LATER[ats](slug, pid)
        except Exception:
            return jid, None
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for jid, text in pool.map(_text, text_jobs):
            if text:
                con.execute("UPDATE jobs SET text=? WHERE id=?", (_plain(text)[:TEXT_CHARS], jid))
    con.commit()
    poll_secs = round(time.time() - t0, 1)

    scored, score_secs = 0, 0.0
    if score:
        t1 = time.time()
        scored = score_pending(con, log=log)
        score_backfill(con, log=log, limit=48)
        score_secs = round(time.time() - t1, 1)

    # What got the user calls, from the applications tracker. Rebuilt at
    # most once a day.
    track_record.refresh(log=log)

    secs = round(time.time() - t0, 1)
    con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                (now, secs, len(boards), failed, matched, len(new_jobs), scored, score_secs))
    con.commit()
    strong = []
    if new_jobs and tracker_live():
        # A posting the user already logged (pasted in by hand, or applied to
        # before it showed up here) is linked now, and never alerts.
        import tracker
        tracker.link_watched()
    tracked = _tracked()
    if new_jobs:
        prefs = fit.load_prefs()
        learned = fit.learn(_acted(con))
        record = track_record.load()
        for r in con.execute("SELECT * FROM jobs WHERE id IN (%s)" %
                             ",".join("?" * len(new_jobs)), new_jobs):
            j = dict(r)
            if j["id"] in tracked:
                continue
            if is_defense(j["company"], j["title"], j.get("text"), j.get("about")) or \
                    is_contract(j["title"], j.get("text"), j.get("emptype")):
                continue
            if fit.adjust(j, prefs, learned, record)[0] == "strong":
                strong.append(j)
    con.close()
    if notify and strong:
        tell(strong, cfg)
    # Board discovery, about once a month, in its own process so the run
    # is not held up (jobs/discover.py logs to logs/discover.log).
    if cfg.get("discover_boards") and discover.due():
        subprocess.Popen([sys.executable, "-W", "ignore", "-m", "jobs.discover"],
                         cwd=os.path.dirname(JOBS_DIR), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("%s  %d boards (%d failed), %d matching open, %d new, %d new strong, "
        "poll %.1fs, scored %d in %.1fs"
        % (now, len(boards), failed, matched, len(new_jobs), len(strong),
           poll_secs, scored, score_secs))
    return {"boards": len(boards), "failed": failed, "matched": matched,
            "new": len(new_jobs), "strong": len(strong), "poll_seconds": poll_secs,
            "scored": scored, "score_seconds": score_secs, "seconds": secs}


def score_pending(con, batch=fit.BATCH, log=print, limit=None):
    """Background fit for open jobs that have none yet. Each job is scored
    once; a job the model fails on twice is left without a tier."""
    rows = [dict(r) for r in con.execute(
        "SELECT id, title, company, location, text FROM jobs "
        "WHERE closed IS NULL AND tier IS NULL AND status != 'dismissed' "
        "AND score_tries < 2 ORDER BY baseline ASC, first_seen DESC")]
    if limit:
        rows = rows[:limit]
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        t = time.time()
        try:
            got = fit.score_batch(chunk)
            err = ""
        except fit.FitError as e:
            got, err = {}, str(e)[:200]
        for j in chunk:
            r = got.get(j["id"])
            if r:
                con.execute("UPDATE jobs SET tier=?, score=?, reason=?, gaps=?, about=?, "
                            "tags=?, scored=?, score_error='' WHERE id=? AND tier IS NULL",
                            (r["tier"], r["score"], r["reason"], json.dumps(r["gaps"]), r["about"],
                             json.dumps(r["tags"]), _now(), j["id"]))
                done += 1
            else:
                con.execute("UPDATE jobs SET score_tries=score_tries+1, score_error=? "
                            "WHERE id=?", (err or "missing from the reply", j["id"]))
        con.commit()
        log("  scored %d/%d in %.0fs%s" % (min(i + batch, len(rows)), len(rows),
                                           time.time() - t, ("  (%s)" % err) if err else ""))
    return done


def score_backfill(con, batch=fit.BATCH * 2, log=print, limit=None):
    """A score for open jobs that got their tier before scores existed.
    The tier stays as it is; only the number inside it is asked for."""
    rows = [dict(r) for r in con.execute(
        "SELECT id, title, company, location, text, tier, reason FROM jobs "
        "WHERE closed IS NULL AND tier IS NOT NULL AND score IS NULL "
        "AND status != 'dismissed' ORDER BY first_seen DESC")]
    if limit:
        rows = rows[:limit]
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        try:
            got = fit.score_only(chunk)
        except fit.FitError as e:
            log("  scores %d/%d failed: %s" % (min(i + batch, len(rows)), len(rows), e))
            continue
        for jid, s in got.items():
            con.execute("UPDATE jobs SET score=? WHERE id=? AND score IS NULL", (s, jid))
            done += 1
        con.commit()
        log("  scores %d/%d" % (min(i + batch, len(rows)), len(rows)))
    return done


def _title_key(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _dupe(con, ats, p, polled):
    """A job from a many-company list that we already have from somewhere
    else: an open job at the same company with the same title, or a link
    to a board the poller reads anyway (it would be there if it matched)."""
    for link in [p.get("url")] + list(p.get("links") or []):
        found = boards_mod.board_from_url(link or "")
        if found and (found[0], found[1].lower()) in polled:
            return True
    key = _title_key(p["title"])
    for r in con.execute("SELECT company, title FROM jobs WHERE closed IS NULL AND ats != ?", (ats,)):
        if _title_key(r["title"]) == key and boards_mod.names_match(r["company"] or "", p.get("company") or ""):
            return True
    return False


def _pretty(name):
    """DOL names are all caps; board names are not. Title-case only caps,
    and drop the "Job Board" or "Careers" some boards add to their name."""
    name = re.sub(r"\s+(job board|jobs|careers|career site)\s*$", "", name.strip(), flags=re.I)
    return name.title() if name.isupper() and len(name) > 4 else name


# ── Notifications (new Strong matches only)

def tell(jobs, cfg):
    top = jobs[0]
    if len(jobs) == 1:
        title = "New strong match"
        body = "%s: %s" % (top["company"], top["title"])
    else:
        title = "%d new strong matches" % len(jobs)
        body = "; ".join("%s: %s" % (j["company"], j["title"]) for j in jobs[:3])
    if cfg.get("notify_mac", True):
        mac_notify(title, body)


def mac_notify(title, body):
    """A macOS notification. Does nothing on other systems."""
    if sys.platform != "darwin":
        return
    try:
        script = 'display notification %s with title %s sound name "Glass"' % (
            _as_str(body), _as_str(title))
        subprocess.run(["osascript", "-e", script], timeout=10, check=False)
    except Exception as e:
        print("mac notification failed: %s" % e, flush=True)


def _as_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def three_a_week():
    """Mondays: a notification with the week's three answers to sharpen
    (weekly.py), once. Rides on the watcher's first Monday run."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        import weekly
        cfg = config()

        def send(title, body, url):
            if cfg.get("notify_mac", True):
                mac_notify(title, body)
        if weekly.monday_push(send, os.environ.get("PUBLIC_BASE") or ""):
            print("three a week: pushed", flush=True)
    except Exception as e:
        print("three a week failed: %s" % e, flush=True)


# ── For the server

TIER_ORDER = {"strong": 0, "look": 1, "stretch": 2, None: 3}


def _tracked():
    """{watched job id: tracker status past Saved}, empty until it is live."""
    if not tracker_live():
        return {}
    import tracker
    return {k: v for k, v in tracker.job_statuses().items() if v != "Saved"}


def _acted(con):
    tracked = _tracked()
    rows = [dict(r) for r in con.execute(
        "SELECT id, status, opened, starred, tailored, tags FROM jobs "
        "WHERE status IN ('applied','dismissed') OR opened IS NOT NULL "
        "OR starred=1 OR tailored IS NOT NULL" +
        (" OR id IN (%s)" % ",".join("?" * len(tracked)) if tracked else ""), list(tracked))]
    for r in rows:
        if r.pop("id") in tracked:
            r["status"] = "applied"
    return rows


def _age(posted, first_seen):
    """'today', '3d', '5w' from the board date, else when we first saw it."""
    for s in (posted, first_seen):
        if not s:
            continue
        try:
            t = time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        days = int((time.time() - t) // 86400)
        if days < 1:
            return "today"
        if days < 14:
            return "%dd" % days
        if days < 60:
            return "%dw" % (days // 7)
        return "%dmo" % (days // 30)
    return ""


# A pay range as the posting writes it: "$150,000 - $200,000", "$180K-$220K",
# "$148,000 to $166,500". Postings with several ranges list them by location
# tier, highest paid city first; the first one is the one shown.
_PAY = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})*(?:\.\d+)?)\s?([kK])?\s*(?:USD\s*)?(?:-|\u2013|\u2014|to)\s*"
    r"\$?\s?(\d{2,3}(?:,\d{3})*(?:\.\d+)?)\s?([kK])?")


def salary(text):
    """The posting's pay range, tidied a little, or "" when it lists none.
    A yearly range wins over an hourly one in the same posting."""
    hourly = ""
    for m in _PAY.finditer(text or ""):
        lo, lk, hi, hk = m.groups()
        num = lambda x, k: float(x.replace(",", "")) * (1000 if k else 1)
        a, b = num(lo, lk), num(hi, hk)
        if b < a:
            continue  # "$130,000-$188" and other cut-off ranges
        if b < 1000:
            hourly = hourly or "$%s - $%s/hr" % (lo, hi)
            continue
        if a < 20000:
            continue
        fmt = lambda v: "$%sK" % ("%.1f" % (v / 1000)).rstrip("0").rstrip(".")
        return fmt(a) if a == b else "%s - %s" % (fmt(a), fmt(b))
    return hourly


def _days_old(posted, first_seen):
    """Days since the board date, else since the watcher first saw it."""
    for s in (posted, first_seen):
        if not s:
            continue
        try:
            t = time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        return max(0.0, (time.time() - t) / 86400)
    return None


def _view(j, prefs, learned, cutoff, record=None):
    tier, note = fit.adjust(j, prefs, learned, record)
    score = fit.moved_score(j.get("score"), j.get("tier"), tier)
    score, tier, older = fit.freshen(score, tier, _days_old(j.get("posted"), j.get("first_seen")))
    try:
        gaps = json.loads(j.get("gaps") or "[]")
    except ValueError:
        gaps = []
    return {
        "id": j["id"], "company": j["company"], "title": j["title"],
        "location": j["location"], "url": j["url"], "seniority": j["seniority"],
        "age": _age(j.get("posted"), j.get("first_seen")),
        "posted": j.get("posted") or "", "first_seen": j.get("first_seen") or "",
        "new": not j["baseline"] and (j.get("first_seen") or "") >= cutoff,
        "status": j["status"], "starred": bool(j.get("starred")),
        "tailored": bool(j.get("tailored")), "opened": bool(j.get("opened")),
        "tier": tier, "tier_label": fit.LABELS.get(tier, "Not sorted yet"),
        "score": score, "older": bool(older),
        "base_tier": j.get("tier"), "reason": j.get("reason") or "",
        "note": " ".join(x for x in (note, older) if x), "gaps": gaps, "about": j.get("about") or "",
        "salary": salary(j.get("text")),
        "added": bool(j.get("added")),
        "source": feeds.NAMES.get(j.get("ats"), ""),
    }


# The morning shortlist:
# Strong and Worth a look jobs that are new in the last day, best first,
# topped up with earlier unapplied Strong matches so there are a few to
# pick from. Never Stretch. Read only: nothing is marked, applied or sent.
SHORT_MIN, SHORT_MAX, SHORT_HOURS = 3, 5, 24
QUESTION_ATS = {"greenhouse", "lever", "ashby"}   # questions readable ahead


def shortlist(n=SHORT_MAX, hours=SHORT_HOURS):
    n = max(1, min(int(n), 10))
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - hours * 3600))
    jobs = [j for j in list_jobs("new") if j["tier"] in ("strong", "look")]
    fresh = [j for j in jobs if j["first_seen"] >= since]
    earlier = [j for j in jobs if j["first_seen"] < since and j["tier"] == "strong"]
    picked = fresh[:n] + earlier[:max(0, SHORT_MIN - len(fresh))]
    con = db()
    raw = {r["id"]: r for r in con.execute(
        "SELECT id, ats, text FROM jobs WHERE id IN (%s)" % ",".join("?" * len(picked)),
        [j["id"] for j in picked])} if picked else {}
    con.close()
    out = []
    for j in picked:
        r = raw.get(j["id"]) or {}
        out.append({
            "company": j["company"], "role": j["title"], "location": j["location"],
            "tier": j["tier"], "tier_label": j["tier_label"], "score": j["score"],
            "link": j["url"], "reason": j["reason"], "first_seen": j["first_seen"],
            "earlier": j["first_seen"] < since,
            "tailored": j["tailored"],
            # Tailoring needs the job description; this says it is stored.
            "can_tailor": len((r["text"] if r else "") or "") >= 400,
            "has_questions": (r["ats"] if r else "") in QUESTION_ATS,
            "warm_path": None,   # no contact data yet
            "id": j["id"],
        })
    return {"since": since, "jobs": out}


def tracker_live():
    try:
        import tracker
        return tracker.live()
    except Exception:
        return False


def list_jobs(view="all", limit=300):
    """Open jobs for the page, best first.

    view: "new" (appeared after baseline, last NEW_DAYS days), "all" (every
    open job not dismissed or applied), "applied", or "dismissed".
    """
    con = db()
    prefs = fit.load_prefs()
    learned = fit.learn(_acted(con))
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - NEW_DAYS * 86400))
    # Once the tracker is live it says which watched jobs the user applied to
    # (anything past Saved); the jobs table's own 'applied' is retired.
    tracked = _tracked()
    if view == "applied" and tracked:
        sql = "SELECT * FROM jobs WHERE id IN (%s)" % ",".join("?" * len(tracked))
        args = list(tracked)
    elif view == "applied":
        sql, args = "SELECT * FROM jobs WHERE status='applied'", []
    elif view == "dismissed":
        sql, args = "SELECT * FROM jobs WHERE status='dismissed' AND closed IS NULL", []
    else:
        sql = "SELECT * FROM jobs WHERE closed IS NULL AND status IN ('new','seen')"
        args = []
        if view == "new":
            sql += " AND baseline=0 AND first_seen >= ?"
            args.append(cutoff)
    record = track_record.load()
    rows = [_view(dict(r), prefs, learned, cutoff, record)
            for r in con.execute(sql, args)
            if not is_defense(r["company"], r["title"], r["text"], r["about"])
            and (r["added"] or not is_contract(r["title"], r["text"], r["emptype"]))
            and (view in ("applied", "dismissed") or r["id"] not in tracked)]
    con.close()
    for j in rows:
        if j["id"] in tracked:
            j["status"], j["stage"] = "applied", tracked[j["id"]]
    rows.sort(key=lambda j: (TIER_ORDER.get(j["tier"], 3), -(j["score"] or 0),
                             not j["starred"], not j["new"],
                             _neg(j["posted"] or j["first_seen"])))
    return rows[:int(limit)]


# ── Adding a job by hand
#
# A posting the user found themselves. On a board the poller reads, it is stored
# under the same id the poller uses, so it closes when the posting comes
# down, and the company's board is watched from then on. Anywhere else it
# is stored as ats "manual" and stays until the user applies or dismisses it.
# Either way the title and place filters do not apply: the user picked it.

class AddError(Exception):
    pass


def _from_board(url):
    """(board, posting) when the link is a live posting on a readable board."""
    found = boards_mod.board_from_url(url)
    if not found:
        return None
    ats, slug = found
    try:
        postings = FETCH[ats](slug)
    except Exception:
        return None
    for p in postings:
        pid = str(p["pid"])
        if (len(pid) >= 6 and pid in url) or (p["url"] and p["url"].split("?")[0] == url.split("?")[0]):
            if p["text"] is None and ats in TEXT_LATER:
                try:
                    p["text"] = TEXT_LATER[ats](slug, pid)
                except Exception:
                    p["text"] = ""
            return {"ats": ats, "slug": slug}, p
    return None


def _board_company(b, body, title, url, meta):
    """The company behind a board: its name on the watch list, else read off
    the posting, else the board's slug tidied up."""
    for known in boards_mod.all_boards():
        if (known["ats"], known["slug"].lower()) == (b["ats"], b["slug"].lower()):
            return _pretty(known.get("board_name") or known.get("name") or known["employer"])
    m = (meta or (lambda *a: {}))(body, title, url) or {}
    return m.get("company") or _pretty(b["slug"].split("|")[0])


def _manual_id(url, text):
    import hashlib
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "pasted").replace("www.", "")
    key = url.split("#")[0].strip() or text[:2000]
    return "manual:%s:%s" % (host, hashlib.sha1(key.encode()).hexdigest()[:12])


def add_job(url="", text="", meta=None):
    """Put one posting on the board. Returns (job id, already there).

    meta(text, title, url) gives {company, role, location} for postings off
    the readable boards; the server passes the tailoring extractor.
    """
    import jd_fetch
    url, text = (url or "").strip(), (text or "").strip()
    if not url and len(text) < 200:
        raise AddError("paste the posting link, or its full text")
    if url and not re.match(r"https?://", url):
        raise AddError("that does not look like a link")
    hit = _from_board(url) if url else None
    if hit:
        b, p = hit
        jid = "%s:%s:%s" % (b["ats"], b["slug"], p["pid"])
        title, location, posted = p["title"], p["location"], p["posted"]
        body = _plain(p["text"] or "")[:TEXT_CHARS]
        company = ""
    else:
        page_title = ""
        if url and len(text) < 200:
            try:
                got = jd_fetch.fetch(url)
            except jd_fetch.JdFetchError as e:
                raise AddError("could not read that page (%s). Paste the posting text too." % e)
            text, page_title = got["text"], got["title"]
        body = _plain(text)[:TEXT_CHARS]
        jid = _manual_id(url, body)
        m = (meta or (lambda *a: {}))(body, page_title, url) or {}
        company, title = m.get("company") or "", m.get("role") or ""
        location, posted = m.get("location") or "", ""
        if not title:
            title = page_title.split("|")[0].split(" - ")[0].strip() or "Untitled posting"
    con = db()
    try:
        row = con.execute("SELECT id, company, status, closed FROM jobs WHERE id=?", (jid,)).fetchone()
        if hit and not company:
            company = row["company"] if row else _board_company(b, body, title, url, meta)
        if is_defense(company, title, body):
            raise AddError("that looks like a defense role, so it stays off the board")
        now = _now()
        if row:
            # Already there: bring it back if it was dismissed, and mark it the user's.
            con.execute("UPDATE jobs SET added=COALESCE(added, ?), closed=NULL, "
                        "status=CASE WHEN status='dismissed' THEN 'new' ELSE status END "
                        "WHERE id=?", (now, jid))
            con.commit()
            return jid, True
        con.execute(
            "INSERT INTO jobs(id, ats, slug, employer, company, title, url, location, "
            "seniority, posted, updated, first_seen, last_seen, status, baseline, text, added) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, hit[0]["ats"] if hit else "manual",
             hit[0]["slug"] if hit else jid.split(":")[1],
             (company or "").strip().lower(), company or "Unknown company", title,
             (hit[1]["url"] if hit else url) or "", location, seniority(title),
             posted, "", now, now, "new", 0, body, now))
        con.commit()
    finally:
        con.close()
    if url and company:
        try:
            boards_mod.add_extra(company, url)
        except Exception:
            pass
    return jid, False


def score_one(jid):
    """The fit for one job right away, instead of at the next run."""
    con = db()
    try:
        row = con.execute("SELECT id, title, company, location, text FROM jobs WHERE id=?",
                          (jid,)).fetchone()
        if not row:
            return False
        got = fit.score_batch([dict(row)]).get(jid)
        if not got:
            return False
        con.execute("UPDATE jobs SET tier=?, score=?, reason=?, gaps=?, about=?, tags=?, "
                    "scored=?, score_error='' WHERE id=?",
                    (got["tier"], got["score"], got["reason"], json.dumps(got["gaps"]),
                     got["about"], json.dumps(got["tags"]), _now(), jid))
        con.commit()
        return True
    finally:
        con.close()


def view_one(jid):
    """One job as the board shows it, or None."""
    con = db()
    try:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return None
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - NEW_DAYS * 86400))
        return _view(dict(r), fit.load_prefs(), fit.learn(_acted(con)), cutoff,
                     track_record.load())
    finally:
        con.close()


def _neg(s):
    """Sort key that puts later ISO dates first."""
    return tuple(-ord(c) for c in s or "")


def set_status(job_id, status):
    if status not in STATUSES:
        raise ValueError("status must be one of %s" % ", ".join(STATUSES))
    con = db()
    cur = con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    con.commit()
    con.close()
    return cur.rowcount == 1


def mark(job_id, what, on=True):
    """Record opened, starred or tailored. These feed learned interest."""
    if what not in MARKS:
        raise ValueError("mark must be one of %s" % ", ".join(MARKS))
    con = db()
    if what == "starred":
        cur = con.execute("UPDATE jobs SET starred=? WHERE id=?", (1 if on else 0, job_id))
    else:
        cur = con.execute("UPDATE jobs SET %s=COALESCE(%s, ?) WHERE id=?" % (what, what),
                          (_now() if on else None, job_id))
    con.commit()
    con.close()
    return cur.rowcount == 1


def job_row(job_id):
    """One stored job as the page sees it, for saving to the tracker."""
    con = db()
    r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    con.close()
    if not r:
        return None
    j = dict(r)
    return {"id": j["id"], "company": j["company"], "title": j["title"],
            "url": j["url"], "location": j["location"] or "",
            "salary": salary(j.get("text"))}


def job_text(job_id):
    """(url, company, title, posting text) for tailoring. Fetches the text
    for a Greenhouse job that does not have it yet."""
    con = db()
    r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        con.close()
        return None
    j = dict(r)
    if not j.get("text") and j["ats"] in TEXT_LATER:
        try:
            t = _plain(TEXT_LATER[j["ats"]](j["slug"], j["id"].rsplit(":", 1)[1]))[:TEXT_CHARS]
            con.execute("UPDATE jobs SET text=? WHERE id=?", (t, job_id))
            con.commit()
            j["text"] = t
        except Exception:
            pass
    con.close()
    return j["url"], j["company"], j["title"], j.get("text") or ""


def prefs():
    return fit.load_prefs()


def save_prefs(lines):
    return fit.save_prefs(lines)


def stats():
    con = db()
    # Same rules as list_jobs: defense and contract jobs never count, and "open" is what
    # Recommended shows (not applied, not dismissed).
    out = {"open": 0, "by_status": {}, "by_tier": {}}
    prefs_ = fit.load_prefs()
    learned = fit.learn(_acted(con))
    record = track_record.load()
    companies = set()
    tracked = _tracked()
    for r in con.execute("SELECT * FROM jobs WHERE closed IS NULL"):
        if is_defense(r["company"], r["title"], r["text"], r["about"]) or \
                not r["added"] and is_contract(r["title"], r["text"], r["emptype"]):
            continue
        status = "applied" if r["id"] in tracked else r["status"]
        out["by_status"][status] = out["by_status"].get(status, 0) + 1
        if status not in ("new", "seen"):
            continue
        out["open"] += 1
        companies.add(r["employer"])
        t = fit.adjust(dict(r), prefs_, learned, record)[0] or "unsorted"
        out["by_tier"][t] = out["by_tier"].get(t, 0) + 1
    out["companies"] = len(companies)
    last = con.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 1").fetchone()
    out["last_run"] = dict(last) if last else None
    out["boards"] = con.execute("SELECT COUNT(*) FROM boards WHERE last_ok IS NOT NULL").fetchone()[0]
    out["quiet"] = [bigco.NAMES.get(r["key"].split(":", 1)[1], r["key"]) for r in con.execute(
        "SELECT key FROM boards WHERE key LIKE 'careers:%' AND quiet >= ?", (QUIET_RUNS,))]
    con.close()
    return out


def _print_list(n=20):
    for j in list_jobs(limit=n):
        print("%-13s %-24.24s %-44.44s %-26.26s %-5s %s" % (
            j["tier_label"], j["company"], j["title"], j["location"], j["age"],
            j["reason"]))


def dry_run():
    """What the new boards would add, without writing anything: the boards
    in boards_more.pending.json and the job lists that are switched off.
    Polls them once and counts the jobs that would land on the board."""
    cfg = config()
    have = {(b["ats"], b["slug"].lower()) for b in boards_mod.all_boards()}
    new = [b for path in (boards_mod.MORE_PENDING, boards_mod.WORKDAY_PENDING)
           for b in boards_mod._load(path, {}).get("boards", [])
           if b.get("verified") and (b["ats"], b["slug"].lower()) not in have]
    for flag, mod in (("careers_sites", bigco), ("yc_jobs", yc), ("hn_jobs", hn)):
        if not cfg.get(flag):
            new += mod.boards()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(_fetch_board, new))
    secs = time.time() - t0
    by, failed = {}, []
    for b, postings, err in results:
        c = by.setdefault(b["ats"], {"boards": 0, "jobs": 0})
        c["boards"] += 1
        if err:
            failed.append("%s:%s %s" % (b["ats"], b["slug"], err))
            continue
        n = sum(1 for p in postings if is_wanted_title(p["title"], cfg)
                and location_ok(p["location"], p["remote"], cfg, p["title"])
                and not is_contract(p["title"], p["text"], p.get("emptype"))
                and not is_defense(b.get("name") or b["employer"], p["title"], p["text"]))
        c["jobs"] += n
        if n:
            print("  %-16s %-28s %d" % (b["ats"], b.get("name") or b["employer"], n))
    print("\n%d new boards, polled in %.0fs" % (len(new), secs))
    for ats, c in sorted(by.items()):
        print("  %-16s %4d boards, %d matching jobs"
              % (ats, c["boards"], c["jobs"]))
    print("%d failed" % len(failed))
    for f in failed:
        print("  " + f)


if __name__ == "__main__":
    # The server loads .env itself. Run on its own (a LaunchAgent or cron),
    # the watcher needs it too: it holds the AI settings that a scheduler
    # does not pass in.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except ImportError:
        pass
    if "--backfill-scores" in sys.argv:
        c = db()
        print("scored", score_backfill(c, log=lambda s: print(s, flush=True)))
        c.close()
    elif "--dry" in sys.argv:
        dry_run()
    elif "--list" in sys.argv:
        _print_list(int(sys.argv[sys.argv.index("--list") + 1]) if len(sys.argv) > 2 else 20)
    else:
        run(notify="--quiet" not in sys.argv,
            log=lambda s: print(s, flush=True))
        if "--quiet" not in sys.argv:
            three_a_week()
