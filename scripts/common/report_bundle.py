"""Shared plumbing for skill reports: markdown cell escaping, evidence copying, and the md/html/json bundle."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from scripts.common.redact import redact, redact_obj
from scripts.common.report_renderer import render_simple_html

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MD_SPECIAL = re.compile(r"([\[\]`<>|])")


def md_cell(text: object) -> str:
    """Redact secrets and make untrusted ``text`` inert inside a markdown table cell (no links, tags, code spans)."""
    return _MD_SPECIAL.sub(r"\\\1", redact(str(text))).replace("\n", " ").strip()


def collect_evidence(out_dir: Path, sources: list[str], roots: list[Path]) -> dict[str, str]:
    """Copy evidence files next to the report so its links keep working once only ``reports/`` is uploaded.

    Only files under one of ``roots`` are accepted (paths come from result files a generated script can write).
    Images are copied as they are; every other file is read as text and redacted, and skipped when it is not text.
    Returns {original path: link relative to the report}.
    """
    allowed = [r.resolve() for r in roots]
    links: dict[str, str] = {}
    dest = out_dir / "evidence"
    for src in dict.fromkeys(s for s in sources if s):
        path = Path(src).resolve()
        if not path.is_file() or not any(path.is_relative_to(r) for r in allowed):
            continue
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            data = None
        else:
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            try:
                data = redact(raw.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        dest.mkdir(parents=True, exist_ok=True)
        name, n = path.name, 1
        while (dest / name).exists():
            n += 1
            name = f"{path.stem}-{n}{path.suffix}"
        if data is None:
            shutil.copyfile(path, dest / name)
        else:
            (dest / name).write_text(data, encoding="utf-8")
        links[src] = f"evidence/{name}"
    return links


def write_bundle(out_dir: Path, title: str, markdown: str, result: dict) -> None:
    """Write report.md, report.html and result.json, all redacted (overwrites: a rerun replaces the previous report)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = redact(markdown)
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")
    (out_dir / "report.html").write_text(render_simple_html(title, markdown), encoding="utf-8")
    (out_dir / "result.json").write_text(json.dumps(redact_obj(result), indent=2), encoding="utf-8")
