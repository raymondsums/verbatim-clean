"""Run every replica form through the real content script, headlessly.

    python3 tests/run_pages.py [name ...]

Each page is served with ?harness=1, which injects the extension's own
content.js plus a stub of the chrome.runtime messaging it normally uses.
So this exercises the shipping fill code, not a copy of it, without a
browser extension installed. The page posts its result to
/api/test/report and this prints what came back.

Needs the server running on 5053.
"""

import json
import os
import re
import tempfile
import subprocess
import sys
import time
import urllib.error
import urllib.request

CHROME = os.environ.get("VERBATIM_TEST_BROWSER",
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
BASE = os.environ.get("VERBATIM_BASE", "http://localhost:5053")
ATS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ats")


def run_page(name):
    # A fresh throwaway profile each time, never a real one.
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={prof}", "--no-first-run",
             "--virtual-time-budget=20000", "--dump-dom",
             f"{BASE}/test/{name}?harness=1"],
            capture_output=True, text=True, timeout=90)
    time.sleep(0.5)
    with urllib.request.urlopen(f"{BASE}/api/test/report", timeout=5) as r:
        return json.load(r)


def expectations(html):
    """{label fragment: value} from a page's <script id="expect">, if any.
    An empty value means the field must be left empty (flagged); "*"
    checks only that a field with that label exists."""
    m = re.search(r'<script type="application/json" id="expect">(.*?)</script>',
                  html, re.S)
    return json.loads(m.group(1)) if m else {}


def panel_expectations(html):
    """What the panel must show at the top: <script id="expect-panel">.
    no_ai, instructions and traps list text that must appear in a quote;
    not_instructions and not_traps list text that must not; predraft is
    "on" or "off"."""
    m = re.search(r'<script type="application/json" id="expect-panel">(.*?)</script>',
                  html, re.S)
    return json.loads(m.group(1)) if m else None


def check_panel(rep, want):
    got = (rep.get("result") or {}).get("posting") or {}
    if not got:
        return ["the panel reported no posting checks"]
    bad = []
    for key in ("no_ai", "instructions", "traps"):
        quotes = got.get(key) or []
        for frag in want.get(key) or []:
            if not any(frag.lower() in q.lower() for q in quotes):
                bad.append(f"{key}: nothing quoting {frag!r} (got {quotes})")
        if not want.get(key) and quotes:
            bad.append(f"{key}: expected none, got {quotes}")
    for key, src in (("not_instructions", "instructions"), ("not_traps", "traps")):
        for frag in want.get(key) or []:
            hit = [q for q in got.get(src) or [] if frag.lower() in q.lower()]
            if hit:
                bad.append(f"{src}: must not include {frag!r} (got {hit})")
    if want.get("predraft") and got.get("predraft") != want["predraft"]:
        bad.append(f"predraft: wanted {want['predraft']!r}, got {got.get('predraft')!r}")
    shown = set(got.get("shown") or [])
    for key, card in (("no_ai", "noai"), ("instructions", "instructions"),
                      ("traps", "traps")):
        if want.get(key) and card not in shown:
            bad.append(f"the {card} card is not on the panel")
    return bad


def canon(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower().replace("\u2019", "'")).strip()


def check(rep, expect):
    """Lines describing each expectation the report does not meet."""
    bad = []
    fields = rep.get("fields", [])
    for frag, want in expect.items():
        # "School#2" is the second field labelled like "School" (rows).
        name, _, nth = frag.partition("#")
        n = int(nth) if nth else 1
        hits = [f for f in fields if name.lower() in (f.get("label") or "").lower()]
        if len(hits) < n:
            bad.append(f"no field labelled like {frag!r}")
            continue
        got = hits[n - 1].get("value") or ""
        if want == "*":
            continue  # only the label is checked
        if canon(got) != canon(want):
            bad.append(f"{frag!r}: wanted {want!r}, got {got!r}")
    return bad


def main(argv):
    try:
        urllib.request.urlopen(f"{BASE}/api/health", timeout=3)
    except (urllib.error.URLError, OSError):
        print(f"server not answering on {BASE}. Start it first.")
        return 1

    names = argv or sorted(f[:-5] for f in os.listdir(ATS_DIR)
                           if f.endswith(".html"))
    problems = 0
    for name in names:
        # Some replicas exist to be clicked through by hand with the
        # extension loaded, because what they test lives in the popup.
        # They carry no autotest hook, so there is nothing to run here.
        with open(os.path.join(ATS_DIR, f"{name}.html"), encoding="utf-8") as f:
            html = f.read()
            if "job-toolkit-autotest" not in html:
                print(f"{name:16} manual only, open it with the extension")
                continue
        try:
            rep = run_page(name)
        except (subprocess.TimeoutExpired, OSError, ValueError) as e:
            print(f"{name:16} could not run: {e}")
            problems += 1
            continue
        if rep.get("page", "").lstrip("/").split("/")[-1] != name:
            print(f"{name:16} no fresh report (page did not run)")
            problems += 1
            continue
        res = rep.get("result", {})
        if res.get("error"):
            print(f"{name:16} error: {res['error']}")
            problems += 1
            continue
        line = (f"{name:16} filled {res.get('filled', 0):2}  "
                f"flagged {res.get('flagged', 0):2}  "
                f"of {res.get('total', 0):2}")
        if res.get("unidentified"):
            line += f"  unidentified {res['unidentified']}"
        if res.get("uploaded"):
            line += f"  uploaded {res['uploaded']}"
        print(line)
        for q in res.get("questions", []):
            lim = ", ".join(f"{v} {k}" for k, v in (q.get("limit") or {}).items())
            print(f"    question: {q['label'][:44]:44} {q['kind']:11} {lim}")
        for f in rep.get("fields", []):
            if f.get("outline"):
                print(f"    flagged: {f['sig'][:60]}")
        for line in check(rep, expectations(html)):
            print(f"    EXPECT FAIL {line}")
            problems += 1
        want = panel_expectations(html)
        if want is not None:
            got = (res.get("posting") or {})
            print(f"    panel: no-AI {len(got.get('no_ai') or [])}, "
                  f"instructions {len(got.get('instructions') or [])}, "
                  f"traps {len(got.get('traps') or [])}, "
                  f"hidden found {got.get('hidden_found', 0)}, "
                  f"read ahead {got.get('predraft') or '?'}")
            for line in check_panel(rep, want):
                print(f"    EXPECT FAIL {line}")
                problems += 1
    print("\nall pages ran" if not problems else f"\n{problems} page(s) failed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
