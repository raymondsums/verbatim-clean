"""Big companies that run their own careers sites.

Amazon, Apple, Microsoft, Google, Meta, Atlassian, Netflix, TikTok, Ford and PwC
post on their own sites, not on
a board the other fetchers read. Each site has a search the page itself
calls; none is a published API, so any of them can change without notice.
The watcher keeps count of runs that came back empty or failed, and the
Jobs page says when one has gone quiet.

Each fetcher returns the same plain dicts as watch.py's:
{pid, title, url, location, remote, posted, updated, text, emptype}.
Searches use the roles in jobs/config.json (else the example design
roles) in the US; the watcher's title and place filters still decide what
lands. Descriptions that the list does
not carry come from TEXT, per matching job.
"""

import html
import json
import re
import threading
import time

import requests

from jobs.common import is_wanted_title

TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
EXAMPLE_QUERIES = ("product designer", "ux designer", "interaction designer",
                   "visual designer", "ui designer")


def queries():
    from jobs.common import load_config
    roles = [r for r in load_config().get("roles") or [] if isinstance(r, str) and r.strip()]
    return tuple(roles[:5]) or EXAMPLE_QUERIES
NAMES = {"amazon": "Amazon", "apple": "Apple", "microsoft": "Microsoft",
         "google": "Google", "meta": "Meta", "atlassian": "Atlassian",
         "netflix": "Netflix", "tiktok": "TikTok", "ford": "Ford", "pwc": "PwC"}

_lock = threading.Lock()
_sessions = {}


def _session(site, warm=None):
    """One session per site, warmed once (cookies, tokens)."""
    with _lock:
        s = _sessions.get(site)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = UA
            if warm:
                warm(s)
            _sessions[site] = s
        return s


def _drop(site):
    with _lock:
        _sessions.pop(site, None)


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(ts)))
    except (TypeError, ValueError):
        return ""


# ── Amazon: a JSON search behind amazon.jobs

