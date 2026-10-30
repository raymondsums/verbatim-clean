"""How well a job fits the user, as a tier plus a 0 to 100 score inside it.

Two parts:

- Background fit. Each job is compared once with the user's own
  content/master.md and profile.json (level, domain, the skills it asks
  for) through the model (llm.py), several
  jobs per call. The tier, a one line reason and a short list of things the
  job asks for that master.md has no proof of are stored with the job and
  never asked again, so a job reads the same every time it is opened.
- Interest. The user's own preferences list ("more AI tools", "less ad tech")
  counts from day one. What the user does with jobs (opened, starred, tailored,
  applied, not for me) is learned per product area. Together they move the
  stored tier at most one step, never a Stretch up to Strong, and the
  reason says why it moved. Nothing here calls the model.
- Track record. Which areas and location types actually got the user recruiter
  calls, counted from the applications tracker (jobs/track_record.py). It sits
  between the user's own words and learned interest, and only moves a tier when
  the counts are big enough to mean something.
"""

import json
import math
import os
import re
import sys

from jobs.common import BASE_DIR, DATA_DIR, compact, load_config

sys.path.insert(0, BASE_DIR)
import llm  # noqa: E402

MASTER_PATH = os.path.join(BASE_DIR, "content", "master.md")
PROFILE_PATH = os.environ.get("VERBATIM_PROFILE") or os.path.join(BASE_DIR, "profile.json")
PREFS_PATH = os.path.join(DATA_DIR, "job_prefs.json")

TIERS = ("stretch", "look", "strong")          # low to high
LABELS = {"strong": "Strong match", "look": "Worth a look", "stretch": "Stretch"}

# Product area tags. The model picks from these for postings, and
# track_record.py uses the same list for the companies the user applied to, so
# the two can be compared.
TAGS = ("ai tools", "automotive", "hardware", "robotics", "insurance", "fintech",
        "ad tech", "health", "developer tools", "design systems", "mobile",
        "enterprise", "consumer", "big tech", "e-commerce", "crypto",
        "productivity", "education", "gaming")

# The score always sits inside its tier's band, so the number and the
# label never disagree. A tier moved by interest carries its score across
# to the same spot in the new band.
BANDS = {"stretch": (30, 59), "look": (60, 79), "strong": (80, 99)}

CLAUDE_MODEL = "sonnet"
TIMEOUT = 300
BATCH = 8
TEXT_CHARS = 2600

# ── Background fit (model, once per job)

SYSTEM = """You sort job postings for one job seeker by how well their
real background fits each one. You get their background (the only source of
truth about them) and a batch of postings.

For each posting decide a tier:
- strong: their level fits (read their current or most recent title and
  years of experience from the background; roles at that level or one step
  up can fit when the domain is a close match) AND the domain or the core
  skills overlap clearly with work they have actually done.
- look: a fair fit with a real gap: level is a step off, or the domain is
  new to them but the core work asked for is what they do.
- stretch: a big gap: two or more levels off, a kind of work they have not
  practiced, or a domain plus level gap together.

Write for each:
- score: 0 to 100, how close the fit is, inside the tier's band: strong
  80 to 99, look 60 to 79, stretch 30 to 59. Use the whole band: a strong
  match with a small gap is low 80s, a near perfect one is mid 90s.
- reason: one plain sentence, at most 18 words, naming the concrete overlap
  or gap. They read it, so address them as "you" (for example "Senior role
  in payments, close to your checkout work at your last company"). No hype,
  no em dashes.
- gaps: up to 3 short items the posting asks for that their background
  shows no proof of yet (for example "healthcare", "B2B data tools",
  "people management"). Empty when nothing important is missing.
- about: what the company or product is, at most 8 words.
- tags: 2 to 4 short lowercase product area tags, such as "ai tools",
  "automotive", "hardware" (physical devices the company makes),
  "robotics", "insurance", "fintech", "ad tech", "health", "developer
  tools", "design systems", "mobile", "enterprise", "consumer", "big tech"
  (a very large, well known tech or consumer company), "e-commerce",
  "crypto", "productivity", "education", "gaming".

Never invent facts about them. Judge only from the background given."""

SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tier": {"type": "string", "enum": ["strong", "look", "stretch"]},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "about": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "tier", "reason", "gaps", "about", "tags"],
            },
        }
    },
    "required": ["jobs"],
}


