"""Shared bits: paths, employer name normalizing, and the title test.

Company names on job boards and in your own lists never match as written
("Airbnb, Inc." vs "Airbnb"), so everything that compares companies goes
through normalize() first.

Which titles count is set in jobs/config.json (copy
jobs/config.example.json): "roles" are title words to keep and "exclude"
are title words to skip.
"""

import json
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
JOBS_DIR = os.path.dirname(os.path.abspath(__file__))

BOARDS_PATH = os.path.join(DATA_DIR, "boards.json")
BOARDS_EXTRA_PATH = os.path.join(DATA_DIR, "boards_extra.json")
DB_PATH = os.path.join(DATA_DIR, "jobs.sqlite")
CONFIG_PATH = os.path.join(JOBS_DIR, "config.json")

UA = "Verbatim job watcher (personal use)"


_CONFIG = {}


def load_config():
    """jobs/config.json as a dict, or {} when it is missing or unreadable.
    Read again only when the file changes, since filters ask per job."""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return {}
    hit = _CONFIG.get(CONFIG_PATH)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    _CONFIG[CONFIG_PATH] = (mtime, data)
    return data

# Legal endings that say nothing about who the company is. Stripped only
# from the end, repeatedly, so "Foo Holdings Inc" becomes "foo".
_SUFFIXES = {
    "inc", "incorporated", "llc", "l l c", "llp", "lp", "ltd", "limited",
    "corp", "corporation", "co", "company", "plc", "pc", "pllc", "gmbh",
    "na", "sa", "ag", "bv", "pbc", "holdings", "holding", "com",
}


