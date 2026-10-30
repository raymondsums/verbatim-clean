"""Markdown to PDF via headless Chrome.

Chrome is already on the machine and prints clean PDFs, so there is no
Python PDF dependency. The markdown subset handled here is what the
tailored documents actually use: headings, bold, italic, bullet lists,
and paragraphs.
"""

import html
import os
import re
import subprocess
import tempfile

_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)


def find_chrome():
    """CHROME_PATH in the environment wins, else the first browser found.

    Falls back to the usual Chrome path so the error names something real.
    """
    override = os.environ.get("CHROME_PATH")
    if override:
        return os.path.expanduser(override)
    for p in _CHROME_PATHS:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return _CHROME_PATHS[0]


CHROME = find_chrome()

# Same typographic system as the resume (Baskerville name, Avenir body,
# terracotta accents), used for the cover letter and any plain document.
PAGE_CSS = """
:root {
  --ink:    #0f0e0d;
  --mid:    #77695b;
  --rule:   #d8d2c8;
  --accent: #bd4228;
}
@page { size: letter; margin: 0.75in 0.9in; }
body {
  font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  font-weight: 400; font-size: 11.5pt; line-height: 1.7;
  color: var(--ink); margin: 0;
  /* pin the footer to the bottom of the page box (11in less 0.75in margins) */
  display: flex; flex-direction: column; min-height: 9.5in;
}
h1 {
  font-family: 'DM Serif Display', Georgia, serif; font-weight: 400;
  font-size: 25pt; line-height: 1.05; letter-spacing: -0.005em; margin: 0;
}
h1 + p {
  margin: 5px 0 38px; font-size: 9.5pt; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.11em; color: var(--accent);
  padding-bottom: 14px; border-bottom: 1px solid var(--rule);
}
body > p:last-child {
  margin: auto 0 0; padding-top: 12px; font-size: 9.5pt;
  color: var(--mid); border-top: 1px solid var(--rule);
}
h2 {
  margin: 22px 0 8px; font-size: 8.5pt; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.14em; color: var(--accent);
}
p  { margin: 0 0 14pt; }
strong { font-weight: 600; }
ul { margin: 6pt 0 10pt; padding-left: 15px; }
li { margin: 0 0 4pt; }
li::marker { color: var(--accent); }
"""


# Typography taken from the embedded fonts and fill colors of your own
# Resume - Full.pdf: Baskerville name, Avenir Book body at ~11pt, Avenir
# Next DemiBold for bold runs and headers, terracotta #B85338. Everything
# scales off the body size; render_resume_pdf tries the largest first and
# steps down only if the content overflows one page.
# Single-column layout: the two-column balancing
# act was more trouble than it was worth, and single column parses better
# in ATS resume parsers. Same type system extracted from a real resume.
RESUME_SCALES = (11, 10.4, 9.8, 9.2)

RESUME_CSS = """
:root {{
  --ink:    #0f0e0d;
  --mid:    #77695b;
  --rule:   #d8d2c8;
  --accent: #bd4228;
}}
@page {{ size: letter; margin: 0.5in; }}
body {{
  font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  font-weight: 400; font-size: {base}pt; line-height: 1.42;
  color: var(--ink); margin: 0;
}}
h1 {{
  font-family: 'DM Serif Display', Georgia, serif; font-weight: 400;
  font-size: 2.38em; letter-spacing: -0.005em; line-height: 1.05; margin: 0;
}}
.role {{ margin: 5px 0 0; font-size: 0.91em; font-weight: 600;
         text-transform: uppercase; letter-spacing: 0.11em; color: var(--accent); }}
.contact {{ margin: 8px 0 0; font-size: 0.9em; color: var(--mid); }}
.contact .sep {{ color: #a29a90; }}
header {{ padding-bottom: 12px; border-bottom: 1px solid var(--rule); }}
h2 {{
  margin: 16px 0 8px; font-size: 0.81em; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.14em; color: var(--accent);
}}
.summary {{ margin: 0; }}
.entry {{ margin-bottom: 14px; break-inside: avoid; }}
.entry:last-child {{ margin-bottom: 0; }}
.entry-head {{
  display: flex; justify-content: space-between;
  align-items: baseline; gap: 12px;
}}
.title {{ margin: 0; font-size: 1.05em; font-weight: 600; letter-spacing: -0.005em; }}
.org {{ font-weight: 500; }}
.dates {{
  font-size: 0.86em; color: var(--mid);
  white-space: nowrap; font-variant-numeric: tabular-nums;
}}
.blurb {{ margin: 5px 0 0; }}
ul {{ margin: 7px 0 0; padding-left: 15px; }}
li {{ margin-bottom: 5px; }}
li:last-child {{ margin-bottom: 0; }}
li::marker {{ color: var(--accent); }}
li strong {{ font-weight: 600; }}
.edu-line {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 6px; }}
.edu-line:last-child {{ margin-bottom: 0; }}
.skills p {{ margin: 0; }}
.skills p + p {{ margin-top: 4px; }}
"""


