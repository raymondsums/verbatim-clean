"""Keep every test away from the real notes folder and the host's .env.

Importing server.py loads .env. A .env with OBSIDIAN_MODE=file once let
tests that expected a stubbed REST API write straight into the real
answer note. load_dotenv never overrides a variable that is
already set, so blanking these first keeps the host's settings out, and the
vault folder points at an empty temp dir for the whole run.

Imported by conftest.py under pytest, and at the top of each test file
that loads answers or server, so running a file on its own is safe too.
"""

import os
import sys
import tempfile

for _name in ("OBSIDIAN_MODE", "PUBLIC_BASE", "RESUME_PDF", "NOTES_DIR",
              "OBSIDIAN_VAULT", "OBSIDIAN_API_KEY", "LLM_PROVIDER"):
    os.environ[_name] = ""

# Practice cards go to a throwaway database, never data/cue.sqlite.
os.environ["CUE_DB"] = os.path.join(tempfile.mkdtemp(), "cue.sqlite")
# Applications too, never data/tracker.sqlite.
os.environ["TRACKER_DB"] = os.path.join(tempfile.mkdtemp(), "tracker.sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import answers  # noqa: E402
import claims  # noqa: E402

# The invented test person, whose never_claim and leave_out_skills the
# claim checks are tested against.
claims.PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "fixtures", "profile.test.json")

answers.VAULT_DIR = os.path.join(tempfile.mkdtemp(), "Vault")

# The version log (read by the Answers page for how much the user changed each
# draft) and the voice samples cache and logs stay out of the real files.
_TMP = tempfile.mkdtemp()
answers.VERSIONS_LOG = os.path.join(_TMP, "versions.jsonl")

import voicesamples  # noqa: E402

voicesamples.CACHE_PATH = os.path.join(_TMP, "voice_samples.json")
voicesamples.SPOKEN_LOG = os.path.join(_TMP, "spoken.jsonl")
voicesamples.PORTFOLIO_DIR = os.path.join(_TMP, "portfolio")
