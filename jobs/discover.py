"""Monthly board discovery (off unless "discover_boards": true in
jobs/config.json).

Finds more Ashby, Greenhouse and Lever boards to poll directly, from:
- VC portfolio job pages that may be read: Greylock's (WordPress, robots
  allows it) and a16z's (robots allows it). Getro and Consider boards are
  not read here: Getro's API disallows robots and Consider's terms ban
  scrapers, however rarely. Index's search needs a login, so not that either.
- The YC company directory (the public yc-oss JSON), for hiring companies
  with 8 or more people in the US or remote.
- VC portfolio company lists: Accel and Khosla, whose robots.txt and terms
  allow it, and Sequoia and General Catalyst, whose terms allow personal
  use. Redpoint, SV Angel, Conviction and Makers
  Fund have no terms against it; Coatue allows personal use. Lightspeed,
  Lux, Kleiner, Bessemer, First Round, Initialized, NFX, Insight, Battery,
  CRV, Felicis, Bitkraft, Index and a16z's list stay out (their terms ban
  scraping or copying).
  These sites are read gently (_polite): robots.txt checked per host, an
  honest user agent, one page at a time with a pause, and company pages
  cached in data/vc_portfolio_cache.json (names and domains only) so a
  month later only new companies are fetched. A 403 or 429 stops that site
  for the pass and is logged; nothing tries to get around it.

A found board is added to data/boards_extra.json only when it answers with
postings, its company passes the defense filter (when that is on), and it
is not polled yet.
A slug guessed from a company name is kept only when the postings name
the company's own domain. Jobs a board shares with the YC jobs page are
deduped by the watcher (feeds.DUPES).

Run by the watcher at most once every DAYS days, in the background:
    python3 -m jobs.discover          # due check, then a full pass
    python3 -m jobs.discover --now    # skip the due check
"""

import html
import json
import os
import re
import sys
import time

import requests
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

from jobs import boards as B
from jobs.common import DATA_DIR, is_defense, normalize

DAYS = 30
STATE_PATH = os.path.join(DATA_DIR, "discover_state.json")
LOG_PATH = os.path.join(os.path.dirname(DATA_DIR), "logs", "discover.log")
SYSTEMS = ("ashby", "greenhouse", "lever")
YC_HIRING = "https://yc-oss.github.io/api/companies/hiring.json"
GREYLOCK = "https://greylock.com/jobs/portfolio-jobs/?search=%s"
A16Z = "https://jobs.a16z.com/jobs?q=%s"
EXAMPLE_QUERIES = ("designer", "product designer", "ux designer", "design lead")


def queries():
    from jobs.common import load_config
    roles = [r for r in load_config().get("roles") or [] if isinstance(r, str) and r.strip()]
    return tuple(roles[:4]) or EXAMPLE_QUERIES
_LINK = re.compile(r'href="(https?://[^"]+)"')
_US = re.compile(r"san francisco|bay area|\bca\b|california|remote|\bus\b|usa|united states|new york", re.I)
WORKERS = 4


def due():
    try:
        with open(STATE_PATH) as f:
            last = json.load(f).get("last", 0)
    except (OSError, ValueError):
        last = 0
    return time.time() - last >= DAYS * 86400


def _page(url):
    r = B.session().get(url, timeout=B.TIMEOUT, headers={"Accept": "text/html"})
    r.raise_for_status()
    return r.text


def vc_links(log=print):
    """(ats, slug) for every Ashby, Greenhouse or Lever link on the pages."""
    found = set()
    for base in (GREYLOCK, A16Z):
        for q in queries():
            try:
                text = _page(base % quote(q))
            except Exception as e:
                log("  %s skipped (%s)" % (urlparse(base).netloc, type(e).__name__))
                break
            for link in _LINK.findall(text):
                hit = B.board_from_url(link)
                if hit and hit[0] in SYSTEMS:
                    found.add((hit[0], hit[1].lower()))
    return found