def short_link(url):
    """A link as the resume shows it: no https://, and LinkedIn bare.

    profile.json keeps full URLs because that is what forms want; the
    resume and letter keep the short form they always had.
    """
    url = re.sub(r"^https?://", "", (url or "").strip())
    return re.sub(r"^www\.(linkedin\.com)", r"\1", url, flags=re.I)


def to_plain_text(md):
    """Markdown to plain text for application textareas.

    Form fields are plain textareas: pasting markdown leaves literal ##
    and ** in a submission. Section headings become uppercase lines,
    bullets keep a hyphen, and emphasis markers are dropped.
    """
    out = []
    for line in md.split("\n"):
        raw = line.rstrip()
        stripped = raw.strip()
        h = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if h:
            level, text = len(h.group(1)), h.group(2)
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
            if out and out[-1] != "":
                out.append("")
            out.append(text.upper() if level <= 2 else text)
            out.append("")
            continue
        b = re.match(r"^[-*]\s+(.*)$", stripped)
        if b:
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", b.group(1))
            text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
            out.append("- " + text)
            continue
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", raw)
        text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
        out.append(text)

    # collapse runs of blank lines, drop leading and trailing ones
    cleaned, blank = [], False
    for line in out:
        if not line.strip():
            if blank or not cleaned:
                continue
            blank = True
            cleaned.append("")
        else:
            blank = False
            cleaned.append(line)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned) + "\n"


class PdfError(Exception):
    pass


def _inline(text):
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def unwrap_bullets(md):
    """Join hard-wrapped bullet continuation lines back onto their bullet.

    resume.md wraps long bullets across lines with a two-space indent. Without
    this, each continuation is parsed as a new paragraph and the bullet breaks
    apart mid-sentence.
    """
    out = []
    for line in md.split("\n"):
        cont = (out and line[:1].isspace() and line.strip()
                and not re.match(r"^\s*[-*]\s", line)
                and out[-1].strip()
                and re.match(r"^[-*]\s", out[-1].strip()))
        if cont:
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
    return "\n".join(out)


def md_to_html(md, reflow=False):
    """reflow=True joins wrapped paragraph lines with a space instead of <br>,
    which is what a resume wants; cover letters keep <br> for signature blocks."""
    lines = md.split("\n")
    out, in_list, para = [], False, []

    def flush_para():
        if para:
            # single newlines stay as line breaks (signature blocks, the
            # contact line); blank lines separate paragraphs
            joiner = " " if reflow else "<br>"
            out.append("<p>" + joiner.join(_inline(p) for p in para) + "</p>")
            para.clear()

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if not stripped:
            flush_para()
            close_list()
        elif heading:
            flush_para()
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif bullet:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + _inline(bullet.group(1)) + "</li>")
        else:
            close_list()
            para.append(stripped)
    flush_para()
    close_list()
    return "\n".join(out)


def parse_sections(md):
    """Split markdown into an ordered {section name: content} dict on '## '."""
    sections, name, buf = {}, None, []
    for line in md.split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line.strip())
        if m and not line.strip().startswith("###"):
            if name:
                sections[name] = "\n".join(buf).strip()
            name, buf = m.group(1).strip().lower(), []
        elif name:
            buf.append(line)
    if name:
        sections[name] = "\n".join(buf).strip()
    return sections


