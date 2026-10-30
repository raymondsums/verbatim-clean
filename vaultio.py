"""Reading and writing note files, on a host where macOS may say no.

When the server runs under launchd and the notes folder is in iCloud
Drive, macOS privacy rules (TCC) can refuse that python any file it did
not create itself: "Operation not permitted". The fix is a Full Disk
Access grant for that python in System Settings, or keeping the notes
folder outside iCloud Drive.

Until then, reads fall back to a snapshot: copies of the notes Verbatim
reads, put in data/vault_snapshot/ by hand if you need it. A snapshot
goes stale, so status() says which one is in use and the
server logs it. Writes never fall back; a refused write raises Refused and
the answer note's own queue keeps the change until the vault can be
reached.
"""

import os
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_DIR = os.path.join(BASE_DIR, "data", "vault_snapshot")

_LAST = {"read": ""}  # "vault" or "snapshot", for status()


class Refused(OSError):
    """macOS refused the vault and there is no snapshot of the file."""


def _snapshot(rel):
    path = os.path.join(SNAPSHOT_DIR, rel)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise Refused(f"macOS refused {rel} and there is no snapshot")
    _LAST["read"] = "snapshot"
    return text


def read(vault_dir, rel):
    """A vault file's text. FileNotFoundError when it does not exist,
    Refused when it cannot be reached."""
    try:
        with open(os.path.join(vault_dir, rel), encoding="utf-8") as f:
            text = f.read()
    except PermissionError:
        return _snapshot(rel)
    _LAST["read"] = "vault"
    return text


def write(vault_dir, rel, text):
    """Write a vault file in one step (a temp file, then a rename), so
    a sync tool or Obsidian never sees half of it."""
    path = os.path.join(vault_dir, rel)
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".verbatim-", suffix=".tmp",
                                   dir=folder)
    except PermissionError:
        raise Refused(f"macOS refused writing {rel}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, PermissionError):
            raise Refused(f"macOS refused writing {rel}")
        raise


def listdir(vault_dir, rel=""):
    """Note names in a vault folder ("" for the top). The snapshot's when
    macOS refuses the folder. [] when neither exists."""
    for base in (vault_dir, SNAPSHOT_DIR):
        try:
            return sorted(os.listdir(os.path.join(base, rel)))
        except FileNotFoundError:
            if base == vault_dir:
                continue
            return []
        except PermissionError:
            continue
    return []


def status():
    """Where the last read came from: "vault", "snapshot" or ""."""
    return _LAST["read"]
