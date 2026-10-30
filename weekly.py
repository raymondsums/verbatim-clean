"""Three a week: each Monday, three answer groups worth sharpening.

Picked from the note and the versions log, no model call:
- most used: groups the user has sent the most
- most edited: groups where what the user sent kept differing from what
  Verbatim put in the field
- oldest best answer: the best answer has not changed in the longest time

The pick holds for the whole week, so the card in the Answers view and
the Monday phone push name the same three. Opening one starts the
existing refine flow (or short and long versions for a group too thin to
refine). Nothing changes without the user's approval.
"""

import json
import os
import threading
from datetime import date, datetime
from difflib import SequenceMatcher

import answers as answers_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "three_a_week.json")
BEST_DATES_PATH = os.path.join(BASE_DIR, "data", "best_dates.json")

_lock = threading.Lock()


def week_of(day=None):
    day = day or date.today()
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def note_best_set(group):
    """Remember the day a best answer changed, for "oldest best answer"."""
    with _lock:
        d = _load(BEST_DATES_PATH, {})
        d[group.strip().lower()] = date.today().isoformat()
        _save(BEST_DATES_PATH, d)


def _edits_by_group(log_path=None):
    """How many times what the user sent differed from what Verbatim inserted."""
    out = {}
    try:
        with open(log_path or answers_mod.VERSIONS_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ins, sent = e.get("inserted") or "", e.get("sent") or ""
                if not ins or not sent:
                    continue
                if SequenceMatcher(None, ins, sent, autojunk=False).ratio() < 0.9:
                    k = (e.get("group") or "").strip().lower()
                    out[k] = out.get(k, 0) + 1
    except OSError:
        pass
    return out


def _best_age_days(g, best_dates, today):
    """Days since the best answer last changed. Uses the day the user approved
    one when known, else the oldest dated version (the best answer starts
    as the first version sent)."""
    d = best_dates.get(g["question"].strip().lower())
    if not d:
        dated = [v["date"] for v in g.get("versions") or []
                 if answers_mod._DAY.match(v.get("date") or "")]
        d = min(dated) if dated else ""
    try:
        return (today - date.fromisoformat(d)).days
    except ValueError:
        return 0


def pick(groups, edits=None, best_dates=None, today=None, n=3):
    """The n groups most worth sharpening, each with a plain reason."""
    today = today or date.today()
    edits = edits if edits is not None else _edits_by_group()
    best_dates = best_dates if best_dates is not None else _load(BEST_DATES_PATH, {})
    scored = []
    for g in groups:
        if not (g.get("answer") or "").strip() and not answers_mod.sent_versions(g):
            continue
        used = len(answers_mod.sent_versions(g))
        edited = edits.get(g["question"].strip().lower(), 0)
        age = _best_age_days(g, best_dates, today)
        score = used * 3 + edited * 4 + min(age, 120) / 10
        reasons = []
        if edited >= 2:
            reasons.append((edited * 4, f"you edited it {edited} times"))
        if used >= 2:
            reasons.append((used * 3, f"sent {used} times"))
        if age >= 30:
            months = age // 30
            reasons.append((min(age, 120) / 10,
                            "best answer unchanged for "
                            + (f"{months} months" if months > 1 else "a month")))
        if not reasons:
            reasons.append((0, "sent once" if used == 1 else "best answer only"))
        reasons.sort(key=lambda r: -r[0])
        why = reasons[0][1]
        why = why[0].upper() + why[1:]
        scored.append((score, g["question"], why, used))
    scored.sort(key=lambda x: (-x[0], x[1].lower()))
    return [{"group": t, "why": why,
             "can_refine": used >= answers_mod.REFINE_MIN_VERSIONS}
            for _, t, why, used in scored[:n]]


def this_week(groups=None, today=None):
    """This week's three, picked once and kept for the week."""
    today = today or date.today()
    wk = week_of(today)
    with _lock:
        state = _load(STATE_PATH, {})
        if state.get("week") == wk and state.get("picks"):
            return state
    if groups is None:
        groups = answers_mod.parse_repo(answers_mod.read_repo())
    picks = pick(groups, today=today)
    with _lock:
        state = _load(STATE_PATH, {})
        if state.get("week") == wk and state.get("picks"):
            return state
        state = {"week": wk, "picked": today.isoformat(), "picks": picks,
                 "pushed": False, "done": []}
        if picks:
            _save(STATE_PATH, state)
        return state


def mark_done(group):
    with _lock:
        state = _load(STATE_PATH, {})
        if not state.get("picks"):
            return state
        done = set(state.get("done") or [])
        done.add(group)
        state["done"] = sorted(done)
        _save(STATE_PATH, state)
        return state


def monday_push(send, base_url, today=None, groups=None):
    """On a Monday, push this week's three to the user's phone once.

    send(title, body, url) is the watcher's phone push. Returns True when
    a push went out. Any other day, or already pushed, does nothing.
    """
    today = today or date.today()
    if today.weekday() != 0:
        return False
    state = this_week(groups=groups, today=today)
    if not state.get("picks") or state.get("pushed"):
        return False
    titles = [p["group"] for p in state["picks"]]
    body = "; ".join(titles)
    send("Three answers to sharpen", body,
         (base_url or "").rstrip("/") + "/#answers")
    with _lock:
        state = _load(STATE_PATH, {})
        state["pushed"] = datetime.now().isoformat(timespec="minutes")
        _save(STATE_PATH, state)
    return True
