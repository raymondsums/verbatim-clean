"""Short samples of the user's own writing, for drafts to copy how they sound.

The voice rules say what to avoid. These show what the user actually sounds
like: answers the user sent that count as their own words
(answers.own_versions), answers typed into a form with their own keys, what
they said out loud into the mic before it was tidied, and, when set up, a few
notes and portfolio pages of their own.

Sources, all optional:
- "Voice Samples.md" in the notes folder: pieces the user wrote to a reader
  with no AI, as quote blocks under one "##" section each. Only the quoted
  text is taken. These rank first and are read each time.
- VOICE_SAMPLE_NOTES in .env: a comma separated list of note file names in
  the notes folder that the user wrote themselves, about work.
- PORTFOLIO_DIR in .env: a folder of the user's case study pages (.html).
  PORTFOLIO_FILES limits it to a comma separated list of file names.

Style only. A sample is never a source of facts: drafts are not allowed to
take a number, a project or an opinion from one, and none of this text is
added to the source check in answers.check_sources.

The notes and portfolio part is cached in data/voice_samples.json
(gitignored, it is personal writing). A source missing on a rebuild keeps
its cached samples instead of dropping them. Typed and spoken answers are
read from the logs each time, so they count as soon as the user writes them.
"""

import html
import json
import os
import re
import time
from datetime import date
from html.parser import HTMLParser

import voicecheck

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "voice_samples.json")
SPOKEN_LOG = os.path.join(BASE_DIR, "logs", "spoken.jsonl")
PORTFOLIO_DIR = os.path.expanduser(os.environ.get("PORTFOLIO_DIR", ""))
REBUILD_AFTER = 7 * 86400

# Notes the user wrote themselves, about work. A fixed list on purpose:
# a notes folder usually holds research and prep sheets too, and some notes
# are personal and must never reach a job application prompt.
VAULT_NOTES = tuple(n.strip() for n in os.environ.get("VOICE_SAMPLE_NOTES", "").split(",")
                    if n.strip())

# The user's case study pages. Empty means every .html file in PORTFOLIO_DIR.
CASE_STUDIES = tuple(n.strip() for n in os.environ.get("PORTFOLIO_FILES", "").split(",")
                     if n.strip())

# The user's own finished writing, one quote block a sample.
VOICE_NOTE = "Voice Samples.md"

PICK = 3          # samples per draft
# Raw writing first: what the user typed or said, then the user's own notes, then the
# case studies, which went through editing passes.
BONUS = {"written": 6, "sent": 5, "typed": 4, "spoken": 4}
SAMPLE_WORDS = 90  # each one trimmed to about this
MIN_WORDS = 20

_STOP = set("""a an and are as at be but by for from has have i i'm in into is
it its it's me my of on or so that the their them they this to was we were
what when which who will with you your our us can do does did not no yes
would could should about more most also just than then there these those""".split())

# Text that is not the user's: lines about Claude, quoted research, and em or en
# dashes, which in the user's files mark copy an AI wrote or rewrote.
_NOT_HIS = re.compile(r"claude|^[\"" + chr(0x201C) + "]|[" + chr(0x2014)
                      + chr(0x2013) + "]", re.I)


def _sounds_like_him(text):
    """No AI tells by the user's own voice check. Long sentences are allowed:
    the user's own notes run on, and that is how the user writes them."""
    if "?" in text or _SNAP.search(text):
        return False
    return not [h for h in voicecheck.voice(text) if h["kind"] != "long"]


# The "X, not Y" snap anywhere in a sample, and the clipped "Two things,
# one idea" pair. Case study copy picked these up in editing passes.
_SNAP = re.compile(r"\b\w+,\s+not\s+\w+[.!?]|\b(?:isn't|wasn't|not) just\b|\b\w+ \w+, one \w+\b", re.I)


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _clean_md(line):
    line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
    line = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
                  lambda m: m.group(2) or m.group(1), line)
    line = re.sub(r"[*_`]+", "", line)
    return line.strip()


