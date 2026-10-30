"""Numbers about the applications, for the Stats page.

Everything comes from data/tracker.sqlite, plus the job board's own record
(data/jobs.sqlite) for where a posting was found: velocity, the funnel,
response and rejection rates, time to an answer, active of total,
applications a day, rejection with where it happens, response by source,
and a pace chart.

Small numbers are kept honest: every rate comes with its count, a rate is
only given once a group has MIN_RATE rows, a median once it has
MIN_MEDIAN dated rows, and a trend only when both four-week windows hold
MIN_TREND applications between them. Saved rows are not applications and
stay out of everything except the first funnel step.
"""

import sqlite3
import statistics
from datetime import date, timedelta

import tracker
from jobs.common import seniority

MIN_RATE = 5
MIN_MEDIAN = 5
MIN_TREND = 10
WEEKS = 12
PACE_DAYS = 14
TREND_DAYS = 56

REPLIED = set(tracker.STAGES[1:]) | {tracker.REJECTED}
LEVEL_NAMES = {"junior": "Junior", "mid": "Mid level", "senior": "Senior", "staff": "Staff",
               "lead": "Lead", "principal": "Principal", "manager": "Manager", "head": "Head of",
               "director": "Director", "vp": "VP"}
FEED_NAMES = {"dribbble": "Dribbble", "himalayas": "Himalayas", "serpapi": "Google Jobs",
              "wwr": "We Work Remotely", "yc": "YC jobs", "hn": "Hacker News", "careers": "Careers sites"}


