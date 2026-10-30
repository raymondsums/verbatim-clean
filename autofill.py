"""Autofill planning: map form fields to profile facts, learn corrections.

The extension sends field descriptors (labels, names, ids, placeholders,
never values). The plan comes back with what to fill and what to flag.
Three layers, cheapest first:

1. A stored per-domain map of field signature -> canonical key. A form
   filled once needs no model call again.
2. For unknown signatures only, one CLI call maps them onto canonical
   keys, and the result is written back into the map.
3. Corrections: when you change a filled value, the extension reports
   it. The corrected value wins on that domain from then on.

Values are attached server-side from profile.json, verbatim. The model
only ever sees field labels, never your data.

Rules enforced here:
- salary and cover-letter fields are flagged, never filled
- EEO fields fill only from your stored choices in profile.json
- authorization fields fill only the stored verbatim wording; a field the
  wording does not clearly fit gets flagged
- nothing here submits anything; the extension has no submit code at all
"""

import json
import os
import re
import threading

from claude_cli import find_claude
import llm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(BASE_DIR, "autofill_map.json")
CORRECTIONS_PATH = os.path.join(BASE_DIR, "corrections.json")
PROFILE_PATH = os.environ.get("VERBATIM_PROFILE") or os.path.join(BASE_DIR, "profile.json")

CLAUDE_BIN = find_claude()
CLAUDE_MODEL = "sonnet"
MAP_TIMEOUT = 90

_LOCK = threading.RLock()

KEYS = [
    "first_name", "last_name", "full_name", "email", "phone", "location",
    "legal_first_name", "legal_last_name", "legal_full_name", "preferred_name",
    "preferred_first_name", "preferred_last_name", "full_name_long",
    "country", "located_in_us", "time_zone", "over_18", "zip_code",
    "address_full", "street_address", "address_line_1", "address_line_2",
    "city", "state", "referred", "employment_type", "non_compete",
    "languages", "sms_opt_in", "worked_here_before",
    "heard_about", "willing_to_relocate", "pronouns", "start_date",
    "open_to_office", "eeo_orientation", "eeo_transgender", "eeo_lgbtq",
    "eeo_hispanic",
    "linkedin", "portfolio", "github", "years_of_experience",
    "authorized_to_work", "requires_sponsorship", "work_authorization_details",
    "eeo_gender", "eeo_race", "eeo_veteran", "eeo_disability",
    # Repeating sections. The key says what the field asks for; which row
    # of the history it belongs to comes from the page, not the model.
    "school", "degree", "degree_type", "discipline",
    "education_start_year", "education_end_year",
    "employer", "job_title", "employer_location",
    "employment_start_month", "employment_start_year",
    "employment_end_month", "employment_end_year",
    "current_employer", "current_title",
    "salary", "cover_letter", "none",
]

MAP_SYSTEM = """You map job application form fields onto canonical keys.
You get each field's label, name, id, placeholder and select options. Pick
the one canonical key each field asks for, from this list:

first_name, last_name, full_name, legal_first_name, legal_last_name,
legal_full_name (fields that say "legal name"), preferred_name (preferred
name, nickname, the name you go by), email, phone, location, linkedin,
portfolio (a portfolio, personal website or "website" link), github, years_of_experience, authorized_to_work (a yes/no "are
you authorized to work in the US" field), requires_sponsorship (a yes/no
"will you require sponsorship" field), work_authorization_details (a free
text field about work authorization or visa status), eeo_gender, eeo_race,
eeo_veteran, eeo_disability, salary (salary or compensation expectations),
cover_letter, none.

Also: country (country of residence), located_in_us (a yes/no "are you
currently located in / based in the US"), time_zone, over_18 (a yes/no "are
you 18 or older"), zip_code (ZIP or postal code), heard_about ("how did you
hear about this job"), willing_to_relocate (a yes/no "willing to relocate"
or "based in or willing to relocate to <city>"), pronouns, start_date
("when can you start", notice period, earliest start date), open_to_office
(a yes/no "open to working in the office / hybrid / on-site"),
referred (a yes/no "were you referred by an employee"; a box asking for
the referrer's name is none), employment_type (full-time, part-time,
contract), non_compete (a yes/no "bound by a non-compete or agreement"),
languages (languages spoken), sms_opt_in (consent to text messages about
the application), worked_here_before ("have you worked here before", "do
you know anyone who works here", "are you related to an employee"),
address_full (one box for the whole mailing address), street_address (a
street address box with no separate line 2), address_line_1, address_line_2
(apartment, suite, unit), city, state (state or province), eeo_hispanic (a yes/no "are you
Hispanic or Latino"), eeo_orientation
(sexual orientation), eeo_transgender ("do you identify as
transgender"), eeo_lgbtq ("member of the LGBTQ+ community").

Education and work history sections repeat, one block per school or job.
Map what the field asks for and ignore which block it is in; the row is
worked out from the page separately. Those keys are:

school, degree (the full degree as written, "M.S. Human-Computer
Interaction"), degree_type (a dropdown of degree levels: Bachelor's,
Master's), discipline (field of study, major), education_start_year,
education_end_year, employer (a company in the work history), job_title,
employer_location, employment_start_month, employment_start_year,
employment_end_month, employment_end_year.

Two more for fields that ask about the current job rather than a
history block: current_employer and current_title.

Use "none" whenever a field does not clearly ask for one of these; a wrong
mapping fills a real application with wrong data, so when unsure, "none"."""

MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "key": {"type": "string", "enum": KEYS},
                },
                "required": ["index", "key"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mappings"],
    "additionalProperties": False,
}


# Keys whose value always comes from profile.json, never from a per-site
# correction: one exact LinkedIn link and one exact website link on every
# form.
ALWAYS_PROFILE = {"linkedin", "portfolio",
                  # Names too: the user's name rules are set in the profile, and an old
                  # correction on one company's form must not spread to every
                  # form on that ATS.
                  "first_name", "last_name", "full_name", "full_name_long",
                  "legal_first_name", "legal_last_name", "legal_full_name",
                  "preferred_name", "preferred_first_name", "preferred_last_name"}


class AutofillError(Exception):
    pass


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _profile():
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return json.load(f)


def signature(field):
    """A stable identity for a field on a domain, from its own signals."""
    parts = [field.get(k) or "" for k in ("label", "name", "id", "placeholder")]
    sig = "|".join(p.strip().lower() for p in parts)
    return re.sub(r"\s+", " ", sig)[:200] or f"tag:{field.get('tag', '?')}"


# Words a widget shows about itself rather than about what it asks for.
# React comboboxes (Greenhouse, Rippling) hand us these instead of a label.
_WIDGET_WORDS = re.compile(
    r"^(select|search|choose|type here|please select|none|-+|\u2014)\W*$")


def _weak(sig, field):
    """True when a signature cannot safely identify one field.

    A field with no name and no id, labelled only with the widget's own
    chrome, is indistinguishable from every other such field on the page.
    Learning it means one cached key later fills a whole page of unrelated
    dropdowns with the same value, which is the wrong-data failure this
    project exists to avoid.
    """
    if sig.startswith("tag:"):
        return True
    if (field.get("name") or "").strip() or (field.get("id") or "").strip():
        return False
    label = (field.get("label") or "").strip().lower()
    placeholder = (field.get("placeholder") or "").strip().lower()
    return not label or bool(_WIDGET_WORDS.match(label)) or label == placeholder