def _chunks(pieces):
    """Lines or paragraphs grouped into samples of MIN_WORDS to about
    SAMPLE_WORDS words, keeping the user's order."""
    out, cur = [], []
    for p in pieces:
        p = " ".join(p.split())
        if not p or _NOT_HIS.search(p):
            if cur:
                out.append(" ".join(cur))
                cur = []
            continue
        if not re.search(r"[.!?:;)]$", p):
            p += "."
        cur.append(p)
        if len(" ".join(cur).split()) >= SAMPLE_WORDS * 0.7:
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return [_trim(c) for c in out
            if len(c.split()) >= MIN_WORDS and _sounds_like_him(c)]


def _trim(text, words=SAMPLE_WORDS):
    """At most about words long, cut at a sentence end when there is one."""
    parts = text.split()
    if len(parts) <= words:
        return text.strip()
    cut = " ".join(parts[:words])
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > len(cut) // 2 else cut).strip()


def _find_notes(vault_dir, names=None):
    """name -> path for each note in names, wherever it sits in the
    notes folder. Matched by exact
    file name only, so nothing off the list is ever read. The note nearest
    the root wins when a name is in more than one folder; hidden folders
    (.obsidian, .trash) are skipped."""
    names = VAULT_NOTES if names is None else names
    found = {}
    for root, dirs, files in os.walk(vault_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        depth = root.count(os.sep)
        for name in names:
            if name in files and (name not in found or depth < found[name][0]):
                found[name] = (depth, os.path.join(root, name))
    return {name: path for name, (_, path) in found.items()}


def vault_samples(vault_dir):
    out = []
    paths = _find_notes(vault_dir)
    for name in VAULT_NOTES:
        try:
            with open(paths[name], encoding="utf-8") as f:
                text = f.read()
        except (KeyError, OSError):
            continue
        lines = [_clean_md(ln) for ln in text.split("\n")
                 if not ln.lstrip().startswith(("#", ">", "|", "---"))]
        for c in _chunks(lines):
            out.append({"from": "vault", "where": name[:-3], "text": c})
    return out


# Only text the user wrote themselves (human first): a section marked as
# unconfirmed or AI-polished is never a sample.
_UNCONFIRMED = re.compile(r"provenance unconfirmed|ai[- ]polished", re.I)


def written_samples(vault_dir):
    """The quote blocks in the Voice Samples note, one sample per "##"
    section. Not run through the voice check: the user wrote these themselves. A
section marked "provenance unconfirmed" or "AI-polished" is skipped
whole."""
    path = _find_notes(vault_dir, (VOICE_NOTE,)).get(VOICE_NOTE)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (TypeError, OSError):
        return []
    out = []
    for sec in re.split(r"(?m)^##\s+", text)[1:]:
        if _UNCONFIRMED.search(sec):
            continue
        title = sec.split("\n", 1)[0].strip()
        quote = " ".join(re.sub(r"^\s*>\s?", "", ln) for ln in sec.split("\n")
                         if ln.lstrip().startswith(">"))
        quote = " ".join(quote.split())
        if len(quote.split()) >= MIN_WORDS:
            out.append({"from": "written", "where": title, "text": _trim(quote)})
    return out


class _Text(HTMLParser):
    """The paragraphs and list items of a page, skipping scripts, styles,
    navigation and footers."""

    SKIP = {"script", "style", "nav", "footer", "header", "svg", "noscript"}
    KEEP = {"p", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.keep = 0
        self.buf = []
        self.paras = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.KEEP and not self.skip:
            self.keep += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.KEEP and self.keep:
            self.keep -= 1
            if not self.keep:
                self.paras.append("".join(self.buf))
                self.buf = []

    def handle_data(self, data):
        if self.keep and not self.skip:
            self.buf.append(data)


def portfolio_samples(portfolio_dir):
    out = []
    if not portfolio_dir or not os.path.isdir(portfolio_dir):
        return out
    names = CASE_STUDIES or tuple(sorted(n for n in os.listdir(portfolio_dir)
                                         if n.endswith(".html")))
    for name in names:
        try:
            with open(os.path.join(portfolio_dir, name), encoding="utf-8") as f:
                page = f.read()
        except OSError:
            continue
        p = _Text()
        try:
            p.feed(page)
        except Exception:
            continue
        paras = [html.unescape(x) for x in p.paras if len(x.split()) >= 8]
        for c in _chunks(paras):
            out.append({"from": "portfolio", "where": name[:-5], "text": c})
    return out


def _read_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data.get("samples"), list) else None
    except (OSError, ValueError, AttributeError):
        return None


def build(vault_dir):
    """Rebuild the cache from whatever is on this machine. A source that
    is missing here keeps its cached samples.
    Returns the samples."""
    old = (_read_cache() or {}).get("samples") or []
    fresh = {"vault": vault_samples(vault_dir),
             "portfolio": portfolio_samples(PORTFOLIO_DIR)}
    samples = []
    for kind, got in fresh.items():
        samples += got or [s for s in old if s.get("from") == kind]
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"built": date.today().isoformat(),
                       "samples": samples}, f, indent=1)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass
    return samples


