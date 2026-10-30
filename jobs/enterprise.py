"""Big employers' own job systems: Intuit (Radancy TalentBrew),
Rivian (iCIMS on Jibe), Joby (iCIMS classic), EA (Avature), Yelp and
Scopely (Phenom).

Their terms discourage robots on their own sites, so this is off unless
you set "company_sites": true in jobs/config.json; that choice is yours.
When on, it keeps to the same gentle rules as the VC portfolio pages: public
listings only, robots.txt checked, an honest user agent, one request at a
time with a pause, and a site that blocks or challenges once is stopped
for good (data/enterprise_blocked.json) rather than worked around. Delete
its entry there to try it again.

Each fetcher returns the watcher's plain dicts:
{pid, title, url, location, remote, posted, updated, text, emptype}.
Lists that carry no description get it from TEXT, per matching job.
"""

import html
import json
import os
import re
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

from jobs.common import DATA_DIR

UA = "VerbatimJobWatcher/1.0 (one person's job search; public listings, a few times a day)"
PAUSE = 1.0
TIMEOUT = 30
BLOCKED_PATH = os.path.join(DATA_DIR, "enterprise_blocked.json")
_CHALLENGE = re.compile(r"just a moment|cf-chl|captcha|access denied|attention required", re.I)
_lock = threading.Lock()
_robots = {}


class Blocked(Exception):
    pass


def _blocked():
    try:
        with open(BLOCKED_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _stop(host, why):
    with _lock:
        b = _blocked()
        b[host] = {"on": time.strftime("%Y-%m-%d %H:%M"), "why": why}
        with open(BLOCKED_PATH + ".tmp", "w") as f:
            json.dump(b, f, indent=1)
        os.replace(BLOCKED_PATH + ".tmp", BLOCKED_PATH)
    raise Blocked("%s stopped: %s" % (host, why))


def _allowed(url):
    host = "{0.scheme}://{0.netloc}".format(urlparse(url))
    with _lock:
        rp = _robots.get(host)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(host + "/robots.txt", headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp.allow_all = True
        with _lock:
            _robots[host] = rp
    return rp.can_fetch(UA, url)


def _req(method, url, **kw):
    """One polite request. A block, a challenge page or a robots.txt
    disallow stops the site; it is never retried around."""
    host = urlparse(url).netloc
    if host in _blocked():
        raise Blocked("%s stopped earlier: %s" % (host, _blocked()[host]["why"]))
    if not _allowed(url):
        _stop(host, "robots.txt disallows %s" % urlparse(url).path)
    headers = {"User-Agent": UA, **kw.pop("headers", {})}
    r = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
    time.sleep(PAUSE)
    if r.status_code in (403, 429) or (r.status_code == 503 and _CHALLENGE.search(r.text[:3000])):
        _stop(host, "answered %d" % r.status_code)
    if "html" in r.headers.get("content-type", "") and _CHALLENGE.search(r.text[:3000]) and r.status_code >= 400:
        _stop(host, "challenge page")
    r.raise_for_status()
    return r


def _plain(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def _job(pid, title, url, loc, remote=None, posted="", text=""):
    return {"pid": str(pid), "title": (title or "").strip(), "url": url, "location": loc or "",
            "remote": bool(remote) if remote is not None else "remote" in (loc or "").lower(),
            "posted": (posted or "")[:19], "updated": "", "text": text or None, "emptype": ""}


# ── Radancy TalentBrew (jobs.intuit.com): the public sitemap. Title and city
#    come from the job's URL; the description is read per matching job.

def fetch_talentbrew(host):
    locs = re.findall(r"<loc>([^<]+)</loc>", _req("GET", "https://%s/sitemap.xml" % host).text)
    for sub in [l for l in locs if l.endswith(".xml")]:
        locs += re.findall(r"<loc>([^<]+)</loc>", _req("GET", sub).text)
    out = {}
    for u in locs:
        m = re.search(r"/job/([^/]+)/([^/]+)/(\d+)/(\d+)", u)
        if m and m.group(4) not in out:
            city, slug, _, jid = m.groups()
            # The pid is the URL's path after /job/, so the description can
            # be read later from the pid alone.
            out[jid] = _job("/".join(m.groups()), slug.replace("-", " ").title(), u,
                            city.replace("-", " ").title())
    return list(out.values())


def text_talentbrew(host, pid):
    """The description, from the job page's JSON-LD."""
    h = _req("GET", "https://%s/job/%s" % (host, pid), headers={"Accept": "text/html"}).text
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            loc = d.get("jobLocation")
            loc = loc[0] if isinstance(loc, list) and loc else loc
            a = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
            place = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"),
                                          a.get("addressCountry")) if isinstance(x, str))
            return " ".join(x for x in (place, _plain(d.get("description"))) if x)
    return ""


# ── iCIMS on Jibe (careers.rivian.com): a JSON list, 100 a page.

def fetch_jibe(host):
    out, page = [], 1
    while True:
        j = _req("GET", "https://%s/api/jobs" % host, params={"page": page, "limit": 100}).json()
        for x in j.get("jobs") or []:
            d = x.get("data") or {}
            loc = d.get("full_location") or d.get("location_name") or ""
            if d.get("country") and d["country"] not in loc:
                loc += ", " + d["country"]
            out.append(_job(d.get("req_id") or d.get("slug"), d.get("title"),
                            "https://%s/careers-home/jobs/%s" % (host, d.get("slug")), loc,
                            "remote" in (loc + " " + (d.get("location_type") or "")).lower(),
                            d.get("posted_date"), _plain(d.get("description"))))
        if not j.get("jobs") or page * 100 >= (j.get("totalCount") or 0):
            return out
        page += 1


# ── iCIMS classic (careers-<co>.icims.com): the search page, which robots
#    allows, a page at a time.

_ICIMS_ROW = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)