def _values(profile):
    """Canonical key -> the exact string to fill. Never touched by a model."""
    name = (profile.get("name") or "").strip()
    first, last = (name.split(" ", 1) + [""])[:2] if name else ("", "")
    eeo = profile.get("eeo") or {}
    addr = profile.get("address") or {}
    return {
        "first_name": first,
        "last_name": last,
        "full_name": name,
        # The user's legal name differs from the name the user goes by. Legal boxes get
        # the legal one; everything else gets the name the user uses.
        "legal_first_name": profile.get("legal_first_name") or "",
        "legal_last_name": profile.get("legal_last_name") or "",
        "legal_full_name": profile.get("legal_name") or "",
        "preferred_name": profile.get("preferred_name") or "",
        "preferred_first_name": profile.get("preferred_first_name") or first,
        "preferred_last_name": profile.get("preferred_last_name") or last,
        # A box that literally says "full name" gets both names; a plain
        # "Name" box keeps the short one.
        "full_name_long": profile.get("full_name_on_forms") or name,
        "email": profile.get("email") or "",
        "phone": profile.get("phone") or "",
        "location": profile.get("location") or "",
        "linkedin": profile.get("linkedin") or "",
        "portfolio": profile.get("portfolio") or "",
        "github": profile.get("github") or "",
        "years_of_experience": profile.get("years_of_experience") or "",
        "authorized_to_work": profile.get("authorized_to_work_in_us") or "",
        "requires_sponsorship": profile.get("requires_sponsorship") or "",
        "work_authorization_details": profile.get("work_authorization") or "",
        "eeo_gender": eeo.get("gender") or "",
        "eeo_race": eeo.get("race_ethnicity") or "",
        "eeo_veteran": eeo.get("veteran_status") or "",
        "eeo_disability": eeo.get("disability_status") or "",
        "eeo_orientation": eeo.get("sexual_orientation") or "",
        "eeo_hispanic": eeo.get("hispanic_latino") or "",
        "eeo_transgender": eeo.get("transgender") or "",
        "eeo_lgbtq": eeo.get("lgbtq") or "",
        "country": profile.get("country") or "",
        "located_in_us": profile.get("located_in_us") or "",
        "time_zone": profile.get("time_zone") or "",
        "over_18": profile.get("over_18") or "",
        "zip_code": profile.get("zip_code") or addr.get("zip") or "",
        "address_full": ", ".join(x for x in (
            " ".join(x for x in (addr.get("line1"), addr.get("line2")) if x),
            addr.get("city"), " ".join(x for x in (addr.get("state"), addr.get("zip")) if x)) if x),
        "street_address": " ".join(x for x in (addr.get("line1"), addr.get("line2")) if x),
        "address_line_1": addr.get("line1") or "",
        "address_line_2": addr.get("line2") or "",
        "city": addr.get("city") or "",
        "referred": profile.get("referred") or "",
        "employment_type": profile.get("employment_type") or "",
        "non_compete": profile.get("non_compete") or "",
        "languages": profile.get("languages") or "",
        "sms_opt_in": profile.get("sms_opt_in") or "",
        # The user's call per company: always flagged, never filled.
        "worked_here_before": "",
        "state": addr.get("state") or "",
        "heard_about": profile.get("heard_about") or "",
        "willing_to_relocate": profile.get("willing_to_relocate") or "",
        "pronouns": profile.get("pronouns") or "",
        "start_date": profile.get("start_date") or "",
        "open_to_office": profile.get("open_to_office") or "",
    }


def _split_date(raw):
    """'Jun 2018' -> ('Jun', '2018'); '2016' -> ('', '2016')."""
    parts = (raw or "").split()
    if len(parts) >= 2:
        return parts[0], parts[-1]
    if len(parts) == 1:
        return ("", parts[0]) if parts[0].isdigit() else (parts[0], "")
    return "", ""


def _history_value(key, row, profile):
    """A value from a repeating section: education or employment, by row.

    A form with three education blocks asks the same question three times,
    and the answer differs per block. Rows past the end of the history are
    empty on purpose: an extra blank block is not something to fill.
    """
    edu = profile.get("education") or []
    emp = profile.get("employment_history") or []

    if key in ("current_employer", "current_title"):
        # Between roles: the user wants these left empty, not filled with the
        # last job.
        if not emp or profile.get("current_job_blank"):
            return ""
        return emp[0].get("company" if key == "current_employer" else "title") or ""

    if key in ("school", "degree", "degree_type", "discipline",
               "education_start_year", "education_end_year"):
        if row >= len(edu):
            return ""
        entry = edu[row]
        if key == "education_start_year":
            return _split_date(entry.get("start"))[1]
        if key == "education_end_year":
            return _split_date(entry.get("end"))[1]
        return entry.get(key) or ""

    if row >= len(emp):
        return ""
    entry = emp[row]
    if key == "employer":
        return entry.get("company") or ""
    if key == "job_title":
        return entry.get("title") or ""
    if key == "employer_location":
        return entry.get("location") or ""
    which = "start" if "_start_" in key else "end"
    month, year = _split_date(entry.get(which))
    return month if key.endswith("_month") else year


def _map_unknowns(fields):
    """One CLI call mapping unknown fields to keys. [{index, key}]."""
    lines = ["Fields:"]
    for f in fields:
        desc = [f"index {f['index']}", f"tag {f.get('tag')}"]
        for k in ("label", "name", "id", "placeholder", "type", "widget"):
            if f.get(k):
                desc.append(f"{k}: {f[k]}")
        if f.get("options"):
            desc.append("options: " + " / ".join(f["options"][:12]))
        lines.append("- " + "; ".join(desc))
    try:
        result = llm.run(MAP_SYSTEM, MAP_SCHEMA, "\n".join(lines),
                         timeout=MAP_TIMEOUT, model=CLAUDE_MODEL,
                         claude_bin=CLAUDE_BIN)
        return [m for m in result.get("mappings", [])
                if isinstance(m.get("index"), int) and m.get("key") in KEYS]
    except Exception as e:
        raise AutofillError(f"field mapping failed: {e}")


