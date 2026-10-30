"""Voice check and skim test for a draft, as the user edits it. No model call.

voice(text): lines that break the user's voice rules. Em dashes, the AI tells
from your "Writing in My Voice" note (optional), elevated words, and sentences
that run long. Each hint is quiet and points at a span; nothing is ever
rewritten for the user.

skim(text, question): what a recruiter skimming takes away. The first
sentence, whether it answers the question, and the concrete nouns and
numbers that survive a fast read.
"""

import re

LONG_SENTENCE = 30  # words; the drafting rule in answers.DRAFT_SYSTEM

# Elevated words. The first row is the "Watch for" list in Writing in My
# Voice; the rest are the consultant words the drafting rules forbid.
# load_watch_words() adds whatever the note lists today.
ELEVATED = [
    "seamlessly", "seamless", "ultimately", "leverage", "leveraged",
    "leveraging", "robust", "holistic", "ensure", "ensures", "ensuring",
    "comprehensive",
    "high-leverage", "at scale", "step-change", "north star", "drive impact",
    "driving impact", "unlock", "unlocks", "unlocking", "user-centric",
    "innovative", "passionate", "proven track record", "results-driven",
    "forward-thinking", "empower", "empowers", "empowering", "synergy",
    "spearheaded", "spearhead", "delve", "tapestry", "landscape",
    "cutting-edge", "game-changer", "game-changing", "paradigm",
    "elevate", "elevated", "elevating", "foster", "fostering", "pivotal",
    "meticulous", "meticulously", "invaluable", "visceral",
    "intentionality", "myriad", "plethora", "utilize", "utilized",
    "utilizing", "endeavor", "furthermore", "moreover",
]

# Phrases the drafting rules call out as tells.
TELL_PHRASES = [
    ("i am excited about", "Stock opener. Say the thing that interests you."),
    ("i'm excited about", "Stock opener. Say the thing that interests you."),
    ("i am excited to", "Stock opener. Say the thing that interests you."),
    ("i'm excited to", "Stock opener. Say the thing that interests you."),
    ("i am drawn to", "Stock phrase. Name what it is about them."),
    ("i'm drawn to", "Stock phrase. Name what it is about them."),
    ("aligns with", "Consultant phrase. Say how it fits, plainly."),
    ("align with", "Consultant phrase. Say how it fits, plainly."),
    ("resonates with", "Consultant phrase. Say what it connects to."),
    ("resonated with", "Consultant phrase. Say what it connects to."),
    ("exactly the kind of", "Names the category instead of the thing."),
    ("the kind of question", "Names the category instead of the thing."),
    ("is precisely why", "Built-to-land line. Give the reason as information."),
    ("that taught me", "Setup then payoff. Say what happened instead."),
    ("what i learned was", "Setup then payoff. Say what happened instead."),
    ("not just", "The \"X, not Y\" snap. Keep at most one, mid paragraph."),
    ("not only", "The \"X, not Y\" snap. Keep at most one, mid paragraph."),
]

_SNAP_END = re.compile(r",\s*not\s+[^,.;!?]{1,40}[.!?]?$", re.I)
_SAME_DIFF = re.compile(r"\bsame\s+\w+(?:\s+\w+)?,\s*different\s+\w+", re.I)


def load_watch_words(note):
    """Extra elevated words from the voice note's "Watch for:" line."""
    m = re.search(r"Watch for:\s*([^\n]+(?:\n(?!\s*\n)[^\n]+)*)", note or "")
    if not m:
        return []
    words = re.split(r",|\band\b", m.group(1).rstrip(". "))
    return [w.strip(" .").lower() for w in words
            if w.strip(" .") and len(w.strip()) < 30]


def sentences(text):
    """(start, end, sentence) spans. Splits on . ! ? and blank lines."""
    out = []
    for m in re.finditer(r"[^.!?\n]+(?:[.!?]+[\"')\]]*|\n|$)", text or ""):
        s = m.group(0)
        if s.strip():
            lead = len(s) - len(s.lstrip())
            out.append((m.start() + lead, m.end(),
                        s.strip()))
    return out


def _word_count(s):
    return len(re.findall(r"[A-Za-z0-9][\w'%.-]*", s))


