"""Hacker News "Who is hiring?".

The whoishiring account posts one thread a month; each top comment is one
company's post, often listing several roles. Comments come from the
official HN API, one request each, only the ones not read yet. Those that
mention one of your roles (jobs/config.json "roles", else the example
design words) go to the model, which pulls out those roles as written;
nothing is guessed. What it found is kept per thread in data/hn_<id>.json,
so a comment is read and sent once.

One board holds many companies, so each posting carries its own. When next
month's thread goes up, last month's roles drop off and close.
"""

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from jobs.common import DATA_DIR, load_config

API = "https://hacker-news.firebaseio.com/v0"
TIMEOUT = 20
MODEL = "haiku"
BATCH = 12
_DESIGN = re.compile(r"\bdesign(er|ers)?\b|\bux\b|\bui\b", re.I)
# The example: used when config.json has no "roles".
EXAMPLE_KIND = ("the design roles only: product, UX, UI, interaction, visual, brand or\n"
                "motion designers, design leads and design managers, design engineers")


def _roles():
    return [r.strip() for r in load_config().get("roles") or [] if isinstance(r, str) and r.strip()]


def _mentions(text):
    roles = _roles()
    if not roles:
        return bool(_DESIGN.search(text))
    low = text.lower()
    return any(r.lower() in low for r in roles)


def system():
    roles = _roles()
    kind = ("only roles like these: " + ", ".join(roles)) if roles else EXAMPLE_KIND
    return SYSTEM.replace("{kind}", kind)


SYSTEM = """You read job posts from Hacker News "Who is hiring?" and pull
out {kind}.
Skip every other role, even in the same post.

Use only what the post says. Copy the company name and the role title as
written. location is the place as written ("SF", "Remote (US)", "NYC or
remote"); remote is true only when the post says the role can be remote.
employment is what the post says (full-time, contract, part-time,
freelance, intern), or "" when it does not say. apply_url is a link from
the post for applying or for the role, or "". Never invent a value."""

SCHEMA = {
    "type": "object",
    "properties": {"roles": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "comment_id": {"type": "integer"}, "company": {"type": "string"},
            "title": {"type": "string"}, "location": {"type": "string"},
            "remote": {"type": "boolean"}, "employment": {"type": "string"},
            "apply_url": {"type": "string"}},
        "required": ["comment_id", "company", "title", "location", "remote",
                     "employment", "apply_url"]}}},
    "required": ["roles"],
}


def _get(path, tries=3):
    for n in range(tries):
        try:
            r = requests.get("%s/%s.json" % (API, path), timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if n == tries - 1:
                raise
            time.sleep(1 + n)


def _item(k):
    """One comment, or None when it would not load; it is tried next run."""
    try:
        return _get("item/%d" % k)
    except requests.RequestException:
        return None


def _plain(raw):
    t = re.sub(r"<p>", "\n\n", raw or "")
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def thread():
    """The newest "Who is hiring?" story: (id, item)."""
    for sid in (_get("user/whoishiring") or {}).get("submitted", [])[:6]:
        item = _get("item/%d" % sid) or {}
        if "who is hiring" in (item.get("title") or "").lower():
            return sid, item
    raise ValueError("no Who is hiring thread found")


def _path(tid):
    return os.path.join(DATA_DIR, "hn_%d.json" % tid)


def _extract(comments):
    from jobs import fit
    prompt = "\n\n".join("## comment_id %d\n%s" % (c["id"], c["text"][:4000]) for c in comments)
    return fit.cli(prompt, system(), SCHEMA, model=MODEL).get("roles") or []


def fetch(slug="whoishiring"):
    tid, story = thread()
    store = {"read": [], "comments": {}, "roles": []}
    try:
        with open(_path(tid)) as f:
            store = json.load(f)
    except (OSError, ValueError):
        pass
    read = set(store["read"])
    todo = [k for k in story.get("kids") or [] if k not in read]
    with ThreadPoolExecutor(max_workers=2) as pool:
        items = [i for i in pool.map(_item, todo) if i]
    todo = [i["id"] for i in items]
    design = []
    for i in items:
        if i.get("deleted") or i.get("dead"):
            continue
        text = _plain(i.get("text"))
        if _mentions(text):
            design.append({"id": i["id"], "time": i.get("time"), "text": text})
            store["comments"][str(i["id"])] = {"time": i.get("time"), "text": text[:12000]}
    # Comments with none of the roles in them are read for good; the others
    # only once the model has been through them, so a failed call is retried.
    read |= set(todo) - {c["id"] for c in design}
    for n in range(0, len(design), BATCH):
        batch = design[n:n + BATCH]
        roles = _extract(batch)
        ids = {c["id"] for c in batch}
        store["roles"] += [r for r in roles if r.get("comment_id") in ids]
        read |= ids
        store["read"] = sorted(read)
        _save(tid, store)
    store["read"] = sorted(read)
    _save(tid, store)
    out = []
    for n, r in enumerate(store["roles"]):
        c = store["comments"].get(str(r["comment_id"])) or {}
        out.append({"pid": "%d-%s" % (r["comment_id"], re.sub(r"[^a-z0-9]+", "-",
                                                             r["title"].lower()).strip("-")[:60]),
                    "title": r["title"], "company": r["company"],
                    "url": r.get("apply_url") or
                           "https://news.ycombinator.com/item?id=%d" % r["comment_id"],
                    "location": r["location"], "remote": bool(r["remote"]),
                    "posted": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(c["time"]))
                    if c.get("time") else "",
                    "updated": "", "text": c.get("text") or "",
                    "emptype": r.get("employment") or ""})
    return out


def _save(tid, store):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path(tid) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, _path(tid))


def boards():
    return [{"employer": "hn", "name": "HN Who's Hiring", "ats": "hn", "slug": "whoishiring",
             "verified": True, "source": "hn"}]