def fetch_amazon():
    out, seen = [], set()
    for q in queries():
        offset = 0
        while offset < 300:
            r = _session("amazon").get(
                "https://www.amazon.jobs/en/search.json",
                params={"base_query": q, "country": "USA", "result_limit": 100,
                        "offset": offset}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            page = data.get("jobs") or []
            for j in page:
                pid = str(j.get("id_icims") or j.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                try:
                    posted = time.strftime("%Y-%m-%dT00:00:00", time.strptime(
                        re.sub(r"\s+", " ", j.get("posted_date") or ""), "%B %d, %Y"))
                except ValueError:
                    posted = ""
                out.append({"pid": pid, "title": j.get("title") or "",
                            "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                            "location": j.get("normalized_location") or j.get("location") or "",
                            "remote": False, "posted": posted, "updated": "",
                            "text": "\n\n".join(j.get(k) or "" for k in (
                                "description", "basic_qualifications",
                                "preferred_qualifications")),
                            "emptype": j.get("job_schedule_type") or ""})
            offset += 100
            if len(page) < 100 or offset >= (data.get("hits") or 0):
                break
    return out


# ── Apple: a CSRF token first, then one exact-phrase search per title
#    (an unquoted search matches thousands of unrelated jobs).

def _apple_warm(s):
    r = s.get("https://jobs.apple.com/api/v1/CSRFToken", timeout=TIMEOUT)
    r.raise_for_status()
    s.headers["x-apple-csrf-token"] = r.headers.get("x-apple-csrf-token", "")


def fetch_apple():
    _drop("apple")  # a fresh token every run
    s = _session("apple", _apple_warm)
    out, seen = [], set()
    for q in queries():
        for page in range(1, 6):
            r = s.post("https://jobs.apple.com/api/v1/search", timeout=TIMEOUT, json={
                "query": '"%s"' % q, "filters": {"locations": ["postLocation-USA"]},
                "page": page, "locale": "en-us", "sort": "newest",
                "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}})
            r.raise_for_status()
            res = r.json().get("res") or {}
            rows = res.get("searchResults") or []
            for j in rows:
                pid = str(j.get("positionId") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                locs = [l.get("name") or "" for l in j.get("locations") or []]
                out.append({"pid": pid, "title": j.get("postingTitle") or "",
                            "url": "https://jobs.apple.com/en-us/details/%s/%s"
                                   % (pid, j.get("transformedPostingTitle") or ""),
                            "location": " / ".join(l for l in locs if l),
                            "remote": bool(j.get("homeOffice")),
                            "posted": (j.get("postDateInGMT") or "")[:19],
                            "updated": "", "text": None, "emptype": ""})
            if len(rows) < 20 or page * 20 >= (res.get("totalRecords") or 0):
                break
    return out


def apple_text(pid):
    s = _session("apple", _apple_warm)
    r = s.get("https://jobs.apple.com/api/v1/jobDetails/PIPE-%s" % pid,
              params={"locale": "en-us"}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json().get("res") or {}
    return "\n\n".join(d.get(k) or "" for k in (
        "jobSummary", "description", "minimumQualifications", "preferredQualifications"))


# ── Microsoft: the careers page sets cookies the search needs; it answers
#    429 to cold or quick calls, so it is warmed first and paced.

MS = "https://apply.careers.microsoft.com"


def _ms_warm(s):
    s.get(MS + "/careers", params={"domain": "microsoft.com"}, timeout=TIMEOUT)
    s.headers.update({"Accept": "application/json", "Referer": MS + "/careers"})


def fetch_microsoft():
    _drop("microsoft")
    s = _session("microsoft", _ms_warm)
    out, seen = [], set()
    for q in queries()[:3]:
        start = 0
        while start < 100:
            time.sleep(1)
            r = s.get(MS + "/api/pcsx/search", timeout=TIMEOUT, params={
                "domain": "microsoft.com", "query": q, "location": "United States",
                "start": start, "sort_by": "timestamp", "filter_include_remote": 1})
            r.raise_for_status()
            rows = (r.json().get("data") or {}).get("positions") or []
            for j in rows:
                pid = str(j.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                out.append({"pid": pid, "title": j.get("name") or "",
                            "url": MS + "/careers/job/" + pid,
                            "location": " / ".join(j.get("standardizedLocations") or
                                                   j.get("locations") or []),
                            "remote": (j.get("workLocationOption") or "") == "remote",
                            "posted": _iso(j.get("postedTs")), "updated": "",
                            "text": None, "emptype": ""})
            start += 10
            if len(rows) < 10:
                break
    return out


def microsoft_text(pid):
    s = _session("microsoft", _ms_warm)
    time.sleep(1)
    r = s.get(MS + "/api/pcsx/position_details", timeout=TIMEOUT,
              params={"position_id": pid, "domain": "microsoft.com", "hl": "en"})
    r.raise_for_status()
    d = r.json().get("data") or {}
    kind = " ".join(d.get("efcustomTextEmploymentType") or [])
    return ("Employment type: %s\n\n" % kind if kind else "") + (d.get("jobDescription") or "")


# ── Google: the results page carries its data as JSON in the HTML. Read by
#    position, so a layout change shows up as an empty or failed run.

_G_DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:(.*?), sideChannel", re.S)


def _at(x, *path):
    for i in path:
        if not isinstance(x, list) or len(x) <= i:
            return None
        x = x[i]
    return x


def fetch_google():
    out, seen = [], set()
    for page in range(1, 6):
        r = _session("google").get(
            "https://www.google.com/about/careers/applications/jobs/results",
            params={"category": "USER_EXPERIENCE", "location": "United States",
                    "page": page}, timeout=TIMEOUT)
        r.raise_for_status()
        m = _G_DATA.search(r.text)
        if not m:
            raise ValueError("Google results page has no job data")
        data = json.loads(m.group(1))
        rows = _at(data, 0) or []
        for j in rows:
            pid = str(_at(j, 0) or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            places = [p for p in (_at(j, 9) or []) if isinstance(p, list)]
            us = [p[0] for p in places if p and _at(p, 5) == "US"]
            title = _at(j, 1) or ""
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            out.append({"pid": pid, "title": title,
                        "url": "https://www.google.com/about/careers/applications/jobs/results/%s-%s"
                               % (pid, slug),
                        "location": " / ".join(us or [p[0] for p in places if p]),
                        "remote": any("remote" in (p[0] or "").lower() for p in places if p),
                        "posted": _iso(_at(j, 12, 0)), "updated": "",
                        "text": "\n\n".join(x for x in (_at(j, 10, 1), _at(j, 3, 1),
                                                        _at(j, 4, 1), _at(j, 19, 1))
                                            if isinstance(x, str)),
                        "emptype": ""})
        total, size = _at(data, 2) or 0, _at(data, 3) or 20
        if len(rows) < size or page * size >= total:
            break
    return out


# ── Meta: a GraphQL query with a token taken from the search page. The
#    query id comes from Meta's own page code and can change on a deploy;
#    when it does, runs fail and the Jobs page says Meta went quiet.

META = "https://www.metacareers.com"
META_SEARCH_DOC = "27506805582236862"
_NAV = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9",
        "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "none"}


def fetch_meta():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    page = s.get(META + "/jobsearch", headers=_NAV, timeout=TIMEOUT)
    page.raise_for_status()
    m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', page.text)
    if not m:
        raise ValueError("Meta search page has no token")
    lsd = m.group(1)
    variables = {"search_input": {
        "q": None, "divisions": [], "offices": [], "roles": [], "leadership_levels": [],
        "saved_jobs": [], "saved_searches": [], "sub_teams": [],
        "teams": ["Design & User Experience"], "is_leadership": False,
        "is_remote_only": False, "sort_by_new": True, "results_per_page": None},
        "isLoggedIn": False, "viewasUserID": None}
    r = s.post(META + "/graphql", timeout=TIMEOUT, data={
        "lsd": lsd, "doc_id": META_SEARCH_DOC, "__a": "1", "__comet_req": "15",
        "fb_api_req_friendly_name": "CareersJobSearchResultsDataQuery",
        "variables": json.dumps(variables)}, headers={
        "X-FB-LSD": lsd, "Origin": META, "Referer": META + "/jobsearch",
        "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"})
    r.raise_for_status()
    body = r.text[r.text.index("{"):] if "{" in r.text else r.text
    rows = ((json.loads(body).get("data") or {}).get("job_search_with_featured_jobs")
            or {}).get("all_jobs")
    if rows is None:
        raise ValueError("Meta search answered without a job list")
    out = []
    for j in rows:
        pid = str(j.get("id") or "")
        if not pid:
            continue
        locs = j.get("locations") or []
        out.append({"pid": pid, "title": j.get("title") or "",
                    "url": META + "/profile/job_details/" + pid,
                    "location": " / ".join(locs),
                    "remote": any("remote" in l.lower() for l in locs),
                    "posted": "", "updated": "", "text": None, "emptype": ""})
    return out


def meta_text(pid):
    r = requests.get(META + "/profile/job_details/" + pid,
                     headers=dict(_NAV, **{"User-Agent": UA}), timeout=TIMEOUT)
    r.raise_for_status()
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S):
        try:
            d = json.loads(html.unescape(block))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            kind = d.get("employmentType") or ""
            parts = [d.get(k) or "" for k in ("description", "responsibilities", "qualifications")]
            parts = [" ".join(p) if isinstance(p, list) else str(p) for p in parts]
            return ("Employment type: %s\n\n" % kind if kind else "") + "\n\n".join(parts)
    return ""


# ── Atlassian (and Loom): one public JSON list behind its careers page
#    robots.txt disallows only the detail pages, not this.

def _plain_html(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def fetch_atlassian():
    r = _session("atlassian").get("https://www.atlassian.com/endpoint/careers/listings", timeout=60)
    r.raise_for_status()
    out = []
    for p in r.json():
        loc = " / ".join(p.get("locations") or [])
        post = p.get("portalJobPost") or {}
        out.append({"pid": str(p["id"]), "title": (p.get("title") or "").strip(),
                    "url": post.get("portalUrl") or "https://www.atlassian.com/company/careers/details/%s" % p["id"],
                    "location": loc, "remote": "remote" in loc.lower(),
                    "posted": (post.get("updatedDate") or "")[:19], "updated": "",
                    "text": _plain_html((p.get("overview") or "") + " " + (p.get("responsibilities") or "")),
                    "emptype": ""})
    return out


# ── Netflix: Eightfold, like Microsoft. Its Workday board
#    (netflix|wd108|Netflix) went empty; this is the search its careers
#    page calls. It answers 429 to a cold call, so the page is opened first.
#    Pages are 10 at most; the list has no description.

NFLX = "https://explore.jobs.netflix.net"


def _netflix_warm(s):
    s.get(NFLX + "/careers", params={"domain": "netflix.com"}, timeout=TIMEOUT)
    s.headers.update({"Accept": "application/json", "Referer": NFLX + "/careers"})


def fetch_netflix():
    _drop("netflix")
    s = _session("netflix", _netflix_warm)
    out, seen = [], set()
    for q in queries():
        start = 0
        while start < 200:
            time.sleep(1)
            r = s.get(NFLX + "/api/apply/v2/jobs", timeout=TIMEOUT, params={
                "domain": "netflix.com", "query": q, "start": start, "num": 10})
            r.raise_for_status()
            data = r.json()
            rows = data.get("positions") or []
            for j in rows:
                pid = str(j.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                locs = j.get("locations") or [j.get("location") or ""]
                out.append({"pid": pid, "title": j.get("posting_name") or j.get("name") or "",
                            "url": j.get("canonicalPositionUrl") or NFLX + "/careers/job/" + pid,
                            "location": " / ".join(l for l in locs if l),
                            "remote": any("remote" in (l or "").lower() for l in locs)
                                      or (j.get("work_location_option") or "") == "remote",
                            "posted": _iso(j.get("t_create")), "updated": "",
                            "text": None, "emptype": ""})
            start += 10
            if len(rows) < 10 or start >= (data.get("count") or 0):
                break
    return out


def netflix_text(pid):
    s = _session("netflix", _netflix_warm)
    time.sleep(1)
    r = s.get(NFLX + "/api/apply/v2/jobs/" + pid, params={"domain": "netflix.com"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("job_description") or ""


# ── TikTok: the keyless search behind lifeattiktok.com. Its
#    SmartRecruiters board ("bytedance") holds only a couple of posts.
#    The list carries the description but no posting date.

def _tiktok_place(city):
    parts, c = [], city or {}
    while c:
        name = c.get("en_name") or c.get("i18n_name") or ""
        if name and name not in parts:
            parts.append(name)
        c = c.get("parent")
    return ", ".join(parts)


def fetch_tiktok():
    s = _session("tiktok")
    out, seen = [], set()
    for q in queries():
        offset = 0
        while offset < 300:
            r = s.post("https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts",
                       timeout=TIMEOUT, headers={"website-path": "tiktok",
                                                 "Origin": "https://lifeattiktok.com",
                                                 "Referer": "https://lifeattiktok.com/"},
                       json={"recruitment_id_list": [], "job_category_id_list": [],
                             "subject_id_list": [], "location_code_list": [],
                             "keyword": q, "limit": 50, "offset": offset})
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, None):
                raise ValueError("TikTok search answered code %s" % data.get("code"))
            d = data.get("data") or {}
            rows = d.get("job_post_list") or []
            for j in rows:
                pid = str(j.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                loc = _tiktok_place(j.get("city_info"))
                out.append({"pid": pid, "title": (j.get("title") or "").strip(),
                            "url": "https://lifeattiktok.com/search/" + pid,
                            "location": loc, "remote": "remote" in loc.lower(),
                            "posted": "", "updated": "",
                            "text": "\n\n".join(x for x in (j.get("description"), j.get("requirement")) if x),
                            "emptype": (j.get("recruit_type") or {}).get("en_name") or ""})
            offset += 50
            if len(rows) < 50 or offset >= (d.get("count") or 0):
                break
    return out


# ── Ford: Oracle Recruiting Cloud. The search behind its careers
#    site answers without a session. A keyword search ranks by relevance and
#    runs on into unrelated jobs, so each query
#    stops at FORD_MAX. The list has no description.

FORD = "https://efds.fa.em5.oraclecloud.com"
FORD_API = FORD + "/hcmRestApi/resources/latest/"
FORD_MAX = 100


def fetch_ford():
    out, seen = [], set()
    for q in queries():
        offset = 0
        while offset < FORD_MAX:
            r = requests.get(FORD_API + "recruitingCEJobRequisitions", timeout=TIMEOUT, params={
                "onlyData": "true", "expand": "requisitionList.secondaryLocations",
                "finder": 'findReqs;siteNumber=CX_1,keyword="%s",limit=50,offset=%d,sortBy=RELEVANCY'
                          % (q, offset)})
            r.raise_for_status()
            items = r.json().get("items") or [{}]
            rows = items[0].get("requisitionList") or []
            for j in rows:
                pid = str(j.get("Id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                locs = [j.get("PrimaryLocation") or ""] + [
                    x.get("Name") or "" for x in j.get("secondaryLocations") or []]
                out.append({"pid": pid, "title": (j.get("Title") or "").strip(),
                            "url": "%s/hcmUI/CandidateExperience/en/sites/CX_1/job/%s" % (FORD, pid),
                            "location": " / ".join(l for l in locs if l),
                            "remote": j.get("WorkplaceTypeCode") == "ORA_REMOTE",
                            "posted": j.get("PostedDate") or "", "updated": "",
                            "text": None, "emptype": ""})
            offset += 50
            if len(rows) < 50:
                break
    return out


def ford_text(pid):
    r = requests.get(FORD_API + "recruitingCEJobRequisitionDetails", timeout=TIMEOUT, params={
        "expand": "all", "onlyData": "true", "finder": 'ById;Id="%s",siteNumber=CX_1' % pid})
    r.raise_for_status()
    d = (r.json().get("items") or [{}])[0]
    return _plain_html(" ".join(d.get(k) or "" for k in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))


# ── PwC: Workday, read here and not as a board. The board reader
#    takes the broad search, and PwC lists well over a thousand hits for it
#    worldwide, most of them not the role you want. Narrow searches on each career
#    site, a second apart, find the real ones. A job can be on two sites, so
#    title and place make it one. The pid carries the site its text is read from.

PWC = "https://pwc.wd3.myworkdayjobs.com"
PWC_SITES = ("US_Experienced_Careers", "Global_Experienced_Careers", "Global_Campus_Careers")


def _pwc_queries():
    """The configured roles; with the example design roles, two more
    narrow design searches."""
    q = queries()
    return q + ("experience design", "ux") if q == EXAMPLE_QUERIES else q
PWC_PAGE = 20
PWC_PAGES = 2


def _pwc_job(site, path):
    r = requests.get("%s/wday/cxs/pwc/%s%s" % (PWC, site, path), timeout=TIMEOUT,
                     headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json().get("jobPostingInfo") or {}


def fetch_pwc():
    out, paths, seen = [], set(), set()
    for site in PWC_SITES:
        for q in _pwc_queries():
            for page in range(PWC_PAGES):
                time.sleep(1)
                r = requests.post("%s/wday/cxs/pwc/%s/jobs" % (PWC, site), timeout=TIMEOUT, json={
                    "appliedFacets": {}, "limit": PWC_PAGE, "offset": page * PWC_PAGE, "searchText": q})
                r.raise_for_status()
                rows = r.json().get("jobPostings") or []
                for j in rows:
                    path, title = j.get("externalPath") or "", (j.get("title") or "").strip()
                    if not path or path in paths:
                        continue
                    paths.add(path)
                    loc = j.get("locationsText") or ""
                    if re.match(r"\d+ locations", loc, re.I):
                        # Only a wanted title is worth the extra call.
                        if not is_wanted_title(title):
                            continue
                        time.sleep(1)
                        info = _pwc_job(site, path)
                        loc = " / ".join(x for x in [info.get("location") or ""]
                                         + list(info.get("additionalLocations") or []) if x)
                    key = (title.lower(), loc.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"pid": site + path, "title": title,
                                "url": "%s/%s%s" % (PWC, site, path), "location": loc,
                                "remote": "remote" in loc.lower(), "posted": "", "updated": "",
                                "text": None, "emptype": ""})
                if len(rows) < PWC_PAGE:
                    break
    return out


def pwc_text(pid):
    site, _, path = pid.partition("/")
    return _plain_html(_pwc_job(site, "/" + path).get("jobDescription") or "")


FETCH = {"amazon": fetch_amazon, "apple": fetch_apple, "microsoft": fetch_microsoft,
         "google": fetch_google, "meta": fetch_meta, "atlassian": fetch_atlassian,
         "netflix": fetch_netflix, "tiktok": fetch_tiktok, "ford": fetch_ford, "pwc": fetch_pwc}
TEXT = {"apple": apple_text, "microsoft": microsoft_text, "meta": meta_text,
        "netflix": netflix_text, "ford": ford_text, "pwc": pwc_text}


def boards():
    """The careers sites, as boards for the poller."""
    return [{"employer": k, "name": n, "ats": "careers", "slug": k, "verified": True,
             "source": "careers site"} for k, n in NAMES.items()]