def _day(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _reached(r):
    """How far a row got: an index into tracker.STAGES."""
    s = r["status"]
    if s in tracker.STAGES:
        return tracker.STAGES.index(s)
    if s == tracker.REJECTED and r.get("rejected_at") in tracker.STAGES:
        return tracker.STAGES.index(r["rejected_at"])
    return 0


def _rate(n, of):
    return {"n": n, "of": of, "pct": round(100 * n / of) if of >= MIN_RATE else None}


def _median(days):
    days = [d for d in days if d is not None and d >= 0]
    return {"n": len(days), "days": statistics.median(days) if len(days) >= MIN_MEDIAN else None}


def _postings(rows):
    """job_id -> (ats, first_seen, title) from the job board's record."""
    ids = [r["job_id"] for r in rows if r.get("job_id")]
    if not ids:
        return {}
    try:
        from jobs import watch
        con = sqlite3.connect("file:%s?mode=ro" % watch.DB_PATH, uri=True, timeout=30)
        got = {jid: (ats, first, title) for jid, ats, first, title in con.execute(
            "SELECT id, ats, first_seen, title FROM jobs WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
        con.close()
        return got
    except sqlite3.Error:
        return {}


def _found(rows, posts):
    """job_id -> where the watcher found it, for postings it saw before the user applied."""
    out = {}
    for r in rows:
        hit = posts.get(r.get("job_id"))
        if hit and _day(hit[1]) and _day(r["date"]) and _day(hit[1]) <= _day(r["date"]):
            out[r["job_id"]] = "Job board: " + FEED_NAMES.get(hit[0], "company boards")
    return out


def _group(rows, key):
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = []
    for name, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        out.append({"name": name, "sent": n,
                    "replied": _rate(sum(r["status"] in REPLIED for r in rs), n),
                    "screen": _rate(sum(_reached(r) >= 1 for r in rs), n),
                    "rejected": _rate(sum(r["status"] == tracker.REJECTED for r in rs), n)})
    return out


def compute(rows, today=None):
    today = today or date.today()
    saved = [r for r in rows if r["status"] == tracker.SAVED]
    sent = [r for r in rows if r["status"] != tracker.SAVED and _day(r["date"])]
    total = len(sent)
    by = lambda s: sum(r["status"] == s for r in sent)
    rejected, silent, waiting = by(tracker.REJECTED), by(tracker.NO_RESPONSE), by("Applied")
    replied = sum(r["status"] in REPLIED for r in sent)
    active = total - rejected - silent - by("Offer")

    # Velocity: applications per week, Monday to Sunday, newest last.
    monday = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(WEEKS - 1, -1, -1):
        start = monday - timedelta(weeks=i)
        n = sum(start <= _day(r["date"]) < start + timedelta(days=7) for r in sent)
        weeks.append({"start": start.isoformat(), "n": n, "current": i == 0})
    last4 = sum(today - timedelta(days=28) < _day(r["date"]) <= today for r in sent)
    prev4 = sum(today - timedelta(days=56) < _day(r["date"]) <= today - timedelta(days=28) for r in sent)
    trend = {"last4": last4, "prev4": prev4, "per_week": round(last4 / 4, 1),
             "prev_per_week": round(prev4 / 4, 1), "enough": last4 + prev4 >= MIN_TREND}

    # The last fortnight, day by day: sent, and how
    # many of those are still in play.
    pace = []
    for i in range(PACE_DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        day_rows = [r for r in sent if _day(r["date"]) == d]
        pace.append({"day": d.isoformat(), "n": len(day_rows),
                     "alive": sum(r["status"] not in tracker.CLOSED for r in day_rows)})
    pace_n = sum(p["n"] for p in pace)
    # And the trend: the same "a day" figure over the 14 days ending
    # on each of the last 8 weeks of days, so the line ends where the
    # headline does.
    per_day = {}
    for r in sent:
        per_day[_day(r["date"])] = per_day.get(_day(r["date"]), 0) + 1
    rolling = []
    for i in range(TREND_DAYS - 1, -1, -1):
        end = today - timedelta(days=i)
        n = sum(per_day.get(end - timedelta(days=k), 0) for k in range(PACE_DAYS))
        rolling.append({"day": end.isoformat(), "v": round(n / PACE_DAYS, 2)})

    funnel = [{"stage": "Saved", "n": len(saved) + total}]
    for i, stage in enumerate(tracker.STAGES):
        funnel.append({"stage": stage, "n": sum(_reached(r) >= i for r in sent)})

    rejected_at = {}
    for r in sent:
        if r["status"] == tracker.REJECTED:
            s = tracker.STAGES[_reached(r)]
            rejected_at[s] = rejected_at.get(s, 0) + 1

    def answered_on(r):
        if r["status"] == tracker.REJECTED:
            return _day(r.get("rejected_on")) or _day(r.get("last_contact"))
        if r["status"] in REPLIED:
            return _day(r.get("last_contact"))
        return None
    to_reply = [(answered_on(r) - _day(r["date"])).days for r in sent if answered_on(r)]
    to_no = [(_day(r["rejected_on"]) - _day(r["date"])).days for r in sent
             if r["status"] == tracker.REJECTED and _day(r.get("rejected_on"))]

    posts = _postings(sent)
    found = _found(sent, posts)
    # A row logged with no role takes the title of the posting it links to.
    title = lambda r: (r.get("role") or "").strip() or (posts.get(r.get("job_id")) or ("", "", ""))[2] or ""
    source = lambda r: (r.get("source") or "").strip() or "Not recorded"
    where = lambda r: found.get(r.get("job_id"), "Somewhere else")
    level = lambda r: LEVEL_NAMES.get(seniority(title(r)), "Mid level") if title(r) else "No title"

    return {
        "today": today.isoformat(), "total": total, "undated": sum(r["status"] != tracker.SAVED and not _day(r["date"]) for r in rows), "saved": len(saved), "active": active,
        "waiting": waiting, "first": min((r["date"] for r in sent), default=None),
        "replied": _rate(replied, total), "rejected": _rate(rejected, total),
        "silent": _rate(silent, total),
        "rejected_at": [{"stage": s, "n": rejected_at[s]} for s in tracker.STAGES if s in rejected_at],
        "weeks": weeks, "trend": trend, "pace": pace, "rolling": rolling,
        "pace_per_day": round(pace_n / PACE_DAYS, 1), "pace_n": pace_n,
        "funnel": funnel,
        "to_reply": _median(to_reply), "to_rejection": _median(to_no),
        "by_source": _group(sent, source), "by_found": _group(sent, where),
        "by_level": _group(sent, level),
        "mins": {"rate": MIN_RATE, "median": MIN_MEDIAN, "trend": MIN_TREND},
    }
