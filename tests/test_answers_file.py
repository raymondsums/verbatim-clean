"""Tests for the plain-file way of reaching the answer note.

This is the default: the note is a plain file in the notes folder, with no
Obsidian app and no REST API. Everything runs against a temp folder. Run: python3 tests/test_answers_file.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import answers  # noqa: E402

REPO = """# Application Answers

## Why this company

**Current answer**
Because the product is one I would use myself.

**Used**
- Umbrella, Product Designer, 2026-08-06

**Seen as**
- "What draws you to Umbrella?"
"""


class vault:
    """A temp vault, file mode on, cache and queue in the same temp dir."""

    def __init__(self, note=REPO, mode="file", key=None, exists=True):
        self.note, self.mode, self.key, self.exists = note, mode, key, exists

    def __enter__(self):
        d = tempfile.mkdtemp()
        self.dir = os.path.join(d, "Vault")
        if self.exists:
            os.makedirs(os.path.join(self.dir, "Job Search"))
        self.path = os.path.join(self.dir, answers.ANSWER_FILE)
        if self.exists and self.note is not None:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(self.note)
        self.old = (answers.VAULT_DIR, answers.CACHE_PATH, answers.QUEUE_PATH,
                    os.environ.get("OBSIDIAN_MODE"),
                    os.environ.get("OBSIDIAN_API_KEY"),
                    answers.requests.get, answers.requests.put)
        answers.VAULT_DIR = self.dir
        answers.CACHE_PATH = os.path.join(d, "cache.md")
        answers.QUEUE_PATH = os.path.join(d, "queue.json")
        _set("OBSIDIAN_MODE", self.mode)
        _set("OBSIDIAN_API_KEY", self.key)

        def no_network(*a, **kw):
            raise AssertionError("file mode must not call the REST API")

        answers.requests.get = answers.requests.put = no_network
        return self

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def __exit__(self, *exc):
        (answers.VAULT_DIR, answers.CACHE_PATH, answers.QUEUE_PATH, mode, key,
         answers.requests.get, answers.requests.put) = self.old
        _set("OBSIDIAN_MODE", mode)
        _set("OBSIDIAN_API_KEY", key)


def _set(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def test_mode_choice():
    with vault(mode=None, key=None):
        assert answers._file_mode(), "no key and no mode should use the file"
    with vault(mode=None, key="k"):
        assert not answers._file_mode(), "a key with no mode keeps REST"
    with vault(mode="file", key="k"):
        assert answers._file_mode(), "OBSIDIAN_MODE=file wins over a key"
    with vault(mode="rest", key=None):
        assert not answers._file_mode(), "OBSIDIAN_MODE=rest wins"


def test_reads_the_note_from_the_vault_and_caches_it():
    with vault() as v:
        assert answers.read_repo() == REPO
        with open(answers.CACHE_PATH, encoding="utf-8") as f:
            assert f.read() == REPO


def test_missing_note_is_seeded():
    with vault(note=None) as v:
        assert answers.read_repo() == answers.SEED
        assert v.read() == answers.SEED


def test_writes_land_in_the_file_and_leave_no_temp_files():
    with vault() as v:
        out = answers.record("Why do you want to work at Umbrella?",
                             "Because I use it every day.",
                             company="Umbrella", role="Product Designer")
        assert out == "created", out
        text = v.read()
        assert "Because I use it every day." in text
        assert "Because the product is one I would use myself." in text
        left = [n for n in os.listdir(os.path.dirname(v.path))
                if n.endswith(".tmp")]
        assert not left, left
        with open(answers.CACHE_PATH, encoding="utf-8") as f:
            assert f.read() == text


def test_missing_vault_falls_back_to_cache_then_queue_then_flushes():
    with vault(exists=False) as v:
        with open(answers.CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(REPO)
        # read falls back to the cache
        assert answers.read_repo() == REPO
        # a save is queued, not lost
        answers.record("Why Linear?", "Speed is the product.", company="Linear")
        assert answers._read_queue(), "the save should be queued"
        # the vault comes back: the next read replays the queue into the file
        os.makedirs(os.path.join(v.dir, "Job Search"))
        with open(v.path, "w", encoding="utf-8") as f:
            f.write(REPO)
        text = answers.read_repo()
        assert "Speed is the product." in text
        assert "Speed is the product." in v.read()
        assert not answers._read_queue()


def test_missing_vault_without_cache_says_so_plainly():
    with vault(exists=False):
        try:
            answers.read_repo()
        except answers.AnswersError as e:
            assert "notes folder" in str(e), e
        else:
            raise AssertionError("no vault and no cache should raise")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
