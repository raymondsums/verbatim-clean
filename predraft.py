"""Reading questions ahead: drafts made in the background, one at a time.

When the panel finds written questions on a form, it hands them here.
One worker drafts them in order, one model call per question, and waits
whenever the user has a draft of the user's own running. Results sit in memory until
the user opens the field. Nothing is ever put in a form from here; the field
card shows the draft and the user clicks Insert.

A page that asks applicants not to use AI is never read ahead. The server
checks the posting itself before queueing, whatever the panel says.
"""

import threading
import time
from collections import OrderedDict

import answers as answers_mod

KEEP_SECONDS = 6 * 3600
MAX_QUEUE = 12
MAX_RESULTS = 200

_lock = threading.Lock()
_wake = threading.Event()
_queue = []                 # job dicts, oldest first
_results = OrderedDict()    # key -> {state, result|error, ts}
_worker = None

# How a draft is made. The server sets this to a function(job) -> result
# dict, so this module needs no Flask and tests can stub it.
DRAFT = None


def key(page, question):
    """One question on one posting. page is the posting URL without its
    query, so the apply page and the posting page share drafts."""
    page = (page or "").split("#")[0].split("?")[0].rstrip("/")
    page = page.split("/apply")[0]
    q = answers_mod.clean_question(question).lower()
    return f"{page}\n{q}"


def _prune():
    now = time.time()
    for k in [k for k, v in _results.items() if now - v["ts"] > KEEP_SECONDS]:
        del _results[k]
    while len(_results) > MAX_RESULTS:
        _results.popitem(last=False)


def enqueue(page, jobs):
    """Queue drafts for questions not already drafted or waiting.

    jobs: [{question, ...whatever DRAFT needs}]. Returns the keys queued.
    """
    queued = []
    with _lock:
        _prune()
        for job in jobs:
            q = answers_mod.clean_question(job.get("question"))
            if not q or answers_mod.classify_guarded(q):
                continue
            k = key(page, q)
            have = _results.get(k)
            if have and have["state"] in ("waiting", "drafting", "ready"):
                continue
            if len(_queue) >= MAX_QUEUE:
                break
            _results[k] = {"state": "waiting", "ts": time.time()}
            _queue.append(dict(job, question=q, key=k))
            queued.append(k)
    if queued:
        _start()
        _wake.set()
    return queued


def waiting_questions(job_key):
    """Other questions still waiting on the same page as this job."""
    page = (job_key or "").rsplit("\n", 1)[0] + "\n"
    with _lock:
        return [j["question"] for j in _queue if j["key"].startswith(page)]


def status(page, questions):
    """{question: {state, result?, error?}} for each question asked about."""
    out = {}
    with _lock:
        for q in questions or ():
            hit = _results.get(key(page, q))
            if hit:
                out[q] = {k: v for k, v in hit.items() if k != "ts"}
    return out


def take(page, question):
    """A ready draft for this field, or None."""
    with _lock:
        hit = _results.get(key(page, question))
        if hit and hit["state"] == "ready":
            return hit.get("result")
    return None


def cancel(page):
    """Drop everything still waiting for a page (Draft anyway turned off,
    or a no-AI line found after the fact)."""
    prefix = key(page, "x").rsplit("\n", 1)[0] + "\n"
    with _lock:
        _queue[:] = [j for j in _queue if not j["key"].startswith(prefix)]
        for k in [k for k in _results if k.startswith(prefix)
                  and _results[k]["state"] == "waiting"]:
            del _results[k]


def _start():
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name="predraft", daemon=True)
        _worker.start()


def _next():
    with _lock:
        return _queue.pop(0) if _queue else None


def _run():
    while True:
        job = _next()
        if job is None:
            _wake.clear()
            if not _wake.wait(timeout=300):
                global _worker
                with _lock:
                    if _queue:
                        continue
                    _worker = None
                    return  # idle; started again on the next enqueue
            continue
        # The user's own drafts first. Wait while one is running.
        waited = 0
        while answers_mod.interactive_busy() and waited < 600:
            time.sleep(2)
            waited += 2
        with _lock:
            if job["key"] in _results:
                _results[job["key"]].update(state="drafting", ts=time.time())
        try:
            result = DRAFT(job) if DRAFT else None
            entry = {"state": "ready", "result": result, "ts": time.time()}
        except Exception as e:  # one failed question never stops the rest
            entry = {"state": "failed", "error": str(e), "ts": time.time()}
        with _lock:
            if job["key"] in _results:
                _results[job["key"]] = entry