def stored(vault_dir):
    """The cached vault and portfolio samples, rebuilt once a week."""
    cache = _read_cache()
    try:
        age = time.time() - os.path.getmtime(CACHE_PATH)
    except OSError:
        age = None
    if cache is None or age is None or age > REBUILD_AFTER:
        try:
            return build(vault_dir)
        except Exception:
            return (cache or {}).get("samples") or []
    return cache["samples"]


def _jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def own_answers(versions_log):
    """Answers the user typed themselves (the extension saw the user's keystrokes, nothing
    pasted or filled by a tool) and what the user said into the mic, raw."""
    out = []
    for e in _jsonl(versions_log):
        if e.get("typed") and not e.get("inserted") and e.get("sent"):
            out.append({"from": "typed", "where": e.get("group") or "",
                        "text": _trim(e["sent"])})
    for e in _jsonl(SPOKEN_LOG):
        if len((e.get("raw") or "").split()) >= MIN_WORDS:
            out.append({"from": "spoken", "where": e.get("question") or "",
                        "text": _trim(e["raw"])})
    return out[-40:]


def log_spoken(question, raw):
    """What the user said into the mic, before any tidying. Local only."""
    if len((raw or "").split()) < MIN_WORDS:
        return
    try:
        os.makedirs(os.path.dirname(SPOKEN_LOG), exist_ok=True)
        with open(SPOKEN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": date.today().isoformat(),
                                "question": question or "",
                                "raw": raw}) + "\n")
    except OSError:
        pass


def pick(question, posting, vault_dir, versions_log, n=PICK, sent=()):
    """A few short samples that fit this question, for style only.

    sent is the text of answers the user sent that count as the user's. Scored by plain
    word overlap with the question and the posting, with a nudge toward
    what the user sent and typed (BONUS). At most one sample from each answer or
    page and two from the portfolio, so the case studies do not crowd out
    the rest.
    """
    try:
        pool = [{"from": "sent", "where": str(i), "text": _trim(t)}
                for i, t in enumerate(sent or ())
                if len((t or "").split()) >= MIN_WORDS]
        pool += own_answers(versions_log)
        pool += written_samples(vault_dir)
        pool += [x for x in stored(vault_dir) if x.get("from") != "vault"]
    except Exception:
        return []
    if not pool:
        return []
    want = {w for w in _words(question) if w not in _STOP}
    near = {w for w in _words((posting or "")[:3000]) if w not in _STOP}

    def score(s):
        words = set(_words(s["text"]))
        return (3 * len(words & want) + len(words & near) / 4
                + BONUS.get(s["from"], 0))

    out, seen, pages = [], set(), 0
    for s in sorted(pool, key=score, reverse=True):
        key = (s["from"], s.get("where"))
        if key in seen or (s["from"] == "portfolio" and pages >= 2):
            continue
        pages += s["from"] == "portfolio"
        seen.add(key)
        out.append(s["text"])
        if len(out) >= n:
            break
    return out


def prompt_block(samples):
    """The prompt section for samples, or "" when there are none."""
    samples = [s for s in samples or () if (s or "").strip()]
    if not samples:
        return ""
    body = "\n\n".join(f'"{s.strip()}"' for s in samples)
    return ("\n# How the user writes: samples of the user's own writing\n"
            "Style only. Match how these sound. Nothing in them is a "
            "source: never take a fact, number, project, name or opinion "
            "from them, and never quote them.\n\n" + body + "\n")
