"""The user's interview scripts and practice answers, as one more source.

The notes folder holds what the user prepared and said out loud for
interviews: the Interviews/ folder and any "Interview ..." notes at the top
of the folder.
They answer the questions application forms ask (tell me about yourself,
why are you looking, a time you disagreed), so drafts can rest on them.

Read only, never written. Only the parts that are the user's answers are kept:
scripts, intros, stories and practice answers. Company research, recruiter
notes, questions to ask them, and anything about pay, visas or sponsorship
are dropped before any of it gets near a prompt. "Why <company>" scripts
are dropped too, since every reason in them belongs to that company.
"""

import re

import vaultio

FOLDER = "Interviews"
ROOT_PREFIX = "Interview "
MAX_CHARS = 5000

_KEEP = re.compile(
    r"script|tell me about yourself|intro|looking|leaving|layoff|career goal|"
    r"\bstar\b|stor(?:y|ies)|practice|answers?\b|fits me|"
    r"talking points|walkthrough|bridging|motivation|behavioral|"
    r"portfolio|product sense|trust|collaboration|process\b|"
    r"ambiguity|critique|redesign|explain|influence|sets a|working with|"
    r"opening|experience|design pov|^\d+\.", re.I)

_DROP = re.compile(
    r"sponsor|visa|immigration|salary|\bcomp\b|compensation|\bpay\b|offer|"
    r"recruiter:|your recruiter|questions? (?:to ask|for them|for you)|"
    r"\bask (?:him|them|the)|sources|controvers|glassdoor|health read|"
    r"elsewhere|timeline|logistics|status|next actions|draft:|homework|"
    r"snapshot|^products$|customers|funding|leadership|culture|competit|"
    r"words to know|process ahead|interview process|round \d|flying|risks|"
    r"honest read|requirements|role summary|^the role|what to expect|"
    r"to confirm|todo|who (?:he|she|they) (?:is|are)|^after$|still open|"
    r"company facts|the product|business model|news|why (?!are you|i\b|avail)\w|"
    r"(?:org|craft|autonomy|stability) questions",
    re.I)

# Lines that are coaching, not the user's answer, and lines that must never reach
# an application prompt.
_SKIP_LINE = re.compile(
    r"^\s*(?:\*\*principles:?\*\*|\*approach:?\*|principles:|approach:|"
    r"if they (?:push|ask)|\*\*lead with|\*\*backup|\*\*delivery notes)", re.I)
GUARDED = re.compile(
    r"sponsor|visa|immigration|I-485|I-140|H-1B|green card|\bEAD\b|authoriz|"
    r"salary|compensation|\bcomp\b|\bpay\b|\bpaid\b|\boffers?\b|"
    r"\$\s?\d|\b\d{2,3}k\b|equity", re.I)
# Coaching written to the user, not words the user says: "Do not volunteer...",
# "Most candidates will not have this. Lead with it."
_COACHING = re.compile(
    r"^\s*(?:[-*]\s*)?(?:do not|don't|never|lead with|land |pivot|say it|"
    r"keep it|most candidates|use this|pick |save #|lock it|if asked|"
    r"remember|mention |avoid|be ready)", re.I)

_STOP = set("""a an and are as at be but by for from has have i i'm in into
is it its it's me my of on or so that the their them they this to was we
were what when which who will with you your our us can do does did not no
yes would could should about more most also just than then there these
those how why tell us""".split())


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def note_names(vault_dir):
    """(relative path) for every interview note in the vault."""
    names = [f"{FOLDER}/{n}" for n in vaultio.listdir(vault_dir, FOLDER)
             if n.endswith(".md")]
    names += [n for n in vaultio.listdir(vault_dir, "")
              if n.startswith(ROOT_PREFIX) and n.endswith(".md")]
    return names


def sections(name, text):
    """[(title, body)] for the parts of one note that are the user's answers."""
    out = []
    stack = []  # (level, heading)
    body = []
    practice = "practice" in name.lower()

    def flush():
        if not stack:
            return
        path = [h for _, h in stack]
        if any(_DROP.search(h) for h in path):
            return
        if not (practice or any(_KEEP.search(h) for h in path)):
            return
        lines, label = [], ""
        for ln in body:
            # A bold label opens a small block ("**Why Bloomberg**", "**Comp**")
            # that is dropped whole when the label is one of the dropped kinds.
            lm = re.match(r"^\s*\*\*([^*]+)\*\*", ln)
            if lm:
                label = lm.group(1)
            if _DROP.search(label):
                continue
            # A quoted line is what the user says; anything after the closing
            # quote is advice to the user ("Lock it fast, ...") and is cut.
            qm = re.match(r'^(\s*"[^"]+")\s+\S', ln)
            if qm:
                ln = qm.group(1)
            if ln.strip() and not _SKIP_LINE.match(ln) \
                    and not GUARDED.search(ln) and not _COACHING.match(ln) \
                    and ln.strip() != "---":
                lines.append(ln)
        clean = "\n".join(re.sub(r"^\s*>\s?", "", ln) for ln in lines).strip()
        if len(clean.split()) >= 25:
            out.append((" / ".join(path), clean))

    for line in text.split("\n"):
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush()
            body = []
            level = len(m.group(1))
            if level == 1:
                stack = []
                continue
            stack = [x for x in stack if x[0] < level] + [(level, m.group(2).strip())]
            continue
        body.append(line)
    flush()
    return out


def load(vault_dir):
    """[{note, title, text}] across every interview note. [] when the vault
    cannot be read; drafts work without these."""
    out = []
    try:
        names = note_names(vault_dir)
    except OSError:
        return []
    for rel in names:
        try:
            text = vaultio.read(vault_dir, rel)
        except OSError:
            continue
        note = rel.split("/")[-1][:-3]
        for title, body in sections(note, text):
            out.append({"note": note, "title": title, "text": body})
    return out


def pick(question, posting, vault_dir, limit=MAX_CHARS):
    """The interview answers that fit this question, as one text block for
    the prompt and the source check. "" when none fit."""
    chunks = load(vault_dir)
    if not chunks:
        return ""
    want = {w for w in _words(question) if w not in _STOP}
    near = {w for w in _words((posting or "")[:3000]) if w not in _STOP}

    def score(c):
        head = set(_words(c["title"]))
        words = set(_words(c["text"]))
        return 4 * len(head & want) + 2 * len(words & want) + len(words & near) / 6

    ranked = sorted(chunks, key=score, reverse=True)
    out, used = [], 0
    for c in ranked:
        if score(c) <= 0:
            break
        piece = f"## {c['note']}: {c['title']}\n{c['text']}\n"
        if used + len(piece) > limit:
            continue
        out.append(piece)
        used += len(piece)
    return "\n".join(out)