def normalize(name):
    """Lowercase, no punctuation, no legal suffix, first name of a d/b/a.

    "Meta Platforms, Inc." -> "meta platforms"
    "Block, Inc. d/b/a Square" -> "block"
    "The Walt Disney Company" -> "walt disney"
    """
    if not name:
        return ""
    s = str(name).lower().strip()
    # d/b/a and friends: the legal name comes first and is what DOL keys on.
    s = re.split(r"\b(?:d\s*/\s*b\s*/\s*a|dba|d\.b\.a\.?|a/k/a|aka|f/k/a|fka)\b", s)[0]
    s = s.replace("&", " and ")
    s = re.sub(r"\.(com|net|io|ai|co)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    # Glue runs of single letters back together: "l l c" -> "llc", "n a" -> "na".
    s = re.sub(r"\b([a-z])(?: ([a-z])\b)+", lambda m: m.group(0).replace(" ", ""), s)
    words = s.split()
    if words and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words = words[:-1]
    return " ".join(words)


def compact(name):
    """normalize() with spaces removed, for slug guesses and loose equality."""
    return normalize(name).replace(" ", "")


# ── Title test. One place, used by every board, so "what counts as a role
#    you want" cannot drift between them.
#
# The patterns below are an EXAMPLE, used only when jobs/config.json has no
# "roles": they match product and UX design titles. Set "roles" and
# "exclude" in config.json to search for your own kind of job.

_DESIGN = re.compile(
    r"product design|\bux\b|\bu\.x\.|user experience|\bui\b|user interface|"
    r"interaction design|experience design|design system|"
    r"design (lead|manager|director)|(head|director|vp|manager) of design|"
    r"(staff|principal|senior|sr\.?|lead|founding) designer|"
    r"visual designer|web designer|digital designer|interface designer|"
    r"service designer|content designer|ux writer|design technologist|"
    r"\bdesigner\b",
    re.I)

# Never a design job, whatever else the title says (part of the example).
_NEVER = re.compile(
    r"\bintern\b|internship|co-op|graphic|motion designer|brand designer|"
    r"marketing designer|communications? designer|video|interior|fashion|"
    r"apparel|footwear|textile|jewelry|hardware|mechanical|electrical|"
    r"instructional|learning design|curriculum|game designer|level designer|"
    r"landscape|urban|architect(?!ure)|lighting|packaging|\bprint\b|"
    r"civil|structural|circuit|asic|chip|silicon|pcb|rf |analog|"
    r"kitchen|floral|merchandis|set designer|costume|"
    r"brand design|creative design|creative services|production designer|"
    r"copywriter|ux writer|data center|data hall|recruit|"
    r"brand marketing|brand advertising|marketing design|\bmotion\b|"
    r"handbag|accessories|presentation designer|investment designer|"
    r"systems? designer|policy design|network planning|technology design|"
    r"manufacturing|learning experience|business designer|software design|"
    r"compensation|linguist|airframe|fuselage|\bcad\b|tooling|wire harness|die casting|"
    r"mechanical design|structural design|packaging engineer|\bnvm\b|\brtl\b|physical design|plant design|\bfpga\b|\bvlsi\b",
    re.I)

# Engineering and research titles that mention design. Dropped unless the
# title also clearly says product design.
_NOT_DESIGN = re.compile(
    r"engineer|\beng\b|developer|researcher|research scientist|data scientist|"
    r"sales|account executive|recruit|product manager|program manager",
    re.I)

# Kept even though they contain an excluded word: these are product design.
_KEEP_ANYWAY = re.compile(
    r"product designer|product design (lead|manager|director)|"
    r"design engineer.*product design",
    re.I)


# Industrial and CMF (color, material, finish) design count only when the
# title also says product, UX or experience.
_PHYSICAL = re.compile(r"industrial|\bcmf\b|color,? material", re.I)
_PHYSICAL_OK = re.compile(r"product|\bux\b|experience", re.I)


def _phrase(words):
    """One regex for a list of title words, each matched as a whole phrase."""
    words = [w.strip() for w in words or [] if isinstance(w, str) and w.strip()]
    if not words:
        return None
    return re.compile("|".join(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(w.lower())
                               for w in words), re.I)


_MATCHER = {}


def title_matcher(cfg=None):
    """(roles regex, exclude regex) from config, or (None, ...) for the
    example design patterns. Cached per config contents."""
    cfg = load_config() if cfg is None else cfg
    key = json.dumps([cfg.get("roles") or [], cfg.get("exclude") or []], sort_keys=True)
    if key not in _MATCHER:
        _MATCHER[key] = (_phrase(cfg.get("roles")), _phrase(cfg.get("exclude")))
    return _MATCHER[key]


def is_wanted_title(title, cfg=None):
    """True when a posting's title is one the user is looking for.

    With "roles" set in config.json, a title must contain one of them and
    none of "exclude". Without roles, the example design test below runs
    (and "exclude" still applies)."""
    roles, exclude = title_matcher(cfg)
    t = title or ""
    if exclude is not None and exclude.search(t):
        return False
    if roles is not None:
        return bool(roles.search(t))
    return is_design_title(t)


def is_design_title(title):
    """The example title test: product and UX design roles."""
    t = title or ""
    if not _DESIGN.search(t) or _NEVER.search(t):
        return False
    if _PHYSICAL.search(t) and not _PHYSICAL_OK.search(t):
        return False
    if _NOT_DESIGN.search(t) and not _KEEP_ANYWAY.search(t):
        return False
    return True


# Optional defense filter, off by default. Turn it on with
# "skip_defense": true in jobs/config.json. A job is out when the company is
# a defense company, or the title or posting points at defense, national
# security or a clearance. Checked when listing and alerting, so stored rows
# need no rescan.
_DEFENSE_COMPANIES = {
    "anduril", "anduril industries", "palantir", "palantir technologies",
    "shield ai", "saronic", "saronic technologies", "epirus", "hermeus",
    "castelion", "chaos industries", "true anomaly", "hadrian", "rebellion defense",
    "scale ai federal", "vannevar labs", "applied intuition defense", "govini",
    "lockheed martin", "northrop grumman", "raytheon", "rtx",
    "general dynamics", "bae systems", "l3harris", "leidos", "booz allen hamilton",
    "saic", "caci", "huntington ingalls", "boeing defense", "sierra nevada",
    "kratos", "aerovironment", "red cat", "mach industries", "neros",
    "echodyne", "armada", "armada ai", "nominal", "nominal connect", "onebrief",
    "primer", "primer ai", "primer technologies", "helsing", "code metal",
    "iceye", "senra systems",
}
# Endings that do not change which company it is, so "Vannevar" matches
# "vannevar labs" and "Primer Technologies, Inc." matches "primer".
_COMPANY_TAIL = {"labs", "lab", "inc", "llc", "corp", "corporation", "co", "company",
                 "technologies", "technology", "tech", "ai", "industries", "systems",
                 "group", "holdings"}


def _company_core(name):
    words = normalize(name).split()
    while len(words) > 1 and words[-1] in _COMPANY_TAIL:
        words.pop()
    return "".join(words)


_DEFENSE_CORES = {_company_core(c) for c in _DEFENSE_COMPANIES} | {compact(c) for c in _DEFENSE_COMPANIES}
# Companies with defense among other markets whose roles are read more
# strictly: any role whose title or posting mentions
# defense, government, classified work, Starshield, national security or a
# clearance is hidden. SpaceX puts "To conform to U.S. Government export
# regulations" on every posting, so that one sentence is set aside first,
# and ITAR alone does not count for them.
_STRICT_COMPANIES = {"spacex", "spacexsi", "xai", "x ai"}
_STRICT_TEXT = re.compile(
    r"defen[cs]e|government|classified|starshield|national security(?! authorit)|clearance", re.I)
_EXPORT_LINE = re.compile(r"to conform to u\.?s\.? government export regulations[^.]*\.?", re.I)
_DEFENSE_TITLE = re.compile(
    r"\bdefen[cs]e\b|national security(?! authorit)|\bdod\b|\bmilitary\b|warfighter|"
    r"\b(army|navy|air force|space force|marine corps)\b",
    re.I)
# Posting text only counts on signals about the role itself. Plain mentions of
# "military" are almost always the equal opportunity line, and a company that
# lists defense as one of many markets is not a defense role.
_DEFENSE_TEXT = re.compile(
    r"national security(?! authorit)|\bdod\b|department of (defense|war)|warfighter|"
    r"intelligence community|\bitar\b|security clearance|"
    r"(secret|ts/sci|top secret) clearance|clearance required|"
    r"(active|eligible for|obtain|maintain) (a |an )?(us |u\.s\. )?(government )?clearance",
    re.I)


def skip_defense(cfg=None):
    """True when the user turned the defense filter on in config.json."""
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("skip_defense"))


