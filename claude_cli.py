"""Where the Claude Code CLI lives on this machine.

Used only when LLM_PROVIDER is claude-cli (the default). which() alone is
not enough: anything launched with a minimal PATH misses
the usual install directories, so the known locations are checked by hand.
CLAUDE_BIN in the environment wins over all of it.
"""

import os
import shutil

_KNOWN_LOCATIONS = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def find_claude():
    """Return a path to the claude binary, or the bare name as a last resort.

    Never raises. A wrong answer here surfaces as the subprocess failing to
    start, which callers handle.
    """
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override

    found = shutil.which("claude")
    if found:
        return found

    for location in _KNOWN_LOCATIONS:
        path = os.path.expanduser(location)
        if os.path.exists(path):
            return path

    return "claude"
