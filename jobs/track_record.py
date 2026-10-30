"""What actually got the user recruiter calls, counted from the tracker.

    python3 -m jobs.track_record      rebuild now and print the table

Every row in the applications tracker (tracker.py) is an application,
except Saved rows. A row got a call when its status is Phone Screen,
Interviewing, Onsite or Offer, or when it was Rejected after it had moved
past Applied. "No response" is an application without a call.

Each company is tagged once with the same product area tags the fit model
uses (fit.TAGS), in one model call for all the new ones, and the tags
are kept in data/company_tags.json so a company is never asked about
twice. The counts per area and per location type go to
data/track_record.json, which fit.adjust reads.

The watcher calls refresh() on every run; it rebuilds at most once a day.
"""

import json
import os
import sys
import time

from jobs import fit
from jobs.common import BASE_DIR, DATA_DIR, normalize

RECORD_PATH = os.path.join(DATA_DIR, "track_record.json")
TAGS_PATH = os.path.join(DATA_DIR, "company_tags.json")

CALL_STATUSES = {"phone screen", "interviewing", "onsite", "on-site", "offer"}
NOT_APPLIED = {"saved"}
LOCATION_TYPES = ("on-site", "hybrid", "remote")


class RecordError(Exception):
    pass


# ── Reading the tracker

def fetch_rows():
    """Every row in the applications tracker, through applied.py's helper."""
    sys.path.insert(0, BASE_DIR)
    try:
        import applied
        rows = applied.list_jobs()
    except Exception as e:
        raise RecordError("could not read the tracker (%s)" % e)
    if isinstance(rows, dict):
        rows = rows.get("jobs") or []
    if not isinstance(rows, list):
        raise RecordError("the tracker did not come back as a list")
    return rows


# ── Counting

def is_application(row):
    return (row.get("status") or "").strip().lower() not in NOT_APPLIED


def got_call(row):
    status = (row.get("status") or "").strip().lower()
    if status in CALL_STATUSES:
        return True
    if status == "rejected":
        at = (row.get("rejected_at") or "").strip().lower()
        return bool(at) and at not in {"applied", "no response", "saved"}
    return False


def location_type(row):
    s = (row.get("location") or "").strip().lower()
    if not s:
        return ""
    if "hybrid" in s:
        return "hybrid"
    if "remote" in s:
        return "remote"
    if "site" in s or "office" in s:
        return "on-site"
    return ""


def count(rows, company_tags):
    """The record: overall, per area tag, per location type."""
    overall = {"apps": 0, "calls": 0}
    areas, locations, called = {}, {}, []
    for row in rows:
        if not is_application(row):
            continue
        call = got_call(row)
        overall["apps"] += 1
        overall["calls"] += call
        if call:
            called.append(row.get("company") or "")
        for t in set(company_tags.get(normalize(row.get("company")), [])):
            e = areas.setdefault(t, {"apps": 0, "calls": 0})
            e["apps"] += 1
            e["calls"] += call
        lt = location_type(row)
        if lt:
            e = locations.setdefault(lt, {"apps": 0, "calls": 0})
            e["apps"] += 1
            e["calls"] += call
    overall["rate"] = round(overall["calls"] / overall["apps"], 4) if overall["apps"] else 0
    for e in list(areas.values()) + list(locations.values()):
        e["rate"] = round(e["calls"] / e["apps"], 4) if e["apps"] else 0
    areas = dict(sorted(areas.items(), key=lambda kv: (-kv[1]["calls"], -kv[1]["apps"])))
    return {"overall": overall, "areas": areas, "locations": locations,
            "called": sorted(c for c in called if c)}


# ── Tagging companies (model, once per company)

TAG_SYSTEM = """You tag companies by product area for one person's job
search. For each company give 1 to 4 tags from the allowed list that
best describe what the company makes or sells. Use "hardware" when it
makes physical devices, "robotics" for robots, "automotive" for cars,
vehicles and mobility, and "big tech" only for very large, well known tech
or consumer companies. Use the role and link given as hints. If you do not
know the company, tag from its name and link, or return an empty list."""


def _tag_schema():
    return {
        "type": "object",
        "properties": {"companies": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"},
                           "tags": {"type": "array",
                                    "items": {"type": "string", "enum": list(fit.TAGS)}}},
            "required": ["id", "tags"]}}},
        "required": ["companies"],
    }


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def tag_companies(rows, log=print):
    """{normalized company: [tags]}, asking the model only about companies
    not in the cache. A failed call leaves those companies untagged until
    the next rebuild."""
    cache = _read(TAGS_PATH, {})
    todo = {}
    for row in rows:
        key = normalize(row.get("company"))
        if key and key not in cache and key not in todo:
            todo[key] = row
    if todo:
        lines = ["--- id: %s\nCompany: %s\nRole: %s\nLink: %s" % (
            k, r.get("company") or "", r.get("role") or "", (r.get("link") or "")[:120])
            for k, r in todo.items()]
        prompt = "COMPANIES\n\n" + "\n".join(lines) + "\n\nReturn one entry per id."
        try:
            res = fit.cli(prompt, TAG_SYSTEM, _tag_schema())
        except fit.FitError as e:
            log("track record: tagging failed, %s" % e)
            res = {}
        allowed = set(fit.TAGS)
        for item in res.get("companies") or []:
            k = item.get("id")
            if k in todo:
                cache[k] = [t for t in (item.get("tags") or []) if t in allowed][:4]
        _write(TAGS_PATH, cache)
    return cache


# ── Build and load

def build(rows=None, log=print):
    rows = fetch_rows() if rows is None else rows
    tags = tag_companies(rows, log=log)
    rec = count(rows, tags)
    rec["built"] = time.strftime("%Y-%m-%d %H:%M")
    rec["companies"] = len({normalize(r.get("company")) for r in rows if is_application(r)})
    _write(RECORD_PATH, rec)
    _cache.clear()
    return rec


def refresh(log=print):
    """Rebuild if the record is not from today. Never raises: a tracker or
    model failure just keeps yesterday's record."""
    rec = _read(RECORD_PATH, {})
    if (rec.get("built") or "")[:10] == time.strftime("%Y-%m-%d"):
        return False
    try:
        build(log=log)
        return True
    except Exception as e:
        log("track record: not rebuilt, %s" % e)
        return False


_cache = {}


def _cached(path, make):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return make(None)
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    val = make(_read(path, None))
    _cache[path] = (mtime, val)
    return val


def load():
    """The record for fit.adjust, or {} when there is none yet."""
    return _cached(RECORD_PATH, lambda d: d if isinstance(d, dict) else {})


def _table(rec):
    o = rec["overall"]
    out = ["%d applications, %d calls (%.1f%%)" % (o["apps"], o["calls"], 100 * o["rate"]), "",
           "%-18s %5s %6s %6s" % ("area", "apps", "calls", "rate")]
    for t, e in rec["areas"].items():
        out.append("%-18s %5d %6d %5.0f%%" % (t, e["apps"], e["calls"], 100 * e["rate"]))
    out.append("")
    for t in LOCATION_TYPES:
        e = rec["locations"].get(t)
        if e:
            out.append("%-18s %5d %6d %5.0f%%" % (t, e["apps"], e["calls"], 100 * e["rate"]))
    out.append("")
    out.append("Calls: " + ", ".join(rec.get("called") or []))
    return "\n".join(out)


if __name__ == "__main__":
    try:
        print(_table(build()))
    except RecordError as e:
        sys.exit("track record: %s" % e)
