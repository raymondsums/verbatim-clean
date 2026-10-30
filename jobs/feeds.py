"""Job lists that carry many companies.

Dribbble's public jobs RSS, We Work Remotely's design RSS, Himalayas' free
search API, and Google Jobs through SerpApi. Dribbble and We Work Remotely
are design job lists; Himalayas and SerpApi search for the "roles" in
jobs/config.json. Each reads as one board whose postings carry their own
company, and they go through the same title, area, defense and contract
filters as every board. A posting that is already open from another
source, same company and same title, is skipped by the watcher (DUPES).

Himalayas and SerpApi show a recent sample, not the whole list, so a job
missing from one run is not gone; the watcher closes them only after
SAMPLED days unseen, like YC.

SerpApi needs SERPAPI_KEY in .env and stays inside a monthly limit you
set (see below), so a free plan is never overrun. The key is never printed
or logged.
"""

import email.utils
import html
import json
import os
import re
import tempfile
import time

import requests

from jobs.common import BASE_DIR, DATA_DIR, load_config

TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
NAMES = {"dribbble": "Dribbble", "himalayas": "Himalayas", "serpapi": "Google Jobs",
         "wwr": "We Work Remotely"}
# YC's jobs page is deduped the same way: its companies' own boards are
# polled too once discovery finds them.
DUPES = set(NAMES) | {"yc"}
SAMPLED = {"himalayas", "serpapi"}


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) if t else ""


# ── Dribbble: one public RSS feed, about 50 jobs worldwide. Titles read
#    "<Company> is hiring for a position of <Role> in <Place>".

DRIBBBLE_RSS = "https://dribbble.com/jobs.rss"
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TAG = lambda tag: re.compile(r"<%s>(.*?)</%s>" % (tag, tag), re.S)
_SAYS = re.compile(r"^(.*?) is hiring for a position of (.*) in (.*)$", re.S)