class FitError(Exception):
    pass


def background():
    """The user's master.md, plus the level and years from profile.json.
    Contact facts and the authorization and EEO wording are left out: they
    say nothing about fit. A "## Positioning" heading, when there is one,
    marks where the useful part starts, and "## Settled decisions" where it
    ends."""
    try:
        with open(MASTER_PATH, encoding="utf-8") as f:
            md = f.read()
    except OSError:
        md = ""
    start = md.find("## Positioning")
    end = md.find("## Settled decisions")
    md = md[start if start >= 0 else 0:end if end > 0 else len(md)].strip()
    extra = []
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            prof = json.load(f)
    except (OSError, ValueError):
        prof = {}
    jobs = prof.get("employment_history") or []
    if jobs and isinstance(jobs[0], dict) and jobs[0].get("title"):
        extra.append("Current or most recent title: %s" % jobs[0]["title"])
    if prof.get("years_of_experience"):
        extra.append("Years of experience: %s" % prof["years_of_experience"])
    level = load_config().get("seniority")
    if level:
        extra.append("Level they are looking for: %s" % level)
    return "\n".join(extra + ([""] if extra else []) + [md]).strip()


def _clean(s):
    return re.sub(r"\s*[\u2014\u2013]\s*", ", ", (s or "").strip())


def _prompt(jobs):
    parts = ["BACKGROUND\n\n" + background(), "\nPOSTINGS"]
    for j in jobs:
        parts.append("\n--- id: %s\nTitle: %s\nCompany: %s\nLocation: %s\n\n%s" % (
            j["id"], j["title"], j["company"], j["location"],
            (j.get("text") or "(no description available)")[:TEXT_CHARS]))
    parts.append("\nReturn one entry per posting id.")
    return "\n".join(parts)


def _run_cli(prompt):
    res = cli(prompt, SYSTEM, SCHEMA)
    if not isinstance(res.get("jobs"), list):
        raise FitError("claude CLI did not return the tiers")
    return res


def cli(prompt, system, schema, model=None):
    """One model call with a JSON schema (see llm.py). Returns the parsed
    object."""
    try:
        return llm.run(system, schema, prompt, timeout=TIMEOUT,
                       model=model or CLAUDE_MODEL)
    except llm.LLMError as e:
        raise FitError(str(e))


def in_band(score, tier):
    """score kept inside its tier's band, or None when there is none."""
    if tier not in BANDS or not isinstance(score, (int, float)) \
            or isinstance(score, bool):
        return None
    lo, hi = BANDS[tier]
    return int(max(lo, min(hi, round(score))))


def moved_score(score, base, tier):
    """A stored score carried to the same spot in the final tier's band."""
    score = in_band(score, base)
    if score is None or tier not in BANDS or tier == base:
        return score
    lo, hi = BANDS[base]
    lo2, hi2 = BANDS[tier]
    return int(round(lo2 + (score - lo) * (hi2 - lo2) / (hi - lo)))


# ── Freshness. An old posting is often filled or stale, so its score comes
# down with age. Points off the 0 to 100 score, by days since posted (else
# since the watcher first saw it):
#
#   up to 14 days   0          nothing for the first two weeks
#   14 to 42 days   0 to 4     gentle: about 4 points by six weeks
#   42 to 90 days   4 to 16    steeper: 16 points by three months
#   past 90 days    16 to 30   keeps falling, more slowly, never past 30
#
# Each piece starts where the last one ends, so the curve has no jumps.
# At three months only a 96 or better stays a Strong match (80 and up);
# by six months a 96 is about 73, Worth a look. Lowering the score can
# lower the tier, never raise it.
FRESH_DAYS = 14
GENTLE_DAYS, GENTLE_PTS = 42, 4
STEEP_DAYS, STEEP_PTS = 90, 16
MAX_PTS, TAIL_DAYS = 30, 120


def age_penalty(days):
    """Points off for a posting this many days old."""
    if days is None or days <= FRESH_DAYS:
        return 0.0
    if days <= GENTLE_DAYS:
        return GENTLE_PTS * ((days - FRESH_DAYS) / (GENTLE_DAYS - FRESH_DAYS)) ** 1.5
    if days <= STEEP_DAYS:
        return GENTLE_PTS + (STEEP_PTS - GENTLE_PTS) * (days - GENTLE_DAYS) / (STEEP_DAYS - GENTLE_DAYS)
    return STEEP_PTS + (MAX_PTS - STEEP_PTS) * (1 - math.exp(-(days - STEEP_DAYS) / TAIL_DAYS))


