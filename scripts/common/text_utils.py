"""Text processing helpers shared across pipeline modules."""

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def slug(title: str) -> str:
    """Convert an issue title to a URL-safe lowercase slug."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]


def strip_ansi(text: str) -> str:
    """Remove terminal colour/cursor escape sequences (browser and test-runner errors carry them)."""
    return _ANSI.sub("", text)
