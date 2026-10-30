"""YC startups' public job list.

ycombinator.com/jobs/role/<role> is public, no account. The role is
"yc_role" in jobs/config.json (YC's own role names, such as designer,
software-engineer, product-manager, marketing or sales; default designer).
The page carries its listings as JSON in a data-page attribute, 20 at a
time. Applying goes through Work at a Startup, which needs a YC account,
so only the public pages are read here.

One board holds jobs from many companies, so each posting carries its own
company.
"""

import html
import json
import re

import requests

from jobs.common import load_config

BASE = "https://www.ycombinator.com"


def paths():
    """The role's pages: everywhere, remote, and the first city if it has
    one of YC's city pages (a missing page is skipped)."""
    cfg = load_config()
    role = re.sub(r"[^a-z0-9-]+", "-", (cfg.get("yc_role") or "designer").lower()).strip("-")
    out = ["/jobs/role/%s" % role, "/jobs/role/%s/remote" % role]
    cities = [c for c in cfg.get("cities") or [] if isinstance(c, str) and c.strip()]
    if cities:
        out.append("/jobs/role/%s/%s" % (role, re.sub(r"[^a-z0-9]+", "-", cities[0].lower()).strip("-")))
    return tuple(out)
# The pages show a sample, not the whole list, so a job missing from one run
# is not gone. The watcher closes a YC job only after this many days unseen.
STALE_DAYS = 7
TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_DATA = re.compile(r'data-page="([^"]+)"')
_AGE = re.compile(r"(\d+)\s*(minute|hour|day|week|month|year)", re.I)
_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _props(path):
    r = requests.get(BASE + path, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    m = _DATA.search(r.text)
    if not m:
        raise ValueError("YC page has no job data")
    return json.loads(html.unescape(m.group(1))).get("props") or {}


def _posted(age):
    """createdAt is relative ("12 months"); turned into a rough date."""
    import time
    m = _AGE.search(age or "")
    if not m:
        return ""
    days = int(m.group(1)) * _DAYS[m.group(2).lower()]
    return time.strftime("%Y-%m-%dT00:00:00", time.gmtime(time.time() - days * 86400))


def fetch(slug="designer"):
    # A page is a sample of 20 from the whole list, and later pages often
    # repeat it, so pages are read until two in a row bring nothing new.
    out, seen = [], set()
    for path in paths():
        dry = 0
        for page in range(1, 13):
            try:
                rows = _props("%s?page=%d" % (path, page)).get("jobPostings") or []
            except requests.HTTPError:
                break  # no such page for this role or city
            fresh = [j for j in rows if j.get("id") not in seen]
            for j in fresh:
                seen.add(j.get("id"))
                loc = j.get("location") or ""
                # The page path is the id: the description is fetched from it.
                out.append({"pid": j.get("url") or str(j["id"]), "title": (j.get("title") or "").strip(),
                            "url": BASE + (j.get("url") or ""), "location": loc,
                            "remote": "remote" in loc.lower(),
                            "posted": _posted(j.get("createdAt")), "updated": "",
                            "text": None, "emptype": j.get("type") or "",
                            "company": j.get("companyName") or ""})
            dry = 0 if fresh else dry + 1
            if len(rows) < 20 and page > 2 or dry >= 2:
                break
    return out


def text(slug, pid):
    """The posting's description, from its own page (pid is its path)."""
    job = _props(pid).get("job") or {}
    return job.get("description") or ""


def boards():
    return [{"employer": "yc", "name": "YC startups", "ats": "yc", "slug": "designer",
             "verified": True, "source": "yc"}]
