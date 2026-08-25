"""Text processing helpers shared across pipeline modules."""

import re


def slug(title: str) -> str:
    """Convert an issue title to a URL-safe lowercase slug."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