def _texts(ats, slug):
    """Posting texts and links for one board, for the domain check."""
    if ats == "greenhouse":
        data = B.session().get("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug,
                               timeout=B.TIMEOUT).json().get("jobs") or []
        return [(j.get("content") or "") + " " + (j.get("absolute_url") or "") for j in data]
    if ats == "ashby":
        data = B.session().get(B.URLS["ashby"] % slug, timeout=B.TIMEOUT).json().get("jobs") or []
        return [(j.get("descriptionPlain") or "") + " " + (j.get("applyUrl") or "") for j in data]
    data = B.session().get(B.URLS["lever"] % slug, timeout=B.TIMEOUT).json()
    return [(j.get("descriptionPlain") or "") + " " + (j.get("additionalPlain") or "")
            for j in (data if isinstance(data, list) else [])]


def _domain(site):
    host = urlparse(site or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def yc_board(c):
    """A verified board for one YC company, or None."""
    w = c.get("website") or ""
    domain = _domain(w if "//" in w else "https://" + w)
    if not domain:
        return None
    name = (c.get("name") or "").lower()
    guesses = dict.fromkeys(g for g in (c.get("slug"), (c.get("slug") or "").replace("-", ""),
                                        re.sub(r"[^a-z0-9]", "", name), re.sub(r"[^a-z0-9]+", "-", name).strip("-"),
                                        domain.split(".")[0]) if g)
    for slug in guesses:
        for ats in SYSTEMS:
            try:
                texts = _texts(ats, slug)
            except Exception:
                continue
            if not texts:
                continue
            if any(domain in t.lower() for t in texts):
                return ats, slug
            break  # a live board that never names the domain is someone else's
    return None


HONEST_UA = "VerbatimJobWatcher/1.0 (one person's job search; monthly; names and domains only)"
CACHE_PATH = os.path.join(DATA_DIR, "vc_portfolio_cache.json")
PAUSE = 1.0
_robots = {}


class Blocked(Exception):
    pass


def _polite(url):
    """One GET as an honest bot: robots.txt first, a pause after, and a
    403 or 429 raised as Blocked so the site's pass stops there."""
    import urllib.robotparser
    host = "{0.scheme}://{0.netloc}".format(urlparse(url))
    rp = _robots.get(host)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
        r = requests.get(host + "/robots.txt", headers={"User-Agent": HONEST_UA}, timeout=B.TIMEOUT)
        if r.status_code in (401, 403):
            rp.disallow_all = True
        elif r.status_code >= 400:
            rp.allow_all = True
        else:
            rp.parse(r.text.splitlines())
        _robots[host] = rp
    if not rp.can_fetch(HONEST_UA, url):
        raise Blocked("robots.txt disallows %s" % url)
    r = requests.get(url, headers={"User-Agent": HONEST_UA}, timeout=B.TIMEOUT)
    time.sleep(PAUSE)
    if r.status_code in (403, 429):
        raise Blocked("%s answered %d" % (host, r.status_code))
    r.raise_for_status()
    return r.text


def _portfolio(site, sitemap, path_rx, read_page):
    """Company pages from a sitemap, each read once and cached."""
    cache = B._load(CACHE_PATH, {})
    seen = cache.setdefault(site, {})
    urls = list(dict.fromkeys(re.findall(path_rx, _polite(sitemap))))
    try:
        for u in urls:
            if u in seen:
                continue
            got = read_page(_polite(u))
            seen[u] = list(got) if got else None
    finally:
        B._save(CACHE_PATH, cache)
    return [{"name": v[0], "website": v[1]} for u, v in seen.items() if v and u in set(urls)]


def _accel_page(h):
    name = re.search(r'property="og:title" content="([^"]*)"', h)
    site = re.search(r'aria-label="Website" href="([^"]+)"', h)
    return name and site and (html.unescape(name.group(1)), _domain(html.unescape(site.group(1))))


def accel_companies():
    return _portfolio("accel", "https://www.accel.com/sitemap.xml",
                      r"<loc>(https://(?:www\.)?accel\.com/companies/[^<]+)</loc>", _accel_page)


_DOMAIN_TEXT = re.compile(r">\s*((?:[a-z0-9-]+\.)+[a-z]{2,})\s*<", re.I)


def _sequoia_page(h):
    name = re.search(r"<title>([^<|]+)", h)
    dom = next((d for d in _DOMAIN_TEXT.findall(h) if d.lower() not in ("domain.com", "sequoiacap.com")), None)
    return name and dom and (html.unescape(name.group(1)).strip(), dom.lower())


def sequoia_companies():
    return _portfolio("sequoia", "https://sequoiacap.com/sitemap.xml",
                      r"<loc>(https://sequoiacap\.com/companies/[^<]+)</loc>", _sequoia_page)


def _gc_page(h):
    name = re.search(r"<h1[^>]*>([^<]+)", h)
    site = re.search(r'href="([^"]+)" class="portfolio_link-block">Website', h)
    return name and site and (html.unescape(name.group(1)).strip(), _domain(site.group(1)))


def gc_companies():
    return _portfolio("general catalyst", "https://www.generalcatalyst.com/sitemap.xml",
                      r"<loc>(https://www\.generalcatalyst\.com/companies/[^<]+)</loc>", _gc_page)


_KHOSLA_CARD = re.compile(r'href="([^"]*)"[^>]*class="company-card-wrapper[^"]*".*?'
                          r'class="back-company-title">([^<]*)', re.S)


def khosla_companies():
    base = "https://www.khoslaventures.com"
    urls = [base + "/portfolio"] + re.findall(r"<loc>(https://www\.khoslaventures\.com/category/[^<]+)</loc>",
                                              _polite(base + "/sitemap.xml"))
    out = {}
    for u in urls:
        for site, name in _KHOSLA_CARD.findall(_polite(u)):
            if site.startswith("http"):
                out.setdefault(html.unescape(name.strip()), site)
    return [{"name": n, "website": w} for n, w in out.items()]


def _next_data(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    return json.loads(m.group(1)) if m else {}


def _pairs(rows):
    return [{"name": html.unescape(n).strip(), "website": (w or "").strip()} for n, w in rows if n and w]


def redpoint_companies():
    d = json.loads(_polite("https://www.redpoint.com/page-data/companies/page-data.json"))
    return _pairs((e["node"].get("title"), e["node"].get("link"))
                  for e in d["result"]["data"]["companies"]["edges"])


def svangel_companies():
    d = _next_data(_polite("https://svangel.com/portfolio"))
    items = d["props"]["pageProps"]["data"]["investmentsListCollection"]["items"]
    return _pairs((c.get("internalTitle"), c.get("url")) for c in items)


def conviction_companies():
    page = _polite("https://www.conviction.com/")
    sec = re.search(r"<h2>\s*Portfolio\s*</h2>(.*?)<h2>", page, re.S)
    rows = []
    for li in re.findall(r"<li>(.*?)</li>", sec.group(1) if sec else "", re.S):
        m = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
        if m:
            rows.append((re.sub(r"<[^>]+>", "", m.group(2)), m.group(1)))
    return _pairs(rows)


def makersfund_companies():
    """Makers Fund, games: the list, then one small JSON per company (cached)."""
    base = "https://www.makersfund.com"
    cache = B._load(CACHE_PATH, {})
    seen = cache.setdefault("makers fund", {})
    try:
        for c in _next_data(_polite(base + "/portfolio"))["props"]["pageProps"]["companies"]:
            if c["slug"] in seen:
                continue
            site = json.loads(_polite(base + "/public_cms/projects/%s.json" % c["slug"])).get("website")
            seen[c["slug"]] = [c["title"], site] if site else None
    finally:
        B._save(CACHE_PATH, cache)
    return _pairs(v for v in seen.values() if v)


def coatue_companies():
    """Coatue, personal-use terms like Sequoia's: the page, then its own "Load more" route, 48 at a time."""
    base = "https://www.coatue.com"
    d = _next_data(_polite(base + "/portfolio"))
    grid = next(x for x in d["props"]["pageProps"]["page"]["sectionsCollection"]["items"]
                if x.get("__typename") == "PortfolioGrid")
    items, total = list(grid["itemsCollection"]["items"]), grid["itemsCollection"]["total"]
    while len(items) < total:
        more = json.loads(_polite(base + "/api/portfolio?id=%s&skip=%d" % (grid["sys"]["id"], len(items))))
        if not more.get("items"):
            break
        items += more["items"]
    return _pairs((i.get("name"), i.get("url")) for i in items)


def yc_companies():
    rows = B.session().get(YC_HIRING, timeout=60).json()
    return [c for c in rows if (c.get("team_size") or 0) >= 8
            and (_US.search(c.get("all_locations") or "")
                 or any(r in ("United States of America", "America / Canada", "Remote", "Fully Remote")
                        for r in c.get("regions") or []))]


def run(log=print, seed=(), only=None):
    """seed: (company, posting link) pairs found some other way. only: the
    names of the company lists to read this time (all when None)."""
    t0 = time.time()
    have = {(b["ats"], b["slug"].lower()) for b in B.all_boards()}
    names = {normalize(b.get("name") or b["employer"]) for b in B.all_boards()}
    add = {}  # (ats, slug) -> (company, source)

    for ats, slug in (vc_links(log) if not only else ()):
        if (ats, slug) not in have:
            add[(ats, slug)] = (slug, "vc pages")
    for company, link in seed:
        hit = B.board_from_url(link)
        if hit and hit[0] in SYSTEMS and (hit[0], hit[1].lower()) not in have:
            add[(hit[0], hit[1].lower())] = (company, "vc boards, once")

    lists = [("yc directory", yc_companies), ("accel portfolio", accel_companies),
             ("khosla portfolio", khosla_companies), ("sequoia portfolio", sequoia_companies),
             ("general catalyst portfolio", gc_companies), ("redpoint portfolio", redpoint_companies),
             ("sv angel portfolio", svangel_companies), ("conviction portfolio", conviction_companies),
             ("makers fund portfolio", makersfund_companies), ("coatue portfolio", coatue_companies)]
    if only:
        lists = [(src, read) for src, read in lists if src in only]
    for src, read in lists:
        try:
            rows = read()
        except Blocked as e:
            log("  %s STOPPED: %s" % (src, e))
            continue
        except Exception as e:
            log("  %s skipped (%s)" % (src, type(e).__name__))
            continue
        todo = [c for c in rows if normalize(c.get("name")) not in names and not is_defense(c.get("name"), "")]
        log("  %s: %d companies to check" % (src, len(todo)))
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for c, hit in zip(todo, pool.map(yc_board, todo)):
                if hit and (hit[0], hit[1].lower()) not in have and (hit[0], hit[1].lower()) not in add:
                    add[(hit[0], hit[1].lower())] = (c.get("name") or hit[1], src)

    B.ensure_extra()
    data = B._load(B.BOARDS_EXTRA_PATH, {})
    added = []
    for (ats, slug), (company, src) in sorted(add.items()):
        hit = B.probe_known(ats, slug, normalize(company), company)
        if not hit:
            continue
        shown = hit.get("board_name") or company
        if is_defense(shown, "") or is_defense(company, ""):
            log("  skipped %s (defense)" % shown)
            continue
        data.setdefault("boards", []).append({"employer": shown, "ats": ats, "slug": slug,
                                              "added": time.strftime("%Y-%m-%d"), "from": src})
        added.append((shown, ats, slug, src))
    B._save(B.BOARDS_EXTRA_PATH, data)
    B._save(STATE_PATH, {"last": time.time(), "added": len(added)})
    log("%s  discovery: %d boards added in %.0fs" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                     len(added), time.time() - t0))
    for a in added:
        log("  + %s (%s:%s, %s)" % a)
    return added


if __name__ == "__main__":
    if "--now" in sys.argv or due():
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            run(log=lambda s: (f.write(s + "\n"), f.flush()))
