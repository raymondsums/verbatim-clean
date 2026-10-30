# Verbatim

A job application helper that runs on your own computer. It tailors your
resume and cover letter to a posting, fills application forms from a
Chrome extension, offers the answers you wrote last time, and keeps a
list of where you applied.

The idea it is built on: your facts go in **exactly as you wrote them**.
Your name, phone, links, dates and work status come from a file you fill
in, character for character. The AI never sees them, so it can never
round "6 years" to "5+". Longer answers are matched to ones you wrote
before and shown to you; nothing goes into a form until you press the
button. Anything it cannot identify is flagged rather than guessed.
Salary, demographic and work authorization questions are always yours to
answer. **It never submits anything.**

```
Chrome extension
      |  http://localhost:5053
      v
Verbatim server (Python, on your computer)
      |-> your AI         (Claude Code, the Anthropic API, OpenAI, Ollama...)
      |-> notes/          (your saved answers and stories, plain Markdown)
      |-> data/           (your application list and job board)
```

## What you need

- Python 3.9 or newer (`python3 --version`)
- Chrome, or a Chrome-based browser (Arc, Brave, Edge)
- An AI, one of:
  - **Claude Code** (the default). Uses your Claude plan, no API key.
  - An **Anthropic API key**.
  - Anything that speaks the **OpenAI chat API**: OpenAI itself, or a
    model running on your own computer with Ollama or LM Studio.
- Optional: Google Chrome installed, for making PDFs. Without it you still
  get Markdown and text versions.

## The quick way

Open this folder in Claude Code (or another coding agent) and paste the
whole of [SETUP_PROMPT.md](SETUP_PROMPT.md) into it. It checks your setup,
installs everything, asks for your details, and walks you through the
extension. The steps below are the same thing by hand.

## Setup by hand

### 1. Install

```bash
cd verbatim
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Pick your AI

Copy the settings file:

```bash
cp .env.example .env
```

Then set `LLM_PROVIDER` in `.env`:

- **Claude Code** (`LLM_PROVIDER=claude-cli`, the default). Install it from
  https://claude.com/claude-code, run `claude` once in a terminal and sign
  in. Verbatim finds it on its own. If it cannot, set `CLAUDE_BIN` to the
  output of `which claude`.
- **Anthropic API** (`LLM_PROVIDER=anthropic`). Run `pip install anthropic`,
  then set `ANTHROPIC_API_KEY`.
- **OpenAI, Ollama or LM Studio** (`LLM_PROVIDER=openai-compatible`). Set
  `LLM_BASE_URL` and `LLM_MODEL`, plus `LLM_API_KEY` for OpenAI. There are
  examples for each in `.env.example`. Small local models work, but
  tailoring and drafting are noticeably better with a large model.

### 3. Add your details

```bash
cp profile.example.json profile.json
cp content/master.example.md content/master.md
cp -r notes.example notes
```

- **`profile.json`** holds your facts: name, contact details, links,
  education, past jobs. Forms are filled from it word for word, so check
  it carefully. Leave the work authorization, sponsorship and EEO fields as
  `null` and those questions will be flagged for you every time instead
  of answered.
- **`content/master.md`** is everything tailoring writes from: your real
  bullets, numbers and projects. It only reorders and rewords what is
  here, so the more you put in, the better the results. A thin file gets
  you generic prose.
- **`notes/`** is a plain folder of Markdown files. Your saved answers and
  your story bank build up here as you use Verbatim. You can edit them by
  hand. Delete the example story once you have your own.

Optional extras:

- `content/motivation.md` (copy `content/motivation.example.md`): why you
  are looking, in your own words. Used for "why this company" answers.
- `content/resume.html`: your own resume as an HTML page. When it exists,
  tailoring edits that page instead of writing a new resume, so your
  layout stays the same.
- `resume_pdf` in `profile.json`: the PDF the extension attaches when there
  is no tailored one.

### 4. Start the server

```bash
python3 server.py
```

Open http://localhost:5053. If you see the Verbatim page, it works. Keep
this terminal open while you use it. To use another port, set `PORT` in
`.env` and use the same address in the extension.

### 5. Load the Chrome extension

The extension is not in the Chrome Web Store, so you load it by hand:

1. Go to `chrome://extensions` (Arc: `arc://extensions`, Brave:
   `brave://extensions`, Edge: `edge://extensions`).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the `extension` folder inside this
   folder.
4. Pin Verbatim from the puzzle piece icon so you can see it.
5. Click the Verbatim icon, open **Settings**, check the server address
   (`http://localhost:5053` unless you changed the port) and press **Save
   and test**. It should say the server is answering.

## First use

1. On the Verbatim page, open **Tailor**, paste a job description or a
   link, and make a resume and cover letter. Read the notes it gives you;
   they list anything the posting asks for that your master file does
   not cover.
2. Open a job application in Chrome. Click the Verbatim icon and press
   **Open panel**, or use **Alt+Shift+F** to fill the form. Fields it is
   sure of are filled from your profile. Fields it will not answer are
   flagged in amber for you.
3. For longer questions, the panel shows your past answers and can draft a
   new one from your material. You edit it, then insert it.
4. After you submit, press **Log it** in the panel. The application goes
   into your list (the **Jobs** page, Applied) and your answers are saved
   to `notes/Application Answers.md` for next time.