def _section_rows(content):
    """Section lines with bullet markers stripped, empties dropped."""
    rows = []
    for line in content.split("\n"):
        line = re.sub(r"^[-*]\s+", "", line.strip())
        if line:
            rows.append(line)
    return rows


DATE_RE = re.compile(r"^\d{1,2}/\d{4}\s*[-\u2013]\s*(\d{1,2}/\d{4}|Present)$|"
                     r"^[A-Z][a-z]{2}\s+\d{4}\s+to\s+([A-Z][a-z]{2}\s+\d{4}|Present)$",
                     re.I)


def _split_heading(text):
    """'Title \u00b7 Org \u00b7 Location \u00b7 06/2018 - 06/2026' -> (title, org, dates).

    Location is dropped: it is noise on a resume where the header already
    carries one. A trailing part that does not look like a date range is kept
    as part of the org, so an unexpected heading shape still renders.
    """
    parts = [x.strip() for x in text.split("\u00b7") if x.strip()]
    dates = ""
    if parts and DATE_RE.match(parts[-1]):
        dates = parts.pop()
    title = parts.pop(0) if parts else text.strip()
    org = parts.pop(0) if parts else ""
    return title, org, dates


def _entries(content):
    """Split an Experience section into [(heading, body_md)] on '### '."""
    out, head, buf = [], None, []
    for line in content.split("\n"):
        if line.strip().startswith("### "):
            if head is not None:
                out.append((head, "\n".join(buf).strip()))
            head, buf = line.strip()[4:].strip(), []
        elif head is not None:
            buf.append(line)
    if head is not None:
        out.append((head, "\n".join(buf).strip()))
    return out


def resume_html(resume_md, profile, base=11):
    """Single-column resume layout: DM Serif Display
    name, DM Sans body, terracotta section headings, right-aligned date rail."""
    s = parse_sections(resume_md)

    # The model sometimes appends stray role headings after Education or
    # Skills. Anything from a "###" onward inside those sections belongs
    # to Experience; relocate it rather than rendering it as garbage.
    for key in ("education", "skills"):
        content = s.get(key) or ""
        if "###" in content:
            head, tail = content.split("###", 1)
            s[key] = head.strip()
            s["experience"] = (s.get("experience", "").rstrip() +
                               "\n\n### " + tail.lstrip())

    def body_html(md):
        return md_to_html(unwrap_bullets(md), reflow=True)

    parts = []

    bits = [x for x in (profile.get("location"), profile.get("phone"),
                        profile.get("email"), short_link(profile.get("portfolio")),
                        short_link(profile.get("linkedin"))) if x]
    contact = "<span class='sep'> \u00b7 </span>".join(
        f"<span>{html.escape(b)}</span>" for b in bits)
    parts.append("<header>")
    parts.append(f"<h1>{html.escape(profile.get('name', ''))}</h1>")
    if profile.get("title"):
        parts.append(f"<p class='role'>{html.escape(profile['title'])}</p>")
    parts.append(f"<p class='contact'>{contact}</p>")
    parts.append("</header>")

    if s.get("summary"):
        parts.append("<h2>Summary</h2>")
        parts.append(f"<p class='summary'>{_inline(' '.join(s['summary'].split()))}</p>")

    if s.get("experience"):
        parts.append("<h2>Experience</h2>")
        entries = _entries(s["experience"])
        if not entries:
            parts.append(body_html(s["experience"]))
        for heading, body in entries:
            title, org, dates = _split_heading(heading)
            label = _inline(title)
            if org:
                label += f", <span class='org'>{_inline(org)}</span>"
            parts.append("<div class='entry'><div class='entry-head'>"
                         f"<h3 class='title'>{label}</h3>"
                         f"<span class='dates'>{html.escape(dates)}</span>"
                         "</div>")
            if body:
                rendered = body_html(body)
                rendered = rendered.replace("<p>", "<p class='blurb'>")
                parts.append(rendered)
            parts.append("</div>")

    if s.get("side projects"):
        parts.append("<h2>Side projects</h2>" + body_html(s["side projects"]))

    if s.get("education"):
        rows = []
        for r in _section_rows(s["education"]):
            cols = [c.strip() for c in r.split("\u00b7")]
            year = cols.pop() if len(cols) > 1 and re.fullmatch(r"\d{4}", cols[-1]) else ""
            lead = cols.pop(0) if cols else r
            rest = ", ".join(cols)
            left = f"<b>{_inline(lead)}</b>" + (f", {_inline(rest)}" if rest else "")
            rows.append(f"<div class='edu-line'><span>{left}</span>"
                        f"<span class='dates'>{html.escape(year)}</span></div>")
        parts.append("<h2>Education</h2>\n" + "\n".join(rows))

    if s.get("skills"):
        paras = [" ".join(b.split()) for b in re.split(r"\n\s*\n", s["skills"]) if b.strip()]
        paras = [re.sub(r"^[-*]\s+", "", x) for x in paras]
        parts.append("<div class='skills'><h2>Skills</h2>" +
                     "".join(f"<p>{_inline(x)}</p>" for x in paras) + "</div>")

    fonts = ("<link rel='preconnect' href='https://fonts.googleapis.com'>"
             "<link href='https://fonts.googleapis.com/css2?family=DM+Serif+Display"
             "&family=DM+Sans:wght@400;500;600&display=swap' rel='stylesheet'>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Resume</title>{fonts}<style>{RESUME_CSS.format(base=base)}</style>"
        f"</head><body>{''.join(parts)}</body></html>")


