"""Fill log: one JSON line per fill, counts and signatures only.

Replaces the coverage guess with a real number. Each fill writes a line
with how many fields were filled, flagged, skipped and left unmapped.
When you change a filled value, a correction line follows, keyed to the
fill by its id, naming the field signatures that changed. No values are
ever written here, not even the corrected ones.

    python3 filllog.py      accuracy per domain, from logs/fills.jsonl
"""

import json
import os
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "logs", "fills.jsonl")

# Test pages are not applications, so they stay out of the numbers.
LOCAL_DOMAINS = ("localhost", "127.0.0.1")

_LOCK = threading.Lock()

# Host fragment -> ATS name. The host is the only reliable signal, and an
# unknown host just logs without one.
ATS_HOSTS = [
    ("greenhouse", "greenhouse"), ("lever.co", "lever"),
    ("ashbyhq", "ashby"), ("myworkdayjobs", "workday"),
    ("workday", "workday"), ("rippling", "rippling"),
    ("smartrecruiters", "smartrecruiters"), ("icims", "icims"),
    ("taleo", "taleo"), ("jobvite", "jobvite"), ("bamboohr", "bamboohr"),
    ("workable", "workable"), ("breezy", "breezy"),
]


def ats_type(domain):
    domain = (domain or "").lower()
    for fragment, name in ATS_HOSTS:
        if fragment in domain:
            return name
    return ""


def _append(entry):
    with _LOCK:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def log_fill(domain, filled, flagged, skipped, unmapped):
    """Record one fill. Returns its id, or None for a test page."""
    domain = (domain or "").strip().lower()
    if not domain or domain in LOCAL_DOMAINS:
        return None
    fill_id = uuid.uuid4().hex[:12]
    try:
        _append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "fill",
                 "fill_id": fill_id, "domain": domain,
                 "ats": ats_type(domain), "filled": filled,
                 "flagged": flagged, "skipped": skipped,
                 "unmapped": unmapped})
    except OSError:
        return None  # logging must never cost a fill
    return fill_id


def log_correction(domain, fill_id, signatures):
    """Record that filled values were changed. Signatures only."""
    domain = (domain or "").strip().lower()
    signatures = [str(s) for s in (signatures or []) if s]
    if not domain or domain in LOCAL_DOMAINS or not signatures:
        return
    try:
        _append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "event": "correction", "fill_id": fill_id or None,
                 "domain": domain, "count": len(signatures),
                 "signatures": signatures})
    except OSError:
        pass


def stats(path=None):
    """Per domain: fills, field counts, corrections and accuracy.

    accuracy is the share of filled fields you did not change afterwards.
    coverage is the share of fields seen that were filled.
    """
    out = {}
    try:
        with open(path or LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        d = out.setdefault(e.get("domain", "?"), {
            "ats": e.get("ats", ""), "fills": 0, "filled": 0, "flagged": 0,
            "skipped": 0, "unmapped": 0, "corrected": 0})
        if e.get("event") == "fill":
            d["fills"] += 1
            d["ats"] = d["ats"] or e.get("ats", "")
            for k in ("filled", "flagged", "skipped", "unmapped"):
                d[k] += int(e.get(k) or 0)
        elif e.get("event") == "correction":
            d["corrected"] += int(e.get("count") or 0)
    for d in out.values():
        seen = d["filled"] + d["flagged"] + d["skipped"] + d["unmapped"]
        d["accuracy"] = (round(1 - min(d["corrected"], d["filled"])
                               / d["filled"], 3) if d["filled"] else None)
        d["coverage"] = round(d["filled"] / seen, 3) if seen else None
    return out


def _pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def main():
    rows = stats()
    if not rows:
        print(f"No fills logged yet ({LOG_PATH}).")
        return
    print(f"{'domain':32} {'ats':16} {'fills':>5} {'filled':>6} "
          f"{'fixed':>5} {'accuracy':>8} {'coverage':>8}")
    for domain, d in sorted(rows.items(), key=lambda kv: -kv[1]["fills"]):
        print(f"{domain[:32]:32} {d['ats'][:16]:16} {d['fills']:>5} "
              f"{d['filled']:>6} {d['corrected']:>5} "
              f"{_pct(d['accuracy']):>8} {_pct(d['coverage']):>8}")


if __name__ == "__main__":
    main()