def voice(text, extra_words=()):
    """Hints for lines that break the user's voice rules.

    Returns [{start, end, kind, quote, message}], in text order. kind is
    one of dash, tell, word, long. Deterministic, so it runs on every edit.
    """
    text = text or ""
    hints = []

    def add(start, end, kind, message):
        hints.append({"start": start, "end": end, "kind": kind,
                      "quote": text[start:end], "message": message})

    for m in re.finditer("[" + chr(0x2014) + chr(0x2013) + r"]|(?<=\w)\s--\s(?=\w)", text):
        add(m.start(), m.end(), "dash",
            "Dash. Use a period, comma or colon instead.")

    low = text.lower()
    for phrase, why in TELL_PHRASES:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", low):
            add(m.start(), m.end(), "tell", why)

    words = list(dict.fromkeys([w.lower() for w in ELEVATED] +
                               [w.lower() for w in extra_words or ()]))
    for w in words:
        for m in re.finditer(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", low):
            add(m.start(), m.end(), "word",
                "Elevated word. Use a plain one.")

    spans = sentences(text)
    for i, (start, end, s) in enumerate(spans):
        n = _word_count(s)
        if n > LONG_SENTENCE:
            add(start, end, "long",
                f"{n} words. Break it in two.")
        if _SNAP_END.search(s) and _ends_paragraph(text, end):
            add(start, end, "tell",
                "Ends a paragraph on an \"X, not Y\" snap. End on information.")
        m = _SAME_DIFF.search(s)
        if m:
            add(start + m.start(), start + m.end(), "tell",
                "\"Same X, different Y\" fragment pair. Spell out what changed.")
        if s.endswith("?") and i + 1 < len(spans):
            nxt = spans[i + 1][2]
            if _word_count(nxt) <= 8:
                add(start, spans[i + 1][1], "tell",
                    "Question answered by a short line. Say it directly.")

    # The same tell found twice (a phrase inside a longer one) shows once.
    hints.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    out = []
    for h in hints:
        if any(o["kind"] == h["kind"] and o["start"] <= h["start"]
               and h["end"] <= o["end"] and o is not h for o in out) \
                and h["kind"] != "long":
            continue
        out.append(h)
    return out


def _ends_paragraph(text, end):
    rest = text[end:]
    return not rest.strip() or rest.lstrip(" \t").startswith("\n")


# ---------- skim test ----------

_STOP = set("""a an the and or but if of to in on at for with from by about
as into over under is are was were be been being do does did have has had i
me my you your we our us they their it its this that these those what which
who whom whose why how when where would could should will can may might
tell describe share please give explain briefly time one some any there here
so than then also just more most very much many such own yourself
""".split())

_WARMUP = re.compile(
    r"^\s*(?:great question|thank you|thanks for|throughout my (?:career|life)|"
    r"in today's|as a (?:designer|product designer|seasoned|passionate)|"
    r"i have always|i've always|ever since|from a young age|growing up|"
    r"when i was (?:young|a kid|little)|having worked|over the (?:years|course)|"
    r"in my (?:\d+|many|several) years|it's no secret|in a world)", re.I)

_YES_NO_Q = re.compile(r"^\s*(?:do|does|are|is|have|has|can|will|would|"
                       r"did|were|was)\b", re.I)
_YES_NO_A = re.compile(r"^\s*(?:yes|no|yeah|not yet|i do|i don't|i am|"
                       r"i'm|i have|i've|i can|i will|i would|i did|i was)\b",
                       re.I)
_WHY_Q = re.compile(r"\bwhy\b|interest|excite|motivat|draws? you|appeal|"
                    r"want to (?:work|join)", re.I)
_STORY_Q = re.compile(r"tell us about|describe|share an? |give an example|"
                      r"walk us through|a time when|a project", re.I)
_REASON = re.compile(r"\b(?:because|so|since|want|wanted|interest|like|"
                     r"care|fit|match|built|build|work|worked|design|"
                     r"designed|shipped)\b", re.I)


def _stem(w):
    w = w.lower().strip("'")
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content(t):
    return {_stem(w) for w in re.findall(r"[A-Za-z][A-Za-z'-]+", t or "")
            if w.lower() not in _STOP and len(w) > 2}


def keepers(text, limit=6):
    """The concrete bits a skimmer remembers: numbers and proper names."""
    found = []
    for m in re.finditer(r"(?<![\w.])(?:\$?\d[\d,.]*(?:\+|%|x|k|K|M)?"
                         r"(?:\s?(?:percent|x|times|users|people|screens|"
                         r"drivers|policies|weeks|months|years))?)", text or ""):
        tok = m.group(0).strip().rstrip(".,")
        if tok and re.search(r"\d", tok):
            found.append(tok)
    for s in sentences(text):
        words = re.findall(r"[A-Za-z][\w'&.-]*", s[2])
        run = []
        for i, w in enumerate(words):
            proper = w[0].isupper() and i > 0 and w.lower() not in _STOP \
                and w not in ("I", "I'm", "I've", "I'd")
            acronym = len(w) >= 2 and w.isupper() and w not in ("I",)
            if proper or acronym:
                run.append(w.rstrip(".,"))
            else:
                if run:
                    found.append(" ".join(run))
                run = []
        if run:
            found.append(" ".join(run))
    out = []
    for f in found:
        if f and f.lower() not in (o.lower() for o in out):
            out.append(f)
    return out[:limit]


def skim(text, question="", company=""):
    """{first, answers, why, keeps, line}: the one-line skim readout.

    answers is False when the first sentence does not answer the question;
    the card then warns. Deterministic and rough on purpose: it reads the
    way a recruiter reads, which is the first line and the numbers.
    """
    text = (text or "").strip()
    spans = sentences(text)
    if not spans:
        return {"first": "", "answers": False, "why": "Nothing written yet.",
                "keeps": [], "line": ""}
    first = spans[0][2]
    q = (question or "").strip()
    answers, why = True, ""
    if _WARMUP.search(first):
        answers, why = False, "Opens with a warm-up line, not the answer."
    elif first.endswith("?"):
        answers, why = False, "Opens with a question."
    elif q and _YES_NO_Q.search(q) and not _STORY_Q.search(q):
        if not _YES_NO_A.search(first):
            answers, why = False, "A yes or no question. Say yes or no first."
    elif q and _WHY_Q.search(q):
        named = company and company.lower() in first.lower()
        overlap = _content(first) & _content(q)
        if not (named or overlap or _REASON.search(first)):
            answers, why = False, "Does not give a reason in the first line."
    elif q and _STORY_Q.search(q):
        if not (keepers(first) or _content(first) & _content(q)):
            answers, why = False, ("Does not name the project or what you "
                                   "did in the first line.")
    elif q:
        if not _content(first) & _content(q):
            answers, why = False, ("The first line does not touch what the "
                                   "question asks.")
    keeps = keepers(text)
    line = first if len(first) <= 160 else first[:157].rsplit(" ", 1)[0] + "..."
    return {"first": first, "answers": answers, "why": why, "keeps": keeps,
            "line": line}


def check(text, question="", company="", voice_note=""):
    """Both checks at once, for the field card."""
    return {"hints": voice(text, load_watch_words(voice_note)),
            "skim": skim(text, question, company)}
