"""Evidence hygiene for the QA report: start clean on every render, and optionally make the HTML self-contained."""

from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

EMBED_PER_FILE = 300_000   # bytes: larger images stay links
EMBED_TOTAL = 3_000_000    # bytes of images inlined into one report
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
_SRC = re.compile(r'src="(evidence/[^"]+)"')


def reset(out_dir: Path) -> None:
    """Remove evidence copied by an earlier render of this report: a rerun replaces it instead of piling up ``name-2.png``."""
    shutil.rmtree(out_dir / "evidence", ignore_errors=True)


def embed_images(html: str, out_dir: Path, per_file: int = EMBED_PER_FILE, total: int = EMBED_TOTAL) -> str:
    """Inline small evidence images as data URIs so ``report.html`` can be shared on its own; big ones stay links."""
    used = 0

    def swap(m: re.Match) -> str:
        nonlocal used
        rel = m.group(1)
        path = (out_dir / rel)
        mime = _MIME.get(path.suffix.lower())
        try:
            if not mime or not path.resolve().is_relative_to((out_dir / "evidence").resolve()) or not path.is_file():
                return m.group(0)
            size = path.stat().st_size
            if size > per_file or used + size > total:
                return m.group(0)
            used += size
            return f'src="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode("ascii")}"'
        except OSError:
            return m.group(0)

    return _SRC.sub(swap, html)