def fetch_dribbble(slug=""):
    r = requests.get(DRIBBBLE_RSS, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for item in _ITEM.findall(r.text):
        get = lambda tag: html.unescape((_TAG(tag).search(item) or [None, ""])[1]).strip()
        m = _SAYS.match(get("title"))
        link = get("link")
        jid = re.search(r"/jobs/(\d+)", link)
        if not m or not jid:
            continue
        company, title, place = (x.strip() for x in m.groups())
        try:
            posted = _iso(email.utils.parsedate_to_datetime(get("pubDate")).timestamp())
        except (TypeError, ValueError):
            posted = ""
        out.append({"pid": jid.group(1), "title": title, "url": link.split("?")[0],
                    "location": place, "remote": "remote" in place.lower(),
                    "posted": posted, "updated": "", "text": None, "emptype": "",
                    "company": get("dc:creator") or company})
    return out


# ── We Work Remotely: the design category's public RSS. Titles read
#    "<Company>: <Role>"; <region> says where it can be done from.

WWR_RSS = "https://weworkremotely.com/categories/remote-design-jobs.rss"


def fetch_wwr(slug=""):
    r = requests.get(WWR_RSS, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for item in _ITEM.findall(r.text):
        get = lambda tag: html.unescape((_TAG(tag).search(item) or [None, ""])[1]).strip()
        company, _, title = get("title").partition(": ")
        link = get("link") or get("guid")
        if not title or not link:
            continue
        region = get("region")
        place = "Remote, " + (region or "anywhere")
        try:
            posted = _iso(email.utils.parsedate_to_datetime(get("pubDate")).timestamp())
        except (TypeError, ValueError):
            posted = ""
        out.append({"pid": link.rstrip("/").rsplit("/", 1)[-1][:120], "title": title.strip(),
                    "url": link, "location": place, "remote": True, "posted": posted,
                    "updated": "", "text": re.sub(r"<[^>]+>", " ", get("description")),
                    "emptype": get("type"), "company": company.strip()})
    return out


# ── Himalayas: remote jobs only. The first page of a few searches, newest
#    first; robots.txt asks that search pages not be paged through.

HIMALAYAS = "https://himalayas.app/jobs/api/search"
# Used when config.json has no "roles".
EXAMPLE_ROLES = ("product designer", "ux designer", "design lead", "ui designer")


def _roles(cfg=None):
    cfg = load_config() if cfg is None else cfg
    roles = [r for r in cfg.get("roles") or [] if isinstance(r, str) and r.strip()]
    return tuple(roles[:4]) or EXAMPLE_ROLES


def _us(cfg):
    return re.fullmatch(r"\s*(us|usa|u\.s\.?|united states( of america)?)\s*",
                        cfg.get("remote_country") or "United States", re.I) is not None


def _himalayas_place(j):
    where = j.get("locationRestrictions") or []
    if not where:
        return "Remote, anywhere"
    if "United States" in where:
        return "Remote, United States"
    return "Remote, " + ", ".join(where[:4])


def fetch_himalayas(slug=""):
    cfg = load_config()
    out, seen = [], set()
    for q in _roles(cfg):
        params = {"q": q, "sort": "recent"}
        if _us(cfg):
            params["country"] = "US"
        r = requests.get(HIMALAYAS, params=params,
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobs") or []:
            pid = j.get("guid") or j.get("applicationLink") or ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            out.append({"pid": pid.rstrip("/").split("/companies/")[-1], "title": (j.get("title") or "").strip(),
                        "url": pid, "location": _himalayas_place(j), "remote": True,
                        "posted": _iso(j.get("pubDate")), "updated": "",
                        "text": html.unescape(re.sub(r"<[^>]+>", " ", j.get("description") or "")),
                        "emptype": j.get("employmentType") or "",
                        "company": j.get("companyName") or ""})
    return out


# ── Google Jobs through SerpApi. Needs SERPAPI_KEY in .env. Searches are
#    counted in data/serpapi_budget.json and stop at "serpapi_monthly_limit"
#    from config.json (default 90, under the free plan's 100), with the month
#    starting on "serpapi_cycle_day" (default 1). Never more than PER_DAY a
#    day (data/serpapi_usage.json).

SERPAPI = "https://serpapi.com/search.json"
USAGE_PATH = os.path.join(DATA_DIR, "serpapi_usage.json")
BUDGET_PATH = os.path.join(DATA_DIR, "serpapi_budget.json")
MONTHLY_LIMIT = 90
CYCLE_DAY = 1
PER_DAY = 3


def serp_queries(cfg=None):
    """(query, place) pairs from the roles and cities in config.json, one per
    watcher run in turn until the day's searches are spent."""
    cfg = load_config() if cfg is None else cfg
    roles = _roles(cfg)[:2]
    cities = [c for c in cfg.get("cities") or [] if isinstance(c, str) and c.strip()][:2]
    country = cfg.get("remote_country") or "United States"
    out = [(r, c) for r in roles for c in cities]
    if cfg.get("allow_remote", True):
        out += [(r + " remote", country) for r in roles]
    return tuple(out) or ((roles[0], country),)


def serpapi_key(cfg=None):
    """SERPAPI_KEY from the environment or this project's .env, read each
    time and kept nowhere else."""
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(BASE_DIR, ".env")) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == "SERPAPI_KEY":
                    return v.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _budget_path(cfg=None):
    return BUDGET_PATH


def _cycle_start(today, day=CYCLE_DAY):
    day = max(1, min(28, int(day or 1)))
    if today.tm_mday >= day:
        return "%04d-%02d-%02d" % (today.tm_year, today.tm_mon, day)
    y, m = (today.tm_year, today.tm_mon - 1) if today.tm_mon > 1 else (today.tm_year - 1, 12)
    return "%04d-%02d-%02d" % (y, m, day)


def _read(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def _write(path, data, indent=None):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _take_search(cfg=None):
    """Claim one search on the meter, or None. A new plan month starts the
    count at 0, and the attempt is counted before the request goes out."""
    cfg = load_config() if cfg is None else cfg
    today = time.localtime()
    mine = _read(USAGE_PATH, {})
    day = time.strftime("%Y-%m-%d", today)
    if mine.get("day") != day:
        mine = {"day": day, "today": 0, "next": mine.get("next", 0)}
    if mine["today"] >= PER_DAY:
        return None
    cycle = _cycle_start(today, cfg.get("serpapi_cycle_day", CYCLE_DAY))
    state = _read(_budget_path(cfg), {})
    if not isinstance(state, dict) or state.get("cycle") != cycle:
        state = {"cycle": cycle, "used": 0, "synced_at": ""}
    try:
        used = max(0, int(state.get("used") or 0))
    except (TypeError, ValueError):
        used = 0
    try:
        limit = int(cfg.get("serpapi_monthly_limit", MONTHLY_LIMIT))
    except (TypeError, ValueError):
        limit = MONTHLY_LIMIT
    if used >= limit:
        return None
    state["used"] = used + 1
    os.makedirs(DATA_DIR, exist_ok=True)
    _write(_budget_path(cfg), state, indent=2)
    queries = serp_queries(cfg)
    q = queries[mine["next"] % len(queries)]
    mine["today"] += 1
    mine["next"] += 1
    _write(USAGE_PATH, mine)
    return q


def _serp_age(s):
    m = re.search(r"(\d+)\s*(hour|day|week|month)", s or "", re.I)
    if not m:
        return ""
    days = int(m.group(1)) * {"hour": 0, "day": 1, "week": 7, "month": 30}[m.group(2).lower()]
    return _iso(time.time() - days * 86400)


def fetch_serpapi(slug="", cfg=None):
    key = serpapi_key(cfg)
    if not key:
        return []
    got = _take_search(cfg)
    if not got:
        return []
    q, where = got
    try:
        r = requests.get(SERPAPI, params={"engine": "google_jobs", "q": q, "location": where,
                                          "hl": "en", "chips": "date_posted:week",
                                          "api_key": key}, timeout=TIMEOUT)
    except requests.RequestException as e:
        # The key rides in the URL, so only the kind of failure is reported.
        raise RuntimeError("SerpApi request failed (%s)" % type(e).__name__) from None
    if r.status_code != 200:
        raise RuntimeError("SerpApi answered %d" % r.status_code)
    out, seen = [], set()
    for j in r.json().get("jobs_results") or []:
        pid = j.get("job_id") or ""
        if not pid or pid in seen:
            continue
        seen.add(pid)
        ext = j.get("detected_extensions") or {}
        links = [o.get("link") for o in j.get("apply_options") or [] if o.get("link")]
        out.append({"pid": pid[:120], "title": (j.get("title") or "").strip(),
                    "url": links[0] if links else (j.get("share_link") or ""),
                    "location": j.get("location") or "", "remote": bool(ext.get("work_from_home")),
                    "posted": _serp_age(ext.get("posted_at")), "updated": "",
                    "text": j.get("description") or "", "emptype": ext.get("schedule_type") or "",
                    "company": j.get("company_name") or "", "links": links})
    return out


def boards(cfg):
    out = []
    if cfg.get("dribbble_jobs"):
        out.append({"employer": "dribbble", "name": "Dribbble", "ats": "dribbble", "slug": "jobs",
                    "verified": True, "source": "dribbble"})
    if cfg.get("wwr_jobs"):
        out.append({"employer": "we work remotely", "name": "We Work Remotely", "ats": "wwr",
                    "slug": "design", "verified": True, "source": "wwr"})
    if cfg.get("himalayas_jobs"):
        out.append({"employer": "himalayas", "name": "Himalayas", "ats": "himalayas", "slug": "design",
                    "verified": True, "source": "himalayas"})
    if cfg.get("serpapi_jobs") and serpapi_key(cfg):
        out.append({"employer": "google jobs", "name": "Google Jobs", "ats": "serpapi", "slug": "design",
                    "verified": True, "source": "serpapi"})
    return out