def _chrome_print(page_html, pdf_path):
    fd, html_path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page_html)
        proc = subprocess.run(
            [find_chrome(), "--headless", "--disable-gpu",
             "--no-pdf-header-footer",
             # Chrome prints as soon as the DOM is ready, which lands before
             # the Google Fonts request finishes and silently falls back to
             # Georgia. Advance virtual time so the webfonts are in.
             "--virtual-time-budget=8000",
             f"--print-to-pdf={pdf_path}", f"file://{html_path}"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0 or not os.path.exists(pdf_path):
            raise PdfError(f"chrome exited {proc.returncode}: {proc.stderr[-300:]}")
    except subprocess.TimeoutExpired:
        raise PdfError("chrome timed out rendering the PDF")
    except OSError as e:
        raise PdfError(f"chrome could not start: {e}")
    finally:
        try:
            os.unlink(html_path)
        except OSError:
            pass

    # Chrome tags everything it writes with com.apple.quarantine. Preview then
    # tries to notarization-check the PDF, gets nothing back, and refuses to
    # open it with a malware warning. Acrobat ignores the tag, Preview does
    # not. Strip it so the file opens everywhere.
    # os.removexattr does not exist on macOS, so shell out to xattr instead.
    subprocess.run(["xattr", "-d", "com.apple.quarantine", pdf_path],
                   capture_output=True)


def _pdf_pages(pdf_path):
    """Page count from Chrome's PDF output, by object markers."""
    with open(pdf_path, "rb") as f:
        data = f.read()
    n = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
    return n if n > 0 else 1


def render_resume_pdf(resume_md, profile, pdf_path):
    """Render the resume markdown (no contact block) as a styled one-page
    PDF: largest type first, stepping down only when a page overflows."""
    for base in RESUME_SCALES:
        _chrome_print(resume_html(resume_md, profile, base=base), pdf_path)
        if _pdf_pages(pdf_path) == 1:
            return
    # still long at the smallest scale: leave the last render (multi-page
    # beats silently dropping content; the one-page budget lives in the
    # tailoring prompt)


def render_pdf(md, pdf_path, title=""):
    """Render markdown to a plain single-column PDF. Raises PdfError."""
    body = md_to_html(md)
    fonts = ("<link rel='preconnect' href='https://fonts.googleapis.com'>"
             "<link href='https://fonts.googleapis.com/css2?family=DM+Serif+Display"
             "&family=DM+Sans:wght@400;500;600&display=swap' rel='stylesheet'>")
    page = ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title>{fonts}"
            f"<style>{PAGE_CSS}</style>"
            f"</head><body>{body}</body></html>")
    _chrome_print(page, pdf_path)
