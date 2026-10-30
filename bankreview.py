"""A review list of whose words each stored answer is.

Every best answer and sent version in the answer note gets one line: a
guessed source tag in brackets, a short id, where it was sent, why the tag
was guessed, and the text itself. The user changes the word in brackets where
the guess is wrong (mine, jobright, verbatim-draft or unknown) and the
list is applied back to the note. Nothing is deleted either way; a text
that is not the user's stays in the note and only stops feeding drafts.

    python3 bankreview.py build [out.md]   write the list (stdout if no path)
    python3 bankreview.py apply list.md    put the tags in the list into the note
"""

import re
import sys
from datetime import date

import answers

HEAD = """# Answer sources

One line per stored answer. The word in brackets is a guess at whose words
it is. Change it where it is wrong:

- mine: you wrote it (typos and all count)
- jobright: Jobright or another tool wrote it
- jobright-edited: Jobright drafted it and you edited it before sending
- verbatim-draft: a Verbatim draft you sent without changing much
- unknown: not sure

Only "mine" (and Verbatim drafts you edited) feed new drafts.
"jobright-edited" lends only its layout, never its wording or facts. Nothing is
deleted. Lines marked (why: ...) say what the guess was based on.

"""

_LINE = re.compile(r"^- \[([a-z-]+(?:, edited \d+%)?)\]\s+([0-9a-f]{6})\b", re.M)


def guess(text, tag=""):
    """(tag, why) for one stored text that has no tag the user set."""
    if tag and answers.parse_source(tag)[0] != "unknown":
        return tag, "already tagged"
    if answers.looks_generated(text):
        found = answers._TOOL_TELLS.search(text).group(0)
        shown = {chr(0x2014): "an em dash", chr(0x2013): "an en dash",
                 chr(0x2011): "a non-breaking hyphen"}.get(found, f'"{found}"')
        return "jobright", f"has {shown}, which you never type"
    return "unknown", "no tool marks, but nothing says whose it is"


def _one_line(text):
    return " / ".join(p.strip() for p in (text or "").split("\n") if p.strip())


def rows(note_text, guesses=None):
    """[(key, tag, where, why, text)] for the note, best answers first in
    each group. guesses maps a key to (tag, why) to use instead of guess()."""
    guesses = guesses or {}
    out = []
    for g in answers.parse_repo(note_text):
        if (g.get("answer") or "").strip():
            k = answers.entry_key(g)
            tag, why = guesses.get(k) or guess(g["answer"], g.get("best_source"))
            out.append((k, tag, f"{g['question']} · best answer", why,
                        g["answer"]))
        for v in answers.sent_versions(g):
            k = answers.entry_key(g, v)
            tag, why = guesses.get(k) or guess(v["text"], v.get("source"))
            where = " · ".join(x for x in (g["question"], v.get("date"),
                                           v.get("company")) if x)
            out.append((k, tag, where, why, v["text"]))
    return out


def build(note_text, guesses=None):
    lines = [HEAD.replace("# Answer sources",
                          f"# Answer sources ({date.today().isoformat()})")]
    group = None
    for k, tag, where, why, text in rows(note_text, guesses):
        g = where.split(" · ")[0]
        if g != group:
            lines.append(f"\n## {g}\n")
            group = g
        lines.append(f"- [{tag}] {k} · {where} (why: {why})\n"
                     f"  \"{_one_line(text)}\"")
    return "\n".join(lines) + "\n"


def parse(review_text):
    """{key: tag} from a review list the user edited."""
    out = {}
    for m in _LINE.finditer(review_text or ""):
        base, edited = answers.parse_source(m.group(1))
        out[m.group(2)] = base if edited is None else f"{base}, edited {edited}%"
    return out


def main(argv):
    if len(argv) >= 2 and argv[1] == "build":
        text = build(answers.read_repo())
        if len(argv) > 2:
            with open(argv[2], "w", encoding="utf-8") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
        return 0
    if len(argv) == 3 and argv[1] == "apply":
        with open(argv[2], encoding="utf-8") as f:
            tags = parse(f.read())
        n = answers.set_sources(tags)
        print(f"{len(tags)} tags read, {n if isinstance(n, int) else 0} "
              f"groups changed")
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