# Name boxes whose label settles the key on its own, checked before the
# site map and the model: a stored "first_name" for a box that says "Legal
# first name" would put the wrong name on a real application. "Legally
# authorized to work" never matches; these need the word "name".
_NAME_RULES = [
    (re.compile(r"\blegal\b.*\bfirst\b.*\blast\s+name|\blegal\b.*\bfull\s+name", re.I), "legal_full_name"),
    (re.compile(r"\blegal\b.*\b(first|given)\s+name|\b(first|given)\s+name\b.*\blegal\b", re.I), "legal_first_name"),
    (re.compile(r"\blegal\b.*\b(last|family|sur)\s*name|\b(last|family)\s+name\b.*\blegal\b", re.I), "legal_last_name"),
    (re.compile(r"\blegal\s+(full\s+)?name\b|\bfull\s+legal\s+name\b|\bname\b.*\(legal\)|\bname\s+as\s+it\s+appears", re.I), "legal_full_name"),
    (re.compile(r"\bpreferred\s+full\s+name\b", re.I), "full_name"),
    (re.compile(r"\bpreferred\s+(first|given)\s+name\b", re.I), "preferred_first_name"),
    (re.compile(r"\bpreferred\s+(last|family|sur)\s*name\b", re.I), "preferred_last_name"),
    (re.compile(r"\bpreferred\s+(full\s+)?name\b|\bnick\s*name\b|\bname\s+you\s+(go|prefer)|\bgo\s+by\b", re.I), "preferred_name"),
    (re.compile(r"\bfull\s+name\b", re.I), "full_name_long"),
    # Asked on its own, not the race list: "Race (if not Hispanic)" must
    # stay a race question.
    (re.compile(r"^\W*(are\s+you|do\s+you\s+identify\s+as)\b[^?]*\bhispanic|^\W*hispanic\s*(/|or)\s*latin[oaex]\W*$", re.I), "eeo_hispanic"),
    (re.compile(r"\bstate\s+of\s+residence\b|\bstate\s+(do\s+)?you\s+(currently\s+)?(live|reside)\b|\bhome\s+state\b|\bresident\s+state\b", re.I), "state"),
]


# "Do you have more than 6 years of experience?" asks for the number of
# years even though the answer is a yes or no; the extension turns the
# stored number into the answer. Decided here, before the site map, so an
# old "none" mapping for the question cannot hide it.
_YEARS_THRESHOLD = re.compile(
    r"(more than|over|at least|minimum of|\d+\s*\+|\d+\s*(or more|and above|plus))"
    r"[^?]{0,40}\byears?\b[^?]*\bexperience\b", re.I)


_GENDER_IDENTITY = re.compile(r"\bgender\s+identity\b|\bidentify\s+(my|your)\s+gender\b|^\W*(what\s+is\s+your\s+)?gender\W*$", re.I)
_COUNTRY = re.compile(r"^\W*country(\s+of\s+residence)?\W*$", re.I)


def rule_key(field):
    """The key a box's own label decides, or None: names, then a
    threshold question about years of experience."""
    key = name_rule(field)
    if key:
        return key
    if _YEARS_THRESHOLD.search(field.get("label") or ""):
        return "years_of_experience"
    # "Gender Identity" is the same question as "Gender"; the model has
    # left it unmapped on forms that ask both.
    if _GENDER_IDENTITY.search(field.get("label") or ""):
        return "eeo_gender"
    # A box labelled just "Country" is the country, whatever an old site
    # map says (one Greenhouse map had it as the city).
    if _COUNTRY.match(field.get("label") or ""):
        return "country"
    return None


def name_rule(field):
    """The name key a box's own label decides, or None."""
    label = (field.get("label") or "").strip()
    # The box's own name comes before any note in brackets: "Preferred Full
    # Name (put your legal name in the boxes above)" asks for the preferred
    # name, not the legal one.
    head = re.split(r"[(.]", label, maxsplit=1)[0]
    for text in (head, label):
        for rx, key in _NAME_RULES:
            if rx.search(text):
                return key
    return None