## Set up the job board

The **Jobs** page can watch company job boards for roles that fit you.

1. `cp jobs/config.example.json jobs/config.json` and edit it:
   - `roles`: words a job title must contain, like `"product designer"` or
     `"data analyst"`. Any one is enough.
   - `exclude`: words that rule a title out, like `"intern"`.
   - `cities`: where you would work in person.
   - `allow_remote` and `remote_country`: whether to keep remote jobs, and
     from which country.
   - `seniority`: your level. Shown to the AI as context, never used as a
     filter.
   - `notify_mac`: a Mac notification for new strong matches.
   - `skip_defense`: `true` hides defense and national security roles.
2. Edit `jobs/seed_companies.json` to list the companies you care about,
   then run `python3 -m jobs.boards` to find their job boards.
3. Run `python3 -m jobs.watch` to fetch jobs. Run it again whenever you
   like, or schedule it hourly with cron. Each new job gets one short AI
   call to rate the fit, so with a paid API the first run on many boards
   costs a little.

## Troubleshooting

- **The extension says it cannot reach the server.** Is `python3 server.py`
  still running? Does the address in the extension's Settings match the
  port? Open http://localhost:5053/api/health in Chrome: it should show
  `"ok": true`.
- **"claude CLI could not start" or "exited 1".** Run `claude` in a
  terminal and sign in again. If Verbatim cannot find it, set `CLAUDE_BIN`
  in `.env` to the output of `which claude` and restart the server.
- **"ANTHROPIC_API_KEY is not set" or "LLM_BASE_URL and LLM_MODEL must be
  set".** Check `.env`, then restart the server. Settings are read when it
  starts.
- **Ollama or LM Studio returns errors or empty drafts.** Make sure the
  model is running and the base URL ends in `/v1`. Try a larger model;
  small ones often fail to return the structured answers Verbatim needs.
- **"The answer note could not be read from the notes folder."** Check
  that `NOTES_DIR` in `.env` (if set) points at a folder that exists.
- **No PDF.** PDFs are printed with Google Chrome. Install it, or set
  `CHROME_PATH` in `.env` to your Chrome or Chromium binary.
- **The extension does nothing on a page.** Reload the page after loading
  or updating the extension. On `chrome://extensions`, press the reload
  arrow on Verbatim after pulling new code.
- **A field was filled wrong.** Fix it in the form. When you press Log it,
  Verbatim learns the field for that site. Anything wrong in
  `profile.json` itself goes onto every form, so fix it there.

`/api/health` also shows which AI is set and where your notes folder is.

## The rules it will not break

- Nothing is ever submitted anywhere.
- Facts are filled verbatim from your profile, never generated.
- Work authorization and sponsorship answers are never written by the AI.
  Only your stored wording is used, and a question it does not clearly fit
  is left empty and flagged.
- EEO and demographic questions fill only from choices you stored, and are
  never written to your notes.
- Salary is your call every time.
- Tailoring reorders and rewords what is true. It never invents.
- Your saved answers keep your words exactly as given.

## Optional extras

- **Obsidian.** Set `OBSIDIAN_VAULT` in `.env` to your vault and the notes
  go in a `Job Search` folder inside it. With `OBSIDIAN_API_KEY` set,
  Verbatim goes through the Local REST API plugin instead of the files.
- **Claims you never want made.** In `profile.json`, `never_claim` lists
  things you worked on but did not build or lead (for example
  `"design system"`). Drafts that say you built, led or owned one are
  refused. `leave_out_skills` lists skills a tailored resume never shows.
- **Your own writing as a style guide.** `notes/Writing in My Voice.md`
  and `notes/Voice Samples.md` teach drafts how you sound. See
  `.env.example` for more sources.
- **Speak an answer.** The mic button appears when `whisper-cli`, `ffmpeg`
  and a whisper model are installed (see `dictate.py`).
- **Run it in the background on a Mac.** Edit the paths in
  `com.verbatim.server.plist.example`, save it as
  `~/Library/LaunchAgents/com.verbatim.server.plist`, and run
  `launchctl load ~/Library/LaunchAgents/com.verbatim.server.plist`.

## Layout

| Path | What it is |
|---|---|
| `server.py` | the local server, port 5053 |
| `llm.py` | every AI call goes through here |
| `profile.json` | your facts, filled verbatim (not in git) |
| `content/master.md` | everything tailoring writes from (not in git) |
| `notes/` | saved answers, story bank, voice notes (not in git) |
| `tailor.py` | resume and cover letter |
| `autofill.py` | which form field is which |
| `answers.py` | saved answers: matching, drafting, recording |
| `applied.py`, `tracker.py` | logging an application, the application list |
| `jobs/` | the job board |
| `extension/` | the Chrome extension |
| `tests/` | unit tests plus replica application forms |

## Tests

```bash
pip install pytest
python3 -m pytest tests
```

The tests use stubs, so they need no AI, no network and no notes of your
own. `tests/run_pages.py` is separate: it runs the extension's fill code
on the replica forms in `tests/ats/` in headless Chrome. Start the server
with the invented test person first:

```bash
VERBATIM_PROFILE=tests/fixtures/profile.test.json python3 server.py
python3 tests/run_pages.py
```

## License

MIT
