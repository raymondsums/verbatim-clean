"""Find each company's public job board.

Who gets checked: the companies in jobs/seed_companies.json (edit it to
list the companies you care about), and your own additions in
data/boards_extra.json.

Greenhouse, Lever and Ashby all serve a keyless JSON feed per company at a
guessable slug. For every company this tries a few slug guesses on each and
keeps a hit only when it really is that company:

- Greenhouse returns the board's own name, which is compared with the
  company name.
- Lever and Ashby return no company name, so the hit counts only if the
  company's name shows up in the text of its postings. A board with no
  postings cannot be checked and is saved as unverified; the poller skips
  unverified boards, and the next run of this script checks them again.
- Boards listed by slug in seed_companies.json are probed directly and
  trusted when they answer.

SmartRecruiters, Rippling and Gem are tried by --more,
only for companies with no board yet. SmartRecruiters answers any name, so
a hit counts only when the company name it returns matches; Rippling's
postings carry the board's company name; Gem is checked like Lever.
--more writes data/boards_more.pending.json, which the poller ignores until
it is renamed to boards_more.json (after a dry run, see jobs.watch --dry).

Companies on Workday, iCIMS or their own site are not found this way. Add
those by hand in data/boards_extra.json. A company you save or apply to
from Verbatim gets its board added there too (add_extra).

    python3 -m jobs.boards              try every company not tried yet
    python3 -m jobs.boards --retry      try them all again
    python3 -m jobs.boards --more       try the newer systems on the rest
    python3 -m jobs.boards --workday    one-time Workday search (see run_workday)
"""

import difflib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from jobs.common import (BOARDS_EXTRA_PATH, BOARDS_PATH, DATA_DIR, JOBS_DIR,
                         UA, compact, is_defense, normalize)

TRIED_PATH = os.path.join(DATA_DIR, "boards_tried.json")
SEEDS_PATH = os.path.join(JOBS_DIR, "seed_companies.json")
# Kept low on purpose: this runs on your own computer.
WORKERS = 4
TIMEOUT = 12

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/%s",
    "lever": "https://api.lever.co/v0/postings/%s?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/%s",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=1",
    "rippling": "https://api.rippling.com/platform/api/ats/v1/board/%s/jobs",
    "gem": "https://api.gem.com/job_board/v0/%s/job_posts/",
}
FIRST = ("greenhouse", "ashby", "lever")
MORE = ("smartrecruiters", "rippling", "gem")
MORE_PATH = os.path.join(DATA_DIR, "boards_more.json")
MORE_PENDING = os.path.join(DATA_DIR, "boards_more.pending.json")
MORE_TRIED = os.path.join(DATA_DIR, "boards_more_tried.json")
WORKDAY_PATH = os.path.join(DATA_DIR, "boards_workday.json")
WORKDAY_PENDING = os.path.join(DATA_DIR, "boards_workday.pending.json")
WORKDAY_TRIED = os.path.join(DATA_DIR, "boards_workday_tried.json")

_local = threading.local()


def session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers["User-Agent"] = UA
        _local.s = s
    return _local.s


# Words that make a slug guess worse when kept ("figma" not "figmainc").
_DROP = {"and", "of", "the", "labs", "lab", "technologies", "technology",
         "software", "systems", "solutions", "services", "platforms",
         "platform", "usa", "us", "international", "global", "group", "hq"}


def slug_guesses(key):
    words = key.split()
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    core = [w for w in words if w not in _DROP]
    if core and core != words:
        out += ["".join(core), "-".join(core)]
    # The first word alone is how most startups name their board ("meta",
    # "block"), but short or common first words collide with other
    # companies, so it is only tried when it is distinctive enough.
    if len(words) > 1 and len(words[0]) >= 5 and words[0] not in _DROP:
        out.append(words[0])
    seen, uniq = set(), []
    for s in out:
        s = re.sub(r"[^a-z0-9-]", "", s)
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:4]


# Words that can be left off a company name without changing which company
# it is: "Robinhood Markets" is Robinhood, "Magic Leap" is not Magic.
_GENERIC = _DROP | {
    "inc", "llc", "corp", "co", "markets", "interactive", "insights",
    "network", "networks", "media", "industries", "companies", "company",
    "app", "apps", "ai", "io", "studio", "studios", "digital", "opco",
    "america", "north", "consolidated", "job", "jobs", "board", "careers",
    "enterprises", "ventures", "worldwide", "partners"}