def fetch_icims(host):
    out, pr = {}, 0
    while pr < 40:
        t = _req("GET", "https://%s/jobs/search" % host, params={"ss": 1, "in_iframe": 1, "pr": pr},
                 headers={"Accept": "text/html"}).text
        new = 0
        for row in _ICIMS_ROW.findall(t):
            a = re.search(r'href="([^"]+/jobs/(\d+)/[^"]+)"', row)
            h3 = re.search(r"<h3[^>]*>(.*?)</h3>", row, re.S)
            if not a or not h3 or a.group(2) in out:
                continue
            loc = re.search(r"Job Locations</span>\s*<span[^>]*>(.*?)</span>", row, re.S)
            desc = re.search(r'class="col-xs-12 description">(.*?)</div>', row, re.S)
            out[a.group(2)] = _job(a.group(2), _plain(h3.group(1)),
                                   a.group(1).replace("?in_iframe=1", "").replace("&amp;", "&"),
                                   _plain(loc.group(1)) if loc else "", None, "",
                                   _plain(desc.group(1)) if desc else "")
            new += 1
        if not new:
            break
        pr += 1
    return list(out.values())


# ── Avature (jobs.ea.com): the search results page, 20 at a time.

_AVATURE_ART = re.compile(r'<article class="article article--result.*?</article>', re.S)


def fetch_avature(slug):
    host, portal = (slug.split("|") + ["/en_US/careers"])[:2]
    out, off = {}, 0
    while off < 2000:
        t = _req("GET", "https://%s%s/SearchJobs/" % (host, portal),
                 params={"jobRecordsPerPage": 20, "jobOffset": off}, headers={"Accept": "text/html"}).text
        arts = _AVATURE_ART.findall(t)
        new = 0
        for a in arts:
            m = re.search(r'href="([^"]+/JobDetail/[^"]*?/(\d+))"[^>]*>(.*?)</a>', a, re.S)
            if not m or m.group(2) in out:
                continue
            loc = re.search(r'list-item-location">(.*?)</span>', a, re.S)
            out[m.group(2)] = _job(m.group(2), _plain(m.group(3)), m.group(1),
                                   _plain(loc.group(1)) if loc else "")
            new += 1
        if not new:
            break
        off += len(arts)
    return list(out.values())


# ── Phenom (www.yelp.careers, careers.scopely.com): the page's own search
#    call, after reading its refNum from the results page.

def fetch_phenom(slug):
    host, base = (slug.split("|") + ["/us/en"])[:2]
    h = _req("GET", "https://%s%s/search-results" % (host, base), headers={"Accept": "text/html"}).text
    ref = re.search(r'"refNum"\s*:\s*"([^"]+)"', h)
    if not ref:
        raise ValueError("Phenom page has no refNum")
    lang = (re.search(r'"locale"\s*:\s*"([^"]+)"', h) or [None, "en_us"])[1]
    out, frm, size = [], 0, 50
    while frm < 3000:
        body = {"lang": lang, "deviceType": "desktop", "country": base.strip("/").split("/")[0],
                "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "Most recent",
                "subsearch": "", "from": frm, "jobs": True, "counts": False, "all_fields": [],
                "size": size, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": "page20", "siteType": "external", "keywords": "", "global": True,
                "selected_fields": {}, "locationData": {}, "refNum": ref.group(1)}
        j = _req("POST", "https://%s/widgets" % host, json=body).json().get("refineSearch") or {}
        jobs = (j.get("data") or {}).get("jobs") or []
        for p in jobs:
            locs = p.get("multi_location") or [p.get("location") or p.get("cityStateCountry") or ""]
            loc = " / ".join(str(x) for x in locs)
            if p.get("country") and p["country"] not in loc:
                loc += ", " + p["country"]
            out.append(_job(p.get("jobId") or p.get("reqId"), p.get("title"),
                            "https://%s%s/job/%s" % (host, base, p.get("jobId")), loc,
                            p.get("isRemote") is True or "remote" in loc.lower(),
                            p.get("postedDate"), p.get("descriptionTeaser") or ""))
        frm += size
        if not jobs or frm >= (j.get("totalHits") or 0):
            break
    return out


FETCH = {"talentbrew": fetch_talentbrew, "jibe": fetch_jibe, "icims": fetch_icims,
         "avature": fetch_avature, "phenom": fetch_phenom}
TEXT = {"talentbrew": text_talentbrew}
BOARDS = [("Intuit", "talentbrew", "jobs.intuit.com"),
          ("Rivian", "jibe", "careers.rivian.com"),
          ("Joby Aviation", "icims", "careers-jobyaviation.icims.com"),
          ("Electronic Arts", "avature", "jobs.ea.com|/en_US/careers"),
          ("Yelp", "phenom", "www.yelp.careers|/us/en"),
          ("Scopely", "phenom", "careers.scopely.com|/us/en")]


def boards():
    return [{"employer": n.lower(), "name": n, "ats": a, "slug": s, "verified": True,
             "source": "company site"} for n, a, s in BOARDS]