def plan(domain, fields):
    """The fill plan for a page.

    {"plan": [{index, key, action, value?, reason?}],
     "unidentified": how many fields had no usable identity,
     "mapping_error": why the model call failed, when it did}
    """
    domain = (domain or "").strip().lower()
    if not domain or not isinstance(fields, list) or not fields:
        raise AutofillError("domain and fields required")

    profile = _profile()
    values = _values(profile)

    with _LOCK:
        site_map = _read_json(MAP_PATH, {}).get(domain, {})
    corrections = _read_json(CORRECTIONS_PATH, {}).get(domain, {})

    # A signature that two fields on one page share identifies neither of
    # them, so it is used for neither: not looked up, not learned. Rows are
    # the exception: an education section repeats the same question once
    # per school, and the row says which school, so "School" in row 0 and
    # "School" in row 1 are two answerable fields, not a collision. The
    # map still learns one entry for both, because the question is one
    # question; only the value differs.
    seen = {}
    for f in fields:
        f["_sig"] = signature(f)
        ident = (f["_sig"], int(f.get("row") or 0))
        seen[ident] = seen.get(ident, 0) + 1

    keyed = {}
    unknown = []
    skipped = 0
    for f in fields:
        sig = f["_sig"]
        if seen[(sig, int(f.get("row") or 0))] > 1 or _weak(sig, f):
            f["_skip"] = True
            skipped += 1
        elif rule_key(f):
            keyed[sig] = rule_key(f)
        elif sig in site_map:
            keyed[sig] = site_map[sig]
        elif not any(u["_sig"] == sig for u in unknown):
            unknown.append(f)  # ask about a repeated question once

    mapping_error = None
    if unknown:
        learned = {}
        by_index = {f["index"]: f for f in unknown}
        try:
            mappings = _map_unknowns(unknown)
        except AutofillError as e:
            # A failed mapping call must cost only the fields it was asked
            # about. Everything already in the map still fills, and nothing
            # is cached, so the next attempt asks again.
            mapping_error = str(e)
            mappings = []
        for m in mappings:
            f = by_index.get(m["index"])
            if f:
                learned[f["_sig"]] = m["key"]
        for f in unknown:
            keyed.setdefault(f["_sig"], learned.get(f["_sig"], "none"))
        if learned:
            with _LOCK:
                all_maps = _read_json(MAP_PATH, {})
                all_maps.setdefault(domain, {}).update(learned)
                _write_json_atomic(MAP_PATH, all_maps)

    out = []
    unmapped = 0
    for f in fields:
        if f.get("_skip"):
            continue
        sig = f["_sig"]
        key = keyed.get(sig, "none")
        if key == "none":
            unmapped += 1
            continue
        if key == "salary":
            out.append({"index": f["index"], "key": key, "action": "flag",
                        "reason": "Salary is your call each time."})
        elif key == "cover_letter":
            out.append({"index": f["index"], "key": key, "action": "flag",
                        "reason": "Paste the tailored cover letter from the "
                                  "toolkit."})
        else:
            # A correction only stands for the key it was made under. If
            # the field is mapped to a different key now, an old full name
            # correction must not land in what is now a first name field.
            fix = corrections.get(sig, {})
            if fix.get("key") and fix.get("key") != key:
                fix = {}
            if key in ALWAYS_PROFILE:
                fix = {}  # the user set one exact value for these everywhere
            value = fix.get("corrected") or values.get(key)
            if not value and key not in values:
                # Only history keys fall through to the history: an empty
                # profile value (no ZIP yet) must stay empty, not become a
                # year from the work history.
                value = _history_value(key, int(f.get("row") or 0), profile)
            if not value and key in ("current_employer", "current_title") \
                    and profile.get("current_job_blank"):
                unmapped += 1
                continue  # left empty on purpose
            if value:
                out.append({"index": f["index"], "key": key,
                            "action": "fill", "value": value})
            else:
                out.append({"index": f["index"], "key": key, "action": "flag",
                            "reason": "Your call for each company."
                            if key == "worked_here_before"
                            else "No stored value for this."})
    return {"plan": out, "unidentified": skipped, "unmapped": unmapped,
            "mapping_error": mapping_error}


def record_corrections(domain, items):
    """Store (signature, key, filled, corrected) tuples for a domain."""
    domain = (domain or "").strip().lower()
    if not domain or not isinstance(items, list):
        raise AutofillError("domain and corrections required")
    stored = 0
    with _LOCK:
        data = _read_json(CORRECTIONS_PATH, {})
        site = data.setdefault(domain, {})
        for c in items:
            sig = (c.get("signature") or "").strip()
            corrected = (c.get("corrected") or "").strip()
            if not sig or not corrected:
                continue
            site[sig] = {"key": c.get("key"), "filled": c.get("filled"),
                         "corrected": corrected}
            stored += 1
        _write_json_atomic(CORRECTIONS_PATH, data)
    return stored


def learned():
    """Everything the autofill has learned, for inspection and pruning."""
    return {"map": _read_json(MAP_PATH, {}),
            "corrections": _read_json(CORRECTIONS_PATH, {})}