def freshen(score, tier, days):
    """(score, tier, note) with the age taken off. note is "" when nothing
    changed. A job with no score drops one tier past three months."""
    pts = age_penalty(days)
    if pts < 0.5 or tier not in TIERS:
        return score, tier, ""
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        if days > STEEP_DAYS and tier != "stretch":
            tier = TIERS[TIERS.index(tier) - 1]
            return score, tier, "Older posting, so it sits a step lower."
        return score, tier, ""
    new = max(BANDS["stretch"][0], int(round(score - pts)))
    for t in reversed(TIERS[:TIERS.index(tier) + 1]):
        if new >= BANDS[t][0] or t == "stretch":
            tier = t
            break
    return new, tier, "Older posting: %d points off for its age." % round(pts)


def score_batch(jobs):
    """{job id: {tier, score, reason, gaps, about, tags}} for the jobs given."""
    res = _run_cli(_prompt(jobs))
    want = {j["id"] for j in jobs}
    out = {}
    for r in res["jobs"]:
        if r.get("id") not in want or r.get("tier") not in TIERS:
            continue
        out[r["id"]] = {
            "tier": r["tier"],
            "score": in_band(r.get("score"), r["tier"]),
            "reason": _clean(r.get("reason"))[:200],
            "gaps": [_clean(g)[:60] for g in (r.get("gaps") or []) if g][:3],
            "about": _clean(r.get("about"))[:80],
            "tags": [t.strip().lower()[:30] for t in (r.get("tags") or []) if t][:4],
        }
    return out


SCORE_SYSTEM = """You give a fit score to job postings for one job seeker
whose tier is already decided. You get their background and the postings,
each with its tier and the reason it was given.

For each posting return a score from 0 to 100 inside its tier's band:
strong 80 to 99, look 60 to 79, stretch 30 to 59. Use the whole band: a
strong match with a small gap is low 80s, a near perfect one is mid 90s.
Do not change the tier. Never invent facts about them."""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"},
                               "score": {"type": "integer"}},
                "required": ["id", "score"],
            },
        }
    },
    "required": ["jobs"],
}


def score_only(jobs):
    """{job id: score} for jobs that have a tier but no score yet."""
    parts = ["BACKGROUND\n\n" + background(), "\nPOSTINGS"]
    for j in jobs:
        parts.append("\n--- id: %s\nTier: %s\nReason: %s\nTitle: %s\nCompany: %s\n"
                     "Location: %s\n\n%s" % (
                         j["id"], j["tier"], j.get("reason") or "", j["title"],
                         j["company"], j["location"],
                         (j.get("text") or "(no description available)")[:TEXT_CHARS]))
    parts.append("\nReturn one entry per posting id.")
    res = cli("\n".join(parts), SCORE_SYSTEM, SCORE_SCHEMA)
    if not isinstance(res.get("jobs"), list):
        raise FitError("claude CLI did not return the scores")
    tiers = {j["id"]: j["tier"] for j in jobs}
    out = {}
    for r in res["jobs"]:
        s = in_band(r.get("score"), tiers.get(r.get("id")))
        if s is not None:
            out[r["id"]] = s
    return out


# ── Preferences (the user's own words, stored locally)

def load_prefs():
    try:
        with open(PREFS_PATH) as f:
            return [l for l in json.load(f).get("lines", []) if l.strip()]
    except (FileNotFoundError, ValueError):
        return []


def save_prefs(lines):
    clean = []
    for l in lines or []:
        l = re.sub(r"\s+", " ", str(l)).strip()[:80]
        if l and l.lower() not in [c.lower() for c in clean]:
            clean.append(l)
    clean = clean[:30]
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"lines": clean}, f, indent=1)
    os.replace(tmp, PREFS_PATH)
    return clean