def names_match(a, b):
    """Board name (or slug) vs employer name, strictly.

    Equal once normalized, or one is the other plus only generic words
    ("Robinhood" vs "Robinhood Markets"). A plain prefix is not enough:
    "National" is not "National Cable Communications".
    """
    ca, cb = compact(a), compact(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    wa = normalize(a).replace("-", " ").split()
    wb = normalize(b).replace("-", " ").split()
    short, long_ = sorted((wa, wb), key=len)
    if short and long_[:len(short)] == short and all(w in _GENERIC for w in long_[len(short):]):
        return True
    # "uberfreight" vs "uber freight": compare glued forms of the core words
    core_a = "".join(w for w in wa if w not in _GENERIC)
    core_b = "".join(w for w in wb if w not in _GENERIC)
    if core_a and core_a == core_b:
        return True
    return difflib.SequenceMatcher(None, ca, cb).ratio() >= 0.9


def _mentions(text, key, strict):
    """Does posting text name this employer? With strict, the full name
    must appear; otherwise the first word is enough for long names."""
    t = (text or "").lower()
    first = key.split()[0] if key else ""
    if len(compact(key)) >= 5 and compact(key) in re.sub(r"[^a-z0-9]", "", t):
        return True
    if not strict and len(first) >= 4 and re.search(r"\b%s\b" % re.escape(first), t):
        return True
    return False


def probe(ats, slug, key, display):
    """One guess. Returns a board dict on a hit, None on a miss."""
    try:
        r = session().get(URLS[ats] % slug, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    hit = {"employer": key, "name": display, "ats": ats, "slug": slug,
           "verified": False, "board_name": "", "jobs": 0}
    if ats == "greenhouse":
        hit["board_name"] = data.get("name") or ""
        hit["verified"] = names_match(hit["board_name"], key)
        if not hit["verified"]:
            return None
        return hit
    if ats == "smartrecruiters":
        first = (data.get("content") or [{}])[0] if data.get("totalFound") else {}
        hit["board_name"] = (first.get("company") or {}).get("name") or ""
        hit["jobs"] = data.get("totalFound") or 0
        hit["verified"] = names_match(hit["board_name"], key)
        return hit if hit["verified"] else None
    if ats == "rippling":
        if not isinstance(data, list):
            return None
        hit["jobs"] = len(data)
        if not data:
            return hit
        try:
            one = session().get(URLS[ats] % slug + "/" + data[0]["uuid"], timeout=TIMEOUT).json()
        except (requests.RequestException, ValueError, KeyError):
            return None
        hit["board_name"] = one.get("companyName") or (one.get("board") or {}).get("companyName") or ""
        hit["verified"] = names_match(hit["board_name"], key)
        return hit if hit["verified"] else None
    postings = data if ats in ("lever", "gem") else (data.get("jobs") or [])
    if not isinstance(postings, list):
        return None
    hit["jobs"] = len(postings)
    for p in postings[:40]:
        text = " ".join(str(p.get(k) or "") for k in
                        ("descriptionPlain", "additionalPlain", "openingPlain",
                         "content_plain"))
        if _mentions(text, key, strict=not names_match(slug, key)):
            hit["verified"] = True
            hit["text_verified"] = not names_match(slug, key)
            break
    if not hit["verified"] and postings:
        # Postings exist and none name the employer: a slug collision.
        return None
    return hit


def find(key, info, systems=FIRST):
    for slug in slug_guesses(key):
        for ats in systems:
            hit = probe(ats, slug, key, info.get("name") or key)
            if hit and hit["verified"]:
                return hit
            if hit:
                pending = hit
                # Keep looking for a verified one before settling.
                for ats2 in systems:
                    if ats2 == ats:
                        continue
                    h2 = probe(ats2, slug, key, info.get("name") or key)
                    if h2 and h2["verified"]:
                        return h2
                return pending
    return None


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def ensure_extra():
    if os.path.exists(BOARDS_EXTRA_PATH):
        return
    _save(BOARDS_EXTRA_PATH, {
        "_how": ("Add companies the automatic search missed. One entry per "
                 "board. ats is greenhouse, lever, ashby or workable; slug is "
                 "the part of the board URL after the domain, for example "
                 "job-boards.greenhouse.io/figma has slug figma. For workday "
                 "the slug is tenant|wdN|site from "
                 "tenant.wdN.myworkdayjobs.com/site. employer is the company "
                 "name. Jobs you save or apply to add their board here. "
                 "smartrecruiters, rippling and gem work too."),
        "_example": {"employer": "Figma", "ats": "greenhouse", "slug": "figma"},
        "boards": [],
    })


# A posting link, read as the board it came from. Workday's slug packs the
# three parts of tenant.wdN.myworkdayjobs.com/site (see watch.fetch_workday).
_BOARD_LINKS = [
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?(?!embed\b)([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([^/?#]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([^/?#]+)/j/", re.I)),
    ("workday", re.compile(r"//([a-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/"
                           r"(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/job/", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/jobs", re.I)),
    ("gem", re.compile(r"jobs\.gem\.com/([^/?#]+)", re.I)),
]


def board_from_url(url):
    """(ats, slug) for a posting link on a board the poller can read, else None."""
    for ats, rx in _BOARD_LINKS:
        m = rx.search(url or "")
        if m:
            return ats, "|".join(m.groups()) if ats == "workday" else m.group(1)
    return None


def add_extra(company, url, found=None, src="applied"):
    """Watch the board a posting came from, when it is not watched yet.

    Called when the user saves or applies to a job from anywhere, so a company
    the user chose once keeps showing up on the job board. True when added.
    found is (ats, slug) when the board is already known.
    """
    found = found or board_from_url(url)
    if not found or not (company or "").strip() or is_defense(company, ""):
        return False
    ats, slug = found
    if any((b["ats"], b["slug"].lower()) == (ats, slug.lower()) for b in all_boards()):
        return False
    ensure_extra()
    data = _load(BOARDS_EXTRA_PATH, {})
    data.setdefault("boards", []).append(
        {"employer": company.strip(), "ats": ats, "slug": slug,
         "added": time.strftime("%Y-%m-%d"), "from": src})
    _save(BOARDS_EXTRA_PATH, data)
    return True


def candidates():
    """Every company worth checking: key -> {name, hires, source, known}.

    hires is kept for the board files' shape and is always 0 here. It only
    ever ordered the work; nothing is scored on it.
    """
    out = {}
    seeds = _load(SEEDS_PATH, {})
    for n in seeds.get("names", []):
        k = normalize(n)
        if k and k not in out:
            out[k] = {"name": n, "hires": 0, "source": "seed list", "known": None}
    for b in seeds.get("boards", []):
        k = normalize(b["name"])
        if not k:
            continue
        info = out.setdefault(k, {"name": b["name"], "hires": 0,
                                  "source": "seed list", "known": None})
        info["known"] = (b["ats"], b["slug"])
    return out


def probe_known(ats, slug, key, display):
    """A board listed by slug. Trusted when it answers with a real feed."""
    try:
        r = session().get(URLS[ats] % slug, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    hit = {"employer": key, "name": display, "ats": ats, "slug": slug,
           "verified": True, "board_name": "", "jobs": 0}
    if ats == "greenhouse":
        hit["board_name"] = data.get("name") or ""
    return hit


# Workday hosts each company at tenant.wdN.myworkdayjobs.com. A wrong host
# answers 422; a real one serves a robots.txt naming its job sites, so one
# request per guess finds both the host and the site.
WD_HOSTS = ("wd1", "wd5", "wd3", "wd12", "wd10", "wd2", "wd103", "wd108", "wd501", "wd101")
_WD_SITE = re.compile(r"^Allow:\s*/([A-Za-z0-9_-]+)/", re.M)
_WD_SKIP = re.compile(r"intern|grad|campus|universit|student|early|alumni|internal|employee|"
                      r"referral|contingent|temp|agency|test|sandbox", re.I)


def workday_board(tenant):
    """(slug, jobs) for a tenant's main external site, or None."""
    for wd in WD_HOSTS:
        host = "https://%s.%s.myworkdayjobs.com" % (tenant, wd)
        try:
            r = session().get(host + "/robots.txt", timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        for site in _WD_SITE.findall(r.text):
            if _WD_SKIP.search(site):
                continue
            try:
                j = session().post("%s/wday/cxs/%s/%s/jobs" % (host, tenant, site), timeout=TIMEOUT,
                                   json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                         "searchText": ""}).json()
            except (requests.RequestException, ValueError):
                continue
            if j.get("total"):
                return "%s|%s|%s" % (tenant, wd, site), j["total"]
        return None
    return None


def find_workday(key, info=None):
    """A verified Workday board for this company, or None. Only the full
    name is tried as the tenant (no first-word guess), so another company
    with a shorter name is not picked up."""
    words = [w for w in key.split() if w not in _GENERIC] or key.split()
    for tenant in dict.fromkeys(re.sub(r"[^a-z0-9]", "", g) for g in
                                ("".join(key.split()), "".join(words))):
        if len(tenant) < 3:
            continue
        found = workday_board(tenant)
        if found:
            return {"employer": key, "name": (info or {}).get("name") or key, "ats": "workday",
                    "slug": found[0], "verified": True, "board_name": "", "jobs": found[1]}
    return None


def run_workday(limit=None):
    """One-time Workday search over the company list, two at a time. Writes boards_workday.pending.json, which
    the poller ignores until it is renamed boards_workday.json."""
    emp = candidates()
    have = {b["employer"] for b in all_boards()}
    tried = _load(WORKDAY_TRIED, {})
    found = {b["employer"]: b for b in _load(WORKDAY_PENDING, {}).get("boards", [])}
    todo = sorted(((k, v) for k, v in emp.items() if k not in have and k not in tried),
                  key=lambda kv: -kv[1]["hires"])[:limit]
    print("%d companies to try on Workday" % len(todo), flush=True)
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(find_workday, k, v): k for k, v in todo}
        for f in as_completed(futs):
            k = futs[f]
            try:
                hit = f.result()
            except Exception:
                hit = None
            tried[k] = time.strftime("%Y-%m-%d")
            if hit and not is_defense(hit["name"], ""):
                hit["hires"], hit["source"] = emp[k]["hires"], emp[k]["source"]
                found[k] = hit
            done += 1
            if done % 100 == 0:
                print("  %d/%d, %d boards, %.0fs" % (done, len(todo), len(found),
                                                    time.time() - t0), flush=True)
                _save(WORKDAY_TRIED, tried)
                _save(WORKDAY_PENDING, {"boards": list(found.values())})
    _save(WORKDAY_TRIED, tried)
    _save(WORKDAY_PENDING, {"built": time.strftime("%Y-%m-%d %H:%M"),
                            "boards": sorted(found.values(), key=lambda b: -b.get("hires", 0))})
    print("took %.0fs, %d Workday boards" % (time.time() - t0, len(found)))


def find_any(key, info):
    if info.get("known"):
        hit = probe_known(info["known"][0], info["known"][1], key, info["name"])
        if hit:
            return hit
    return find(key, info)


def run_more():
    """Try SmartRecruiters, Rippling and Gem for every company with no
    board yet. Writes boards_more.pending.json; nothing is polled until it
    is renamed to boards_more.json."""
    emp = candidates()
    have = {b["employer"] for b in all_boards()}
    tried = _load(MORE_TRIED, {})
    found = {b["employer"]: b for b in _load(MORE_PENDING, {}).get("boards", [])}
    todo = [(k, v) for k, v in emp.items() if k not in have and k not in tried]
    print("%d companies without a board, %d to try" % (len(emp) - len(have), len(todo)),
          flush=True)
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(find, k, v, MORE): k for k, v in todo}
        for f in as_completed(futs):
            k = futs[f]
            try:
                hit = f.result()
            except Exception:
                hit = None
            tried[k] = time.strftime("%Y-%m-%d")
            if hit:
                hit["hires"], hit["source"] = emp[k]["hires"], emp[k]["source"]
                found[k] = hit
            done += 1
            if done % 200 == 0:
                print("  %d/%d, %d boards, %.0fs" % (done, len(todo), len(found),
                                                    time.time() - t0), flush=True)
                _save(MORE_TRIED, tried)
                _save(MORE_PENDING, {"boards": list(found.values())})
    _save(MORE_TRIED, tried)
    boards = tighten(found.values())
    _save(MORE_PENDING, {"built": time.strftime("%Y-%m-%d %H:%M"), "boards": boards})
    print("took %.0fs" % (time.time() - t0))
    report(emp, boards, MORE)


def run(retry=False):
    ensure_extra()
    emp = candidates()
    if not emp:
        sys.exit("No companies to check. Add some to jobs/seed_companies.json.")
    tried = {} if retry else _load(TRIED_PATH, {})
    found = {} if retry else {b["employer"]: b for b in _load(BOARDS_PATH, {}).get("boards", [])}
    # Unverified boards get tried again every run; they may have postings now.
    todo = [(k, v) for k, v in emp.items()
            if k not in tried or (k in found and not found[k]["verified"])]
    print("%d companies, %d to try" % (len(emp), len(todo)), flush=True)
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(find_any, k, v): k for k, v in todo}
        for f in as_completed(futs):
            k = futs[f]
            try:
                hit = f.result()
            except Exception:
                hit = None
            tried[k] = time.strftime("%Y-%m-%d")
            if hit:
                hit["hires"], hit["source"] = emp[k]["hires"], emp[k]["source"]
                found[k] = hit
            elif k in found:
                del found[k]
            done += 1
            if done % 200 == 0:
                print("  %d/%d, %d boards, %.0fs" % (done, len(todo), len(found),
                                                    time.time() - t0), flush=True)
                _save(TRIED_PATH, tried)
    _save(TRIED_PATH, tried)
    boards = tighten(found.values())
    _save(BOARDS_PATH, {"built": time.strftime("%Y-%m-%d %H:%M"), "boards": boards})
    print("took %.0fs" % (time.time() - t0))
    report(emp, boards)


def tighten(boards):
    """One company per board, and only names that really match.

    A board the name check no longer accepts is dropped. When several
    employers landed on the same board, the closest name keeps it.
    """
    keep = {}
    for b in boards:
        if b["verified"]:
            if b["ats"] in ("smartrecruiters", "rippling"):
                ok = names_match(b.get("board_name") or "", b["employer"])
            elif b["ats"] == "greenhouse":
                ok = names_match(b.get("board_name") or b["slug"], b["employer"]) \
                    or b.get("source") == "seed list" and names_match(b["slug"], b["employer"])
            else:
                ok = names_match(b["slug"], b["employer"]) or b.get("text_verified")
            if not ok:
                continue
        k = (b["ats"], b["slug"])
        rank = (compact(b.get("board_name") or b["slug"]) == compact(b["employer"]),
                b.get("hires", 0))
        if k not in keep or rank > keep[k][0]:
            keep[k] = (rank, b)
    return sorted((b for _, b in keep.values()),
                  key=lambda b: (-b.get("hires", 0), b["employer"]))


def report(emp, boards, systems=FIRST):
    """Hit rate per ATS, split by where the company came from."""
    by_src = {}
    for k, v in emp.items():
        by_src.setdefault(v["source"], {"tried": 0})["tried"] += 1
    for b in boards:
        if not b["verified"]:
            continue
        src = by_src.setdefault(b.get("source", "?"), {"tried": 0})
        src[b["ats"]] = src.get(b["ats"], 0) + 1
    unver = sum(1 for b in boards if not b["verified"])
    ver = len(boards) - unver
    print("tried %d companies: %d verified boards (%.1f%%), %d unverified"
          % (len(emp), ver, 100.0 * ver / max(len(emp), 1), unver))
    for src, c in sorted(by_src.items()):
        t = max(c["tried"], 1)
        parts = ", ".join("%s %d (%.1f%%)" % (a, c.get(a, 0), 100.0 * c.get(a, 0) / t)
                          for a in systems)
        print("  %-15s %5d tried: %s" % (src, c["tried"], parts))


def all_boards():
    """Verified boards plus the user's hand-added ones, for the poller."""
    out = [b for b in _load(BOARDS_PATH, {}).get("boards", []) if b.get("verified")]
    out += [b for b in _load(MORE_PATH, {}).get("boards", []) if b.get("verified")]
    out += [b for b in _load(WORKDAY_PATH, {}).get("boards", []) if b.get("verified")]
    have = {(b["ats"], b["slug"]) for b in out}
    for b in _load(BOARDS_EXTRA_PATH, {}).get("boards", []):
        if not b.get("ats") or not b.get("slug"):
            continue
        if (b["ats"], b["slug"]) in have:
            continue
        key = normalize(b.get("employer") or b["slug"])
        out.append({"employer": key, "name": b.get("employer") or b["slug"],
                    "ats": b["ats"], "slug": b["slug"], "verified": True,
                    "extra": True, "source": "your list"})
    return out


if __name__ == "__main__":
    if "--more" in sys.argv:
        run_more()
    elif "--workday" in sys.argv:
        run_workday()
    else:
        run(retry="--retry" in sys.argv)
