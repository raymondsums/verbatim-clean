"""Claims the user never wants made about them, set in profile.json.

Two optional keys:

  "never_claim": ["design system"]
      Things the user worked on but did not build or lead. A draft,
      resume or letter that says the user defined, built, created, led,
      owned or set one up is refused. Saying they maintained, used or
      contributed to one is fine.

  "leave_out_skills": ["Design Systems"]
      Skills a tailored resume never lists, even when the user's own
      resume does.

Drafts are also never allowed to name Verbatim itself or describe a "job
application tool", since an employer reading that learns the answer was
filled by software.
"""

import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.environ.get("VERBATIM_PROFILE") or os.path.join(BASE_DIR, "profile.json")

_CACHE = {"mtime": None, "profile": {}}


def _profile():
    try:
        mtime = os.path.getmtime(PROFILE_PATH)
    except OSError:
        return {}
    if _CACHE["mtime"] != mtime:
        try:
            with open(PROFILE_PATH, encoding="utf-8") as f:
                _CACHE["profile"] = json.load(f) or {}
        except (OSError, ValueError):
            _CACHE["profile"] = {}
        _CACHE["mtime"] = mtime
    return _CACHE["profile"]


def never_claim():
    """The nouns from profile.json's never_claim, lowercased."""
    items = _profile().get("never_claim") or []
    return [" ".join(str(x).split()).lower() for x in items if str(x).strip()]


def leave_out_skills():
    items = _profile().get("leave_out_skills") or []
    return [" ".join(str(x).split()).lower() for x in items if str(x).strip()]


_TOOL = r"(?-i:\bVerbatim\b)|job[- ]application (tool|extension)"


def claim_pattern(nouns=None, tool=True):
    """One regex that matches a claim to have built or led any of nouns.

    Fails safe: any claim verb up to 50 characters before the noun blocks,
    with three narrow ways out. "lead" before a role ("the lead
    engineer"), "own" after a possessive ("my own"), and "lead" in an
    earlier clause ("Leading the audit, I maintained").
    """
    nouns = never_claim() if nouns is None else nouns
    parts = [_TOOL] if tool else []
    for noun in nouns:
        n = r"\b" + r"\s+".join(re.escape(w) for w in noun.split()) + r"s?\b"
        parts += [
            r"\b(defin|buil|creat|led\b|architect|establish|start|set\s?(ting\s)?up)\w*\b[^.]{0,50}" + n,
            r"\blead(?!\s+(engineer|developer|designer|researcher|pm|product manager|architect)s?\b)\w*\b[^.,;]{0,50}" + n,
            r"(?<!\bmy\s)(?<!\bour\s)(?<!\btheir\s)\bown\w*\b[^.]{0,50}" + n,
            r"\bdesign(ed|ing)?\s+((the|a|an|our|my|its|their|new|whole|entire)\s+){1,4}(\w+\s+)?" + n,
        ]
    if not parts:
        return None
    return re.compile("|".join(parts), re.I)


def problems(text, nouns=None):
    """The claim phrases found in text, for the pre-tailoring check."""
    nouns = never_claim() if nouns is None else nouns
    if not nouns:
        return []
    return [m.group(0) for m in claim_pattern(nouns, tool=False).finditer(text or "")]


def prompt_rule():
    """A line for model prompts, or "" when nothing is set."""
    nouns = never_claim()
    if not nouns:
        return ""
    listed = ", ".join(f'"{n}"' for n in nouns)
    return ("Never say the user defined, built, created, led, owned or set up "
            f"any of these: {listed}. The user worked on or with them; saying "
            "more is a claim they cannot back up in an interview.")