_DOWN = re.compile(r"^\s*(?:(?:less|fewer|no|not|avoid|skip|never|without)\b|-)\s*", re.I)
_UP = re.compile(r"^\s*(?:(?:more|want|love|prefer|yes)\b|\+)\s*", re.I)
_FILLER = {"a", "an", "the", "and", "or", "of", "for", "in", "on", "at", "to",
           "tool", "tools", "product", "products", "company", "companies",
           "role", "roles", "job", "jobs", "work", "stuff", "things", "startup",
           "startups", "space", "related", "please"}


def parse_pref(line):
    """'more AI tools' -> (1, ['ai']). 'less ad tech' -> (-1, ['ad', 'tech'])."""
    sign = 1
    m = _DOWN.match(line)
    if m:
        sign, line = -1, line[m.end():]
    else:
        m = _UP.match(line)
        if m:
            line = line[m.end():]
    words = [w for w in re.findall(r"[a-z0-9+#.]+", line.lower()) if w not in _FILLER]
    return sign, words, line.strip()


def _has(words, hay):
    return bool(words) and all(
        re.search(r"(?<![a-z0-9])%s(s|es)?(?![a-z0-9])" % re.escape(w), hay) for w in words)


def haystack(job):
    tags = job.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags or "[]")
        except ValueError:
            tags = []
    return " | ".join([" ".join(tags or []), job.get("about") or "",
                       job.get("title") or "", job.get("company") or ""]).lower()


# ── Learned interest

WEIGHTS = {"applied": 3, "starred": 2, "tailored": 2, "opened": 1, "dismissed": -2}
LEARN_MIN_JOBS = 3      # this many acted-on jobs share the area
LEARN_MIN_SCORE = 4     # and the net signal is at least this strong


def job_signal(job):
    """One job's weight from what the user did with it."""
    if job.get("status") == "dismissed":
        return WEIGHTS["dismissed"]
    if job.get("status") == "applied":
        return WEIGHTS["applied"]
    w = 0
    if job.get("starred"):
        w += WEIGHTS["starred"]
    if job.get("tailored"):
        w += WEIGHTS["tailored"]
    if job.get("opened"):
        w += WEIGHTS["opened"]
    return min(w, WEIGHTS["applied"])


def learn(jobs):
    """Area tag -> [net signal, jobs acted on, positive count, negative count]."""
    out = {}
    for j in jobs:
        w = job_signal(j)
        if not w:
            continue
        tags = j.get("tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags or "[]")
            except ValueError:
                tags = []
        for t in set(tags or []):
            e = out.setdefault(t, [0, 0, 0, 0])
            e[0] += w
            e[1] += 1
            e[2 if w > 0 else 3] += 1
    return out


# ── Track record (what got the user calls, from jobs/track_record.py)

RECORD_UP_CALLS = 2       # an area needs at least this many calls
RECORD_UP_RATIO = 2.0     # and a call rate this many times the overall one
RECORD_DOWN_APPS = 20     # an area needs this many applications
RECORD_DOWN_CALLS = 1     # with at most this many calls to count against it,
                          # and a call rate below the overall one


def _tags(job):
    tags = job.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags or "[]")
        except ValueError:
            tags = []
    return tags or []


def location_type(loc):
    """'remote', 'hybrid', 'on-site', or '' when the text says nothing."""
    s = (loc or "").lower()
    if not s.strip():
        return ""
    if "hybrid" in s:
        return "hybrid"
    if "remote" in s or "anywhere" in s:
        return "remote"
    return "on-site"


def _calls(n):
    return "no calls" if n == 0 else "1 call" if n == 1 else "%d calls" % n