def is_defense(company, title, text="", about="", cfg=None):
    """True for a defense role, only when the filter is on in config.json.

    about (the company's one-line blurb) is accepted but not used: a company
    with defense among other markets stays, and only its defense roles are
    hidden."""
    if not skip_defense(cfg):
        return False
    return looks_defense(company, title, text)


def looks_defense(company, title, text=""):
    """The defense test itself, whatever the setting."""
    if _company_core(company) in _DEFENSE_CORES or compact(company) in _DEFENSE_CORES:
        return True
    if normalize(company) in _STRICT_COMPANIES or compact(company) in _STRICT_COMPANIES:
        return bool(_STRICT_TEXT.search(title or "")
                    or _STRICT_TEXT.search(_EXPORT_LINE.sub(" ", text or "")))
    if _DEFENSE_TITLE.search(title or ""):
        return True
    return bool(_DEFENSE_TEXT.search(text or ""))


# Full-time roles only by default: contract, temporary, freelance and
# fixed-term jobs are hidden ("include_contract": true in config.json keeps
# them). The title counts first, then the
# board's own employment type when it has one, else only plain statements in
# the posting ("this is a 6-month contract"), since benefits text mentions
# contracts in passing. Checked when listing and alerting, like defense.
_CONTRACT_TYPE = re.compile(r"contract|temporary|\btemp\b|freelance|fixed[- ]?term|seasonal", re.I)
# "Contract" in a title is only the job type when it stands alone: "(Contract)",
# "Designer, Contract", "Contract Designer". "Contract Orchestration" is a
# product area.
_CONTRACT_TITLE = re.compile(
    r"\bcontractors?\b|^\W*contract\b|[(\[]\s*contract\b|\bcontract\s*[)\]]|"
    r"\bcontract\W*$|\bcontract (?:role|position|designer)|"
    r"\btemporary\b|\btemp\b|freelance|fixed[- ]term|"
    r"temp[- ]to[- ](?:hire|perm)|contract[- ]to[- ]hire|\bc2h\b|\b1099\b", re.I)
_CONTRACT_TEXT = re.compile(
    r"this (?:is|will be) an? (?:\d+[- ](?:month|week)[- ])?(?:temporary|contract|freelance|fixed[- ]term)\b|"
    r"\b\d+[- ](?:month|week)s? (?:contract|fixed[- ]term)\b|"
    r"\b(?:contract|temporary|freelance|fixed[- ]term) (?:role|position|assignment|engagement|opportunity)\b|"
    r"temp[- ]to[- ]hire|contract[- ]to[- ]hire|"
    r"employment type:?\s*(?:contract|temporary|freelance)", re.I)


def is_contract(title, text="", emptype="", cfg=None):
    cfg = load_config() if cfg is None else cfg
    if cfg.get("include_contract"):
        return False
    if _CONTRACT_TITLE.search(title or ""):
        return True
    if emptype:
        # "Full-time / Contract" offers full-time, so it stays.
        return bool(_CONTRACT_TYPE.search(emptype)) and not re.search(r"full[- ]?time|permanent", emptype, re.I)
    t = re.sub(r"\b(?:not|never|isn't|is not) an? (?:\w+ ){0,2}contract\w*", "", text or "", flags=re.I)
    return bool(_CONTRACT_TEXT.search(t))


def seniority(title):
    t = (title or "").lower()
    for word, label in (("principal", "principal"), ("staff", "staff"),
                        ("director", "director"), ("head of", "head"),
                        ("vp", "vp"), ("manager", "manager"),
                        ("lead", "lead"), ("senior", "senior"), ("sr", "senior"),
                        ("junior", "junior"), ("associate", "junior"),
                        ("entry", "junior")):
        if re.search(r"\b%s\b" % re.escape(word), t):
            return label
    m = re.search(r"\b(i{1,3}|iv|v)\b\s*$", t)
    if m:
        return {"i": "junior", "ii": "mid", "iii": "senior",
                "iv": "staff", "v": "principal"}[m.group(1)]
    return "mid"
