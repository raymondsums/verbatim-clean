"""Cue: practice cards for recruiter calls and interviews, inside Verbatim.

Its routes live under /api/cue and its cards in data/cue.sqlite. Three decks: recruiter, portfolio, and
interview, which holds one set of cards per job that prep.py makes.

Scheduling is SM-2-lite: each rating moves the card's ease and next date.
"""

import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import Blueprint, g, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
# The user's starter cards, used only when the database has no cards at all. It
# holds the user's own answers (salary, sponsorship), so it lives in data/, which
# stays out of git.
SEED_PATH = os.path.join(DATA_DIR, "cue_seed.json")

bp = Blueprint("cue", __name__, url_prefix="/api/cue")


class CueError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def db_path():
    """CUE_DB lets tests use a throwaway file. Read on each call, since
    server.py loads .env after this module is imported."""
    return os.environ.get("CUE_DB") or os.path.join(DATA_DIR, "cue.sqlite")


def _connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db():
    conn = getattr(g, "_cue_db", None)
    if conn is None:
        conn = g._cue_db = _connect()
    return conn


@bp.teardown_app_request
def close_db(_exc):
    conn = g.pop("_cue_db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(db_path()) or ".", exist_ok=True)
    with sqlite3.connect(db_path()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                tag TEXT,
                front TEXT NOT NULL,
                back TEXT NOT NULL,
                ease REAL NOT NULL DEFAULT 2.5,
                interval_days REAL NOT NULL DEFAULT 0,
                due_date TEXT NOT NULL DEFAULT (date('now','localtime')),
                reviews INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                rating TEXT NOT NULL,
                seconds_taken REAL,
                reviewed_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            """
        )
        # Columns added over time, for a database made by an older Cue.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cards)").fetchall()}
        if "priority" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        if "variations" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN variations TEXT")
        if "notes" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN notes TEXT")
        if "deck" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN deck TEXT NOT NULL DEFAULT 'recruiter'")
        # Interview deck: job is prep.py's id for the tracker row, gen_key
        # marks a card prep.py made (NULL means written by hand).
        if "job" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN job TEXT")
        if "gen_key" not in cols:
            conn.execute("ALTER TABLE cards ADD COLUMN gen_key TEXT")
        if "gen_hash" not in cols:
            # What prep.py last wrote. A card whose text no longer matches
            # was edited by hand, and an update leaves its text alone.
            conn.execute("ALTER TABLE cards ADD COLUMN gen_hash TEXT")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                   job TEXT PRIMARY KEY,
                   company TEXT NOT NULL,
                   role TEXT,
                   updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
               )"""
        )

        count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if count == 0 and os.path.exists(SEED_PATH):
            with open(SEED_PATH, encoding="utf-8") as f:
                seed = json.load(f)
            conn.executemany(
                "INSERT INTO cards (category, tag, front, back, priority) VALUES (?, ?, ?, ?, ?)",
                [(c["category"], c.get("tag"), c["front"], c["back"], c.get("priority", 0)) for c in seed],
            )

        # The three recruiter openers always exist, pinned high, with the
        # other ways a recruiter asks the same thing.
        openers = [
            {
                "category": "recruiter", "tag": "opener-1",
                "front": "Tell me about yourself.",
                "variations": [
                    "Tell me about yourself.",
                    "Walk me through your background.",
                    "Give me the elevator pitch. Who are you?",
                    "Can you start by introducing yourself?",
                    "I'd love to hear about your journey to where you are now.",
                    "Take me through your career so far.",
                ],
            },
            {
                "category": "recruiter", "tag": "opener-2",
                "front": "Why are you interested in [this company]?",
                "variations": [
                    "Why are you interested in [this company]?",
                    "What drew you to apply here?",
                    "Why us, of all the places you could be looking?",
                    "What about [this company] caught your attention?",
                    "What made you want to talk to us?",
                    "Why do you want to work at [this company]?",
                ],
            },
            {
                "category": "recruiter", "tag": "opener-3",
                "front": "What do you know about us?",
                "variations": [
                    "What do you know about us?",
                    "Tell me what you know about [this company].",
                    "How familiar are you with what we do?",
                    "What's your understanding of our product / business?",
                    "Have you had a chance to look into [this company]?",
                    "What's your impression of [this company] so far?",
                ],
            },
        ]
        for o in openers:
            variations_json = json.dumps(o["variations"])
            row = conn.execute(
                "SELECT id FROM cards WHERE category = ? AND front = ?",
                (o["category"], o["front"]),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE cards SET priority = 10, tag = ?, variations = ? WHERE id = ?",
                    (o["tag"], variations_json, row[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO cards (category, tag, front, back, priority, variations) VALUES (?, ?, ?, ?, ?, ?)",
                    (o["category"], o["tag"], o["front"],
                     "[Empty. Open Library to fill in your answer.]", 10, variations_json),
                )

        # Portfolio review deck: starter prompts by walkthrough arc, added once.
        portfolio_seed = [
            ("walkthrough", "Walk me through one of your projects, start to finish."),
            ("walkthrough", "Give me the two-minute version of [this project]."),
            ("decisions", "What was the hardest tradeoff you had to make here?"),
            ("decisions", "Why did you choose this direction over the alternatives you considered?"),
            ("craft", "Walk me through your design process on this, research to final."),
            ("craft", "How did you handle the trickiest UX problem in this project?"),
            ("impact", "How did you measure success? What was the outcome?"),
            ("impact", "What was the business or user impact of this work?"),
            ("reflection", "What would you do differently if you started over?"),
            ("reflection", "What did you learn from this project?"),
        ]
        for cat, front in portfolio_seed:
            exists = conn.execute(
                "SELECT 1 FROM cards WHERE deck = 'portfolio' AND front = ?", (front,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO cards (deck, category, front, back) VALUES ('portfolio', ?, ?, ?)",
                    (cat, front, "[Open Library to fill in your talking points.]"),
                )
        conn.commit()


# ── Spaced repetition (SM-2-lite)

def schedule_next(ease, interval, rating):
    """Return (new_ease, new_interval_days, new_due_iso)."""
    if rating == "again":
        ease = max(1.3, ease - 0.2)
        new_interval = 0  # due today
    elif rating == "hard":
        ease = max(1.3, ease - 0.15)
        new_interval = max(1, interval * 1.2) if interval > 0 else 1
    elif rating == "good":
        new_interval = max(1, interval * ease) if interval > 0 else 1
    elif rating == "easy":
        ease = ease + 0.15
        new_interval = max(2, interval * ease * 1.3) if interval > 0 else 3
    else:
        new_interval = interval or 1
    due = date.today() + timedelta(days=round(new_interval))
    return ease, new_interval, due.isoformat()


# ── Cards

def _card_to_dict(row):
    d = dict(row)
    raw = d.get("variations")
    try:
        d["variations"] = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        d["variations"] = []
    return d


def _variations_to_json(value):
    if value is None:
        return None
    if isinstance(value, str):
        items = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
    else:
        return None
    return json.dumps(items) if items else None


@bp.get("/cards")
def list_cards():
    deck = request.args.get("deck")
    job = request.args.get("job")
    category = request.args.get("category")
    due_only = request.args.get("due") == "1"
    q = "SELECT * FROM cards"
    params = []
    where = []
    if deck:
        where.append("deck = ?")
        params.append(deck)
    if job:
        where.append("job = ?")
        params.append(job)
    if category and category != "all":
        where.append("category = ?")
        params.append(category)
    if due_only:
        where.append("due_date <= date('now','localtime')")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY priority DESC, due_date ASC, id ASC"
    rows = [_card_to_dict(r) for r in db().execute(q, params).fetchall()]
    return jsonify(rows)


@bp.post("/cards")
def create_card():
    d = request.get_json(force=True)
    cur = db().execute(
        "INSERT INTO cards (deck, category, tag, front, back, variations, job) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (d.get("deck", "recruiter"), d["category"], d.get("tag"), d["front"], d["back"],
         _variations_to_json(d.get("variations")), d.get("job") or None),
    )
    db().commit()
    row = db().execute("SELECT * FROM cards WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_card_to_dict(row)), 201


@bp.put("/cards/<int:card_id>")
def update_card(card_id):
    d = request.get_json(force=True)
    current = db().execute("SELECT priority, deck FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not current:
        return jsonify({"error": "not found"}), 404
    priority = d.get("priority", current["priority"])
    deck = d.get("deck", current["deck"])   # a pin toggle sends no deck; keep it
    if "variations" in d:
        db().execute(
            """UPDATE cards
               SET deck = ?, category = ?, tag = ?, front = ?, back = ?, priority = ?, variations = ?,
                   updated_at = datetime('now','localtime')
               WHERE id = ?""",
            (deck, d["category"], d.get("tag"), d["front"], d["back"], priority,
             _variations_to_json(d["variations"]), card_id),
        )
    else:
        db().execute(
            """UPDATE cards
               SET deck = ?, category = ?, tag = ?, front = ?, back = ?, priority = ?,
                   updated_at = datetime('now','localtime')
               WHERE id = ?""",
            (deck, d["category"], d.get("tag"), d["front"], d["back"], priority, card_id),
        )
    db().commit()
    row = db().execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return jsonify(_card_to_dict(row))


@bp.delete("/cards/<int:card_id>")
def delete_card(card_id):
    db().execute("DELETE FROM cards WHERE id = ?", (card_id,))
    db().commit()
    return ("", 204)


@bp.post("/cards/<int:card_id>/review")
def review_card(card_id):
    d = request.get_json(force=True)
    rating = d["rating"]  # again | hard | good | easy
    seconds = d.get("seconds")
    row = db().execute("SELECT ease, interval_days FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    new_ease, new_interval, new_due = schedule_next(row["ease"], row["interval_days"], rating)
    db().execute(
        """UPDATE cards
           SET ease = ?, interval_days = ?, due_date = ?, reviews = reviews + 1, updated_at = datetime('now','localtime')
           WHERE id = ?""",
        (new_ease, new_interval, new_due, card_id),
    )
    db().execute(
        "INSERT INTO reviews (card_id, rating, seconds_taken) VALUES (?, ?, ?)",
        (card_id, rating, seconds),
    )
    db().commit()
    return jsonify({"ease": new_ease, "interval_days": new_interval, "due_date": new_due})


@bp.post("/cards/<int:card_id>/notes")
def save_notes(card_id):
    d = request.get_json(force=True)
    cur = db().execute(
        "UPDATE cards SET notes = ?, updated_at = datetime('now','localtime') WHERE id = ?",
        (d.get("notes", ""), card_id),
    )
    db().commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return ("", 204)


# ── Interview deck: one job's cards, made by prep.py

INTERVIEW_CATS = ("why", "gap", "answer", "other")
_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def _gen_hash(front, back):
    return hashlib.sha1((front + "\x00" + back).encode("utf-8")).hexdigest()


def job_counts():
    """{job id: interview card count} for every job with a deck."""
    conn = _connect()
    try:
        return {r["job"]: r["cards"] for r in _jobs(conn)}
    finally:
        conn.close()


def _jobs(conn):
    return conn.execute(
        """SELECT j.job, j.company, j.role, j.updated_at,
                  (SELECT COUNT(*) FROM cards c WHERE c.job = j.job AND c.deck = 'interview') AS cards
           FROM jobs j ORDER BY j.updated_at DESC"""
    ).fetchall()


@bp.get("/jobs")
def list_jobs():
    """Jobs that have interview cards, newest first, with card counts."""
    return jsonify([dict(r) for r in _jobs(db())])


def import_job(job, company, role, cards, conn=None):
    """Create or replace the cards prep.py made for one job, in one call.

    cards is [{key, category, front, back}]. Cards written by hand are
    never touched. A made card that survives an update keeps its notes and
    review progress; one edited by hand keeps its text too. A made card
    that is gone from the new set is deleted, unless it has notes, in which
    case it stays as a hand card. Raises CueError on bad input.
    """
    if not _JOB_ID.match(job or ""):
        raise CueError("bad job id")
    company = (company or "").strip()
    if not company or not isinstance(cards, list):
        raise CueError("company and cards are required")
    clean, seen = [], set()
    for c in cards:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key") or "").strip()[:120]
        front = str(c.get("front") or "").strip()
        back = str(c.get("back") or "").strip()
        cat = c.get("category") if c.get("category") in INTERVIEW_CATS else "other"
        if not key or not front or not back or key in seen:
            continue
        seen.add(key)
        clean.append((key, cat, front, back))
    if not clean:
        raise CueError("no usable cards")

    own = conn is None
    conn = conn or _connect()
    added = updated = kept = removed = 0
    try:
        conn.execute(
            """INSERT INTO jobs (job, company, role) VALUES (?, ?, ?)
               ON CONFLICT(job) DO UPDATE SET company = excluded.company, role = excluded.role,
                   updated_at = datetime('now','localtime')""",
            (job, company, (role or "").strip()),
        )
        old = {
            r["gen_key"]: r
            for r in conn.execute(
                "SELECT id, gen_key, gen_hash, front, back, notes FROM cards WHERE job = ? AND gen_key IS NOT NULL",
                (job,),
            ).fetchall()
        }
        for key, cat, front, back in clean:
            h = _gen_hash(front, back)
            r = old.pop(key, None)
            if r is None:
                conn.execute(
                    """INSERT INTO cards (deck, category, front, back, job, gen_key, gen_hash)
                       VALUES ('interview', ?, ?, ?, ?, ?, ?)""",
                    (cat, front, back, job, key, h),
                )
                added += 1
            elif r["gen_hash"] and _gen_hash(r["front"], r["back"]) != r["gen_hash"]:
                kept += 1   # edited by hand: the user's text wins
            else:
                conn.execute(
                    """UPDATE cards SET category = ?, front = ?, back = ?, gen_hash = ?,
                           updated_at = datetime('now','localtime') WHERE id = ?""",
                    (cat, front, back, h, r["id"]),
                )
                updated += 1
        for r in old.values():
            edited = r["gen_hash"] and _gen_hash(r["front"], r["back"]) != r["gen_hash"]
            if (r["notes"] or "").strip() or edited:
                conn.execute("UPDATE cards SET gen_key = NULL, gen_hash = NULL WHERE id = ?", (r["id"],))
                kept += 1
            else:
                conn.execute("DELETE FROM cards WHERE id = ?", (r["id"],))
                removed += 1
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE job = ? AND deck = 'interview'", (job,)
        ).fetchone()[0]
    except sqlite3.Error as e:
        conn.rollback()
        raise CueError(f"could not save the cards ({e})", 500)
    finally:
        if own:
            conn.close()
    return {"job": job, "cards": count, "added": added, "updated": updated,
            "kept": kept, "removed": removed, "path": f"#cue/{job}"}


@bp.post("/jobs/<job>/import")
def import_job_cards(job):
    """Body: {company, role, cards}. See import_job."""
    d = request.get_json(silent=True) or {}
    try:
        return jsonify(import_job(job, d.get("company"), d.get("role"), d.get("cards"), db()))
    except CueError as e:
        return jsonify({"error": str(e)}), e.status


@bp.get("/stats")
def stats():
    c = db()
    total = c.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    due = c.execute("SELECT COUNT(*) FROM cards WHERE due_date <= date('now','localtime')").fetchone()[0]
    today_reviews = c.execute(
        "SELECT COUNT(*) FROM reviews WHERE date(reviewed_at) = date('now','localtime')"
    ).fetchone()[0]
    by_cat = [
        dict(r)
        for r in c.execute(
            "SELECT category, COUNT(*) AS n FROM cards GROUP BY category ORDER BY category"
        ).fetchall()
    ]
    streak_rows = c.execute(
        "SELECT DISTINCT date(reviewed_at) AS d FROM reviews ORDER BY d DESC"
    ).fetchall()
    streak = 0
    today = date.today()
    for r in streak_rows:
        d = datetime.fromisoformat(r["d"]).date()
        if d == today - timedelta(days=streak):
            streak += 1
        else:
            break
    return jsonify(
        {"total": total, "due": due, "today_reviews": today_reviews, "by_category": by_cat, "streak": streak}
    )