def record_step(job, record):
    """(step, why) from the track record, or (0, '').

    Up when an area the user applied in got at least RECORD_UP_CALLS calls at
    RECORD_UP_RATIO times the user's overall rate. Down when an area or location
    type has RECORD_DOWN_APPS applications and RECORD_DOWN_CALLS calls or
    fewer, and a call rate below the user's overall one (with few calls in all,
    1 in 35 is average, not a warning). Mixed evidence moves nothing.
    """
    if not record:
        return 0, ""
    overall = record.get("overall") or {}
    rate = overall.get("calls", 0) / overall["apps"] if overall.get("apps") else 0
    areas = record.get("areas") or {}
    ups, downs = [], []
    for t in _tags(job):
        e = areas.get(t)
        if not e or not e.get("apps"):
            continue
        r = e["calls"] / e["apps"]
        if e["calls"] >= RECORD_UP_CALLS and r >= RECORD_UP_RATIO * rate:
            ups.append((r, e["calls"], "you got calls from %d of %d %s applications"
                        % (e["calls"], e["apps"], t)))
        elif (e["apps"] >= RECORD_DOWN_APPS and e["calls"] <= RECORD_DOWN_CALLS
              and r < rate):
            downs.append((e["calls"] / e["apps"], -e["apps"], "%s from %d %s applications"
                          % (_calls(e["calls"]), e["apps"], t)))
    lt = location_type(job.get("location"))
    e = (record.get("locations") or {}).get(lt) if lt else None
    if (e and e.get("apps", 0) >= RECORD_DOWN_APPS and e.get("calls", 0) <= RECORD_DOWN_CALLS
            and e["calls"] / e["apps"] < rate):
        downs.append((e["calls"] / e["apps"], -e["apps"], "%s from %d %s applications"
                      % (_calls(e["calls"]), e["apps"], lt)))
    if ups and not downs:
        return 1, max(ups)[2]
    if downs and not ups:
        return -1, min(downs)[2]
    return 0, ""


def adjust(job, prefs=None, learned=None, record=None):
    """(final tier, note) from the stored background tier.

    Signals in order, first one that says something wins: the user's own
    preference lines, then the user's track record, then learned interest.
    Moves one step at most. A Stretch never becomes a Strong match: interest
    can lift what fits, it cannot make up for what does not.
    """
    return _adjust(job, prefs, learned, record)


AI_LABS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_labs.txt")


def ai_labs():
    """[(company key, [words])] from ai_labs.txt, the companies exempt from
    lower_tags; words empty means any role."""
    out = []
    try:
        with open(AI_LABS_PATH) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.split("#")[0].strip()
        if not line:
            continue
        name, _, words = line.partition(":")
        out.append((compact(name), [w.strip().lower() for w in words.split(",") if w.strip()]))
    return out


def is_ai_lab(job):
    key = compact(job.get("company") or "")
    hay = ((job.get("title") or "") + " " + (job.get("text") or "")).lower()
    for name, words in ai_labs():
        if key == name and (not words or any(re.search(r"\b%s\b" % re.escape(w), hay) for w in words)):
            return True
    return False


def _adjust(job, prefs, learned, record):
    base = job.get("tier")
    if base not in TIERS:
        return base, ""
    hay = haystack(job)
    up, down = [], []
    for line in prefs or []:
        sign, words, phrase = parse_pref(line)
        if _has(words, hay):
            (up if sign > 0 else down).append(line.strip())
    step, why = 0, ""
    if up and not down:
        step, why = 1, "you asked for %s" % _lower_first(up[0])
    elif down and not up:
        step, why = -1, "you asked for %s" % _lower_first(down[0])

    # Product areas listed under "lower_tags" in config.json stay listed but
    # sit a tier lower, unless the user's own lines ask for them or the
    # company is in jobs/ai_labs.txt.
    lower = [t.strip().lower() for t in load_config().get("lower_tags") or []
             if isinstance(t, str) and t.strip()]
    hit = next((t for t in _tags(job) if t in lower), None)
    if step == 0 and not up and hit and not is_ai_lab(job):
        step, why = -1, "%s is on your lower list" % hit

    if step == 0 and record:
        step, why = record_step(job, record)

    if step == 0 and learned:
        best = None
        for t in _tags(job):
            e = learned.get(t)
            if e and e[1] >= LEARN_MIN_JOBS and abs(e[0]) >= LEARN_MIN_SCORE:
                if best is None or abs(e[0]) > abs(best[1][0]):
                    best = (t, e)
        if best:
            t, e = best
            if e[0] > 0:
                step, why = 1, "you have shown interest in %d %s jobs" % (e[2], t)
            else:
                step, why = -1, "you passed on %d %s jobs" % (e[3], t)

    i = TIERS.index(base)
    j = max(0, min(len(TIERS) - 1, i + step))
    if base == "stretch" and TIERS[j] == "strong":
        j = TIERS.index("look")
    if j == i:
        return base, ""
    return TIERS[j], "%s from %s: %s." % ("Up" if j > i else "Down", LABELS[base], why)


def _lower_first(s):
    return s[:1].lower() + s[1:] if s else s
