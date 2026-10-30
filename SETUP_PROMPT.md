# Set up Verbatim with a coding agent

Open this folder in Claude Code (or another coding agent that can run
commands and edit files), then paste everything inside the box below as
one message. It does the setup with you and asks before it writes
anything about you.

````text
You are setting up Verbatim, a job application helper, in the current
folder. Read README.md first. Then work through the steps below in order.
Ask me questions one at a time and wait for my answer. Use plain words.
Do not invent anything about me: every fact you write must come from what
I tell you or from a file I give you. Never commit or push anything.

1. Check the basics.
   - Run `python3 --version`. It must be 3.9 or newer. If it is missing or
     too old, tell me how to install a current Python for my system and
     stop until I have.
   - Check that Google Chrome (or Chromium) is installed. If not, tell me
     PDFs will not work until it is, and carry on.

2. Install.
   - Make a virtual environment: `python3 -m venv .venv`.
   - Install with `.venv/bin/pip install -r requirements.txt`
     (on Windows: `.venv\Scripts\pip install -r requirements.txt`).
   - Copy the starting files, without overwriting any that already exist:
     `.env.example` to `.env`, `profile.example.json` to `profile.json`,
     `content/master.example.md` to `content/master.md`,
     `notes.example/` to `notes/`, and
     `jobs/config.example.json` to `jobs/config.json`.

3. Ask which AI I want to use, and set it in `.env`:
   - Claude Code (LLM_PROVIDER=claude-cli, the default). Check that
     `claude` runs. If it is not installed, point me to
     https://claude.com/claude-code. Ask me to run `claude` once and sign
     in if I have not. If Verbatim might not find it, set CLAUDE_BIN to
     the output of `which claude`.
   - The Anthropic API (LLM_PROVIDER=anthropic). Install it with
     `.venv/bin/pip install anthropic`. Ask me to paste my API key
     straight into `.env` as ANTHROPIC_API_KEY myself, so it never goes
     through this chat.
   - OpenAI, Ollama or LM Studio (LLM_PROVIDER=openai-compatible). Set
     LLM_BASE_URL and LLM_MODEL from the examples in `.env.example`. For
     OpenAI, ask me to paste my key into `.env` as LLM_API_KEY myself. For
     Ollama or LM Studio, check the server answers at that address.

4. Fill in my profile (`profile.json`).
   - Ask me for my resume, as a file path I can give you (PDF, Word,
     Markdown or text) or pasted text. Read it.
   - From it, fill in: name, title, email, phone, location, portfolio,
     linkedin, github, years_of_experience, education and
     employment_history. Show me what you filled in and ask me to correct
     it. These are typed into real applications word for word, so get my
     confirmation on every one.
   - Ask where my resume PDF is and set resume_pdf to that path.
   - Ask for my legal name and preferred name, and fill the name fields.
   - Work authorization and sponsorship: explain that I can store my own
     exact answers (authorized_to_work_in_us, requires_sponsorship) or
     leave them null so those questions are flagged for me every time.
     Use only my own wording. Never suggest an answer.
   - EEO questions (gender, race and ethnicity, veteran status,
     disability): explain they are optional and filled only from what I
     store. Leave them null unless I give you my answers.
   - Ask whether there is anything I worked on but did not build or lead,
     which drafts should never claim I built (never_claim), and any skills
     a tailored resume should leave out (leave_out_skills). Both can stay
     empty.
   - Replace or remove every example value. The "_note" keys can stay.

5. Write my master content file (`content/master.md`).
   - Build it from my resume and anything else I share: past jobs with
     dates, projects, real numbers, and skills, following the layout the
     example file shows. Keep my numbers exactly as I gave them.
   - Ask me for one or two more details per job that a resume leaves out
     (what the problem was, what I did, what changed). More real detail
     here means better tailoring.
   - Show it to me and make the changes I ask for.
   - Optional: ask if I want to write a few lines on why I am looking.
     If yes, save my words in `content/motivation.md` (start from
     `content/motivation.example.md`).
   - In `notes/Story Bank.md`, remove the example stories, or replace them
     with one or two of my real ones in the same format if I want.

6. Set up the job board (`jobs/config.json`). Ask me:
   - What roles am I looking for? Put the job title words in "roles"
     (for example "product designer", "data analyst").
   - Any title words to skip? Put them in "exclude" (for example
     "intern", "manager").
   - Which cities would I work in? Put them in "cities".
   - Am I open to remote work, and from which country? Set "allow_remote"
     and "remote_country".
   - My level (for example junior, mid, senior). Put it in "seniority".
   - Do I want defense and national security roles hidden? Set
     "skip_defense".
   - On a Mac, do I want a notification for new strong matches? Set
     "notify_mac".
   - Ask which companies I want to watch, replace the examples in
     `jobs/seed_companies.json` with them, then run
     `.venv/bin/python -m jobs.boards` to find their job boards.

7. Start the server.
   - Find a free port: check that nothing is listening on 5053 (on Mac or
     Linux: `lsof -iTCP:5053 -sTCP:LISTEN`). If 5053 is taken, pick
     another and set PORT in `.env`.
   - Start it in the background: `.venv/bin/python server.py`.
   - Open http://localhost:<port>/api/health and check it says "ok": true
     and shows the AI I picked. Then tell me to open
     http://localhost:<port> in Chrome.
   - Tell me that to start it again later, I run
     `.venv/bin/python server.py` in this folder.

8. Walk me through loading the Chrome extension, one step at a time,
   waiting for me after each:
   - Go to chrome://extensions (arc://extensions in Arc,
     brave://extensions in Brave, edge://extensions in Edge).
   - Turn on Developer mode, top right.
   - Click Load unpacked and choose the `extension` folder inside this
     folder. Tell me its full path.
   - Pin Verbatim from the puzzle piece icon.
   - Click the Verbatim icon, open Settings, set the server address to
     http://localhost:<port>, and press Save and test. It should say the
     server is answering.

9. Finish with a short summary: what is set up, which AI it uses, how to
   start the server, and the first thing to try (the Tailor page with a
   real job posting). Point me to the Troubleshooting section of
   README.md.
````
