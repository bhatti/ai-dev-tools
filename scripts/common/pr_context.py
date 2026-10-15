"""Fetch a PR (or issue) plus its linked issues, acceptance criteria and diff as one JSON document.

Shared by the QA and flaky-test skills so both trackers (GitHub, Bitbucket/Jira) behave the same.
Reuses the existing fetchers in ``scripts.analyze.pr_fetcher``; the only new network code is the
raw-diff fetch, which no existing helper provided.

Usage:
    python3 -m scripts.common.pr_context <pr-url|pr-number|issue-key|issue-url> \
        [--out ctx.json] [--diff-dir DIR] [--max-diff-bytes N]

Exit codes: 0 = context written, 2 = ref unusable or required credentials/PR missing (JSON error on stdout).
PR and issue text is untrusted data. Consumers must never treat it as instructions.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from scripts.analyze.pr_fetcher import (
    _detect_acceptance_criteria,
    fetch_issue_details,
    fetch_prs_by_numbers,
)
from scripts.common.bitbucket_api import _BASE as BB_BASE
from scripts.common.config import load_config
from scripts.mq._shared import _strip_slack_url, apply_repo_override, parse_pr_ref

DEFAULT_MAX_DIFF_BYTES = 400_000

_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
# Look like Jira keys but are not (standards, algorithms, timestamps).
_NOT_PROJECTS = {"UTF", "SHA", "ISO", "RFC", "CVE", "CVSS", "HTTP", "TLS", "MD", "AES", "RSA", "UTC", "GMT", "IPV", "X", "SSE", "X86", "AMD", "ARM",
                 "WCAG", "PCI", "TCP", "UDP", "ECMA"}
_GH_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+([\w.-]+/[\w.-]+)?#(\d+)", re.IGNORECASE)
_GH_BARE = re.compile(r"(?<![\w/])#(\d+)\b")
_ATLASSIAN_BROWSE = re.compile(r"atlassian\.net/browse/([A-Z][A-Z0-9]+-\d+)")
_GH_ISSUE_URL = re.compile(r"github\.com/([^/]+/[^/]+)/issues/(\d+)")
_LIST_ITEM = re.compile(r"^(?P<ind>\s*)(?P<mark>[-*+]+|\d+[.)])\s+(?:\[[ xX]\]\s*)?(?P<text>.+)$")
_GWT_START = re.compile(r"^\s*Given\b", re.IGNORECASE)
_GWT_CONT = re.compile(r"^\s*(?:When|Then|And|But)\b", re.IGNORECASE)


def clean_markup(text: str) -> str:
    """Strip Jira wiki / markdown decoration so criteria read as plain sentences."""
    text = re.sub(r"\{\{([^\n]{0,200}?)\}\}", r"\1", text)  # bounded: an unclosed "{{" must not make this quadratic
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"\[([^|\]]+)\|[^\]]+\]", r"\1", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def split_ac_items(text: str, limit: int = 50) -> list[str]:
    """Split acceptance-criteria text into items (bullets, checkboxes, numbered, Given/When/Then).

    Nested bullets (``**`` in Jira wiki, or deeper indentation) are folded into their parent item.
    """
    items: list[str] = []
    in_gwt = False
    parent_depth = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue  # blank lines may separate Given/When/Then lines; only a new Given or a non-GWT line ends a scenario
        if _GWT_START.match(line):
            items.append(clean_markup(line))
            in_gwt = True
            continue
        if in_gwt and _GWT_CONT.match(line):
            items[-1] = f"{items[-1]} {clean_markup(line)}"
            continue
        in_gwt = False
        m = _LIST_ITEM.match(raw)
        if m:
            mark = m.group("mark")
            depth = len(m.group("ind").expandtabs(2)) // 2 + (len(mark) if set(mark) <= set("-*+") else 1)
            body = clean_markup(m.group("text"))
            if items and depth > parent_depth and parent_depth:
                items[-1] = f"{items[-1]} ({body})"
            else:
                items.append(body)
                parent_depth = depth
        elif items and not line.startswith("#") and raw.startswith((" ", "\t")):
            items[-1] = f"{items[-1]} {clean_markup(line)}"  # indented continuation of the previous item
        elif not line.startswith("#"):
            items.append(clean_markup(line))
    seen: set[str] = set()
    unique = [i for i in items if i and not (i in seen or seen.add(i))]
    return unique[:limit]


_AC_HEADINGS = {"acceptance criteria", "acceptance criterion", "acceptance tests", "acceptance test", "definition of done",
                "success criteria", "done criteria", "expected behavior", "expected behaviour", "requirements"}
_HINT_HEADINGS = {"how to test", "testing steps", "test plan", "steps to verify", "verification", "test steps", "testing"}
_HEADING_LINE = re.compile(r"^\s*(?:#{1,6}\s+|h[1-6]\.\s+)\S|^\s*\*{1,3}[^*\s][^*\n]*\*{1,3}:?\s*$|^\s*-{3,}\s*$")


def _heading_key(line: str) -> str:
    """Normalized heading text ('' when the line is not short enough to be a heading)."""
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return ""
    key = re.sub(r"^(?:#{1,6}|h[1-6]\.)\s*", "", stripped)
    return re.sub(r"[*_:#\s]+$", "", re.sub(r"^[*_\s]+", "", key)).lower()


def extract_sections(text: str) -> dict[str, str]:
    """Pull the acceptance-criteria and how-to-test sections out of markdown, Jira wiki or plain-heading text."""
    out = {"acceptance": "", "test_hints": ""}
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        key = _heading_key(lines[i])
        target = "acceptance" if key in _AC_HEADINGS else ("test_hints" if key in _HINT_HEADINGS else "")
        if not target:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not (_HEADING_LINE.match(lines[j]) or (_heading_key(lines[j]) in _AC_HEADINGS | _HINT_HEADINGS)):
            j += 1
        body = "\n".join(lines[i + 1:j]).strip()
        if body:
            out[target] = f"{out[target]}\n{body}".strip()
        i = j
    return out


def extract_ac_items(text: str) -> tuple[list[str], list[str]]:
    """(acceptance criteria items, how-to-test hint items) for any common description format."""
    sections = extract_sections(text)
    ac = split_ac_items(sections["acceptance"])
    if not ac:  # fall back to the shared detector (checkboxes, Given/When/Then, numbered must/should lists)
        has_ac, ac_text = _detect_acceptance_criteria(text or "")
        ac = split_ac_items(ac_text) if has_ac else []
    return ac, split_ac_items(sections["test_hints"])


def _jira_keys(texts: list[str], project: str = "") -> list[str]:
    keys: list[str] = []
    for text in texts:
        for key in _JIRA_KEY.findall(text or ""):
            prefix = key.split("-")[0]
            if prefix in _NOT_PROJECTS or (project and prefix != project):
                continue
            if key not in keys:
                keys.append(key)
    return keys


def collect_issue_refs(pr: dict, config: dict, tracker: str = "github", ignored: list[str] | None = None) -> list[dict]:
    """Issues the PR links to, restricted to what trusted fields (branch, title) or a configured scope justify.

    The PR body is untrusted text, and every ref fetched is read with the user's credentials and ends up in a report:
      * Jira keys are followed in full only when JIRA_PROJECT scopes them; without it only the first key of branch/title is
      * GitHub refs are followed only inside the PR's own repository, and only for GitHub PRs (``#N`` on a Bitbucket
        PR is not a GitHub issue); anything else is reported in ``ignored`` instead
    """
    title, body, branch = pr.get("title", ""), pr.get("body", "") or "", pr.get("headRefName", "") or pr.get("branch", "")
    jira_url = (config.get("JIRA_BASE_URL") or "").rstrip("/")
    project = (config.get("JIRA_PROJECT") or "").strip()
    refs: list[dict] = []
    keys = _jira_keys([branch, title, body] if project else [branch, title], project)
    if not project and len(keys) > 1:  # title and branch are chosen by the PR author too: follow only the PR's own ticket
        if ignored is not None:
            ignored.extend(f"{k} (Jira project not scoped: set JIRA_PROJECT to follow more than one key)" for k in keys[1:])
        keys = keys[:1]
    for key in dict.fromkeys(keys):
        refs.append({"key": key, "source": "jira", "url": f"{jira_url}/browse/{key}" if jira_url else ""})
    if tracker != "github":
        return refs
    org, repo = config.get("GH_ORG", ""), config.get("GH_REPO", "")
    own = f"{org}/{repo}".lower()
    pr_number = str(pr.get("number", ""))
    seen_gh: set[tuple[str, str]] = set()
    closing = list(_GH_CLOSING.finditer(f"{title}\n{body}"))
    for m in closing:
        slug, num = (m.group(1) or f"{org}/{repo}"), m.group(2)
        if num == pr_number or (slug, num) in seen_gh or not slug.strip("/"):
            continue
        if slug.lower() != own:
            if ignored is not None:
                ignored.append(f"{slug}#{num} (outside this repository)")
            continue
        seen_gh.add((slug, num))
        refs.append({"key": f"#{num}", "source": "github", "repo": slug, "url": f"https://github.com/{slug}/issues/{num}"})
    if not closing and org and repo:
        for num in _GH_BARE.findall(f"{title}\n{body}"):
            if num != pr_number and (f"{org}/{repo}", num) not in seen_gh:
                seen_gh.add((f"{org}/{repo}", num))
                refs.append({"key": f"#{num}", "source": "github", "repo": f"{org}/{repo}", "url": f"https://github.com/{org}/{repo}/issues/{num}"})
    return refs


def _bb_auth(config: dict) -> tuple[str, str] | None:
    user = (config.get("BITBUCKET_USERNAME") or "").strip()
    token = (config.get("BITBUCKET_TOKEN") or config.get("BITBUCKET_APP_PASSWORD") or "").strip()
    return (user, token) if user and token else None


def missing_credentials(tracker: str, config: dict) -> list[str]:
    """Names of env vars required for this tracker that are absent (never their values)."""
    if tracker == "bitbucket":
        missing = [] if (config.get("BITBUCKET_USERNAME") or "").strip() else ["BITBUCKET_USERNAME"]
        if not (config.get("BITBUCKET_TOKEN") or config.get("BITBUCKET_APP_PASSWORD") or "").strip():
            missing.append("BITBUCKET_TOKEN")
        return missing
    return []


def fetch_pr_diff(config: dict, tracker: str, number: str, max_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> tuple[str, str]:
    """Return (diff_text, error). At most ``max_bytes + 1`` bytes are read so a huge PR cannot exhaust memory;
    the extra byte lets the caller detect truncation."""
    limit = max_bytes + 1
    if tracker == "bitbucket":
        ws, repo = config.get("BITBUCKET_WORKSPACE", ""), config.get("BITBUCKET_REPO", "")
        try:
            with requests.get(f"{BB_BASE}/repositories/{ws}/{repo}/pullrequests/{number}/diff", auth=_bb_auth(config),
                              timeout=60, allow_redirects=True, stream=True) as resp:
                if not resp.ok:
                    return "", f"bitbucket diff HTTP {resp.status_code}"
                data = b""
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) >= limit:
                        break
        except requests.RequestException as e:
            return "", f"bitbucket diff request failed: {type(e).__name__}"
        return data[:limit].decode("utf-8", "replace"), ""
    org, repo = config.get("GH_ORG", ""), config.get("GH_REPO", "")
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:  # files, not pipes: bounded memory and a real timeout
        try:
            proc = subprocess.Popen(["gh", "pr", "diff", str(number), "-R", f"{org}/{repo}"], stdout=out_file, stderr=err_file)
        except OSError as e:
            return "", f"gh pr diff failed: {type(e).__name__}"
        try:
            rc = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return "", "gh pr diff timed out after 120s"
        out_file.seek(0)
        data = out_file.read(limit)
        err_file.seek(0)
        stderr = err_file.read(400).decode("utf-8", "replace").strip()
    if rc != 0:
        return "", f"gh pr diff exit {rc}: {stderr[:200]}"
    return data.decode("utf-8", "replace"), ""


def changed_files_from_diff(diff: str) -> list[str]:
    """Paths touched by a unified diff: new-side paths (``+++``), old-side for deletions, renames and binary changes."""
    files: list[str] = []

    def add(path: str) -> None:
        path = path.strip().strip('"')
        if path and path != "/dev/null" and path not in files:
            files.append(path)

    old = ""
    for line in diff.splitlines():
        if line.startswith("--- "):
            old = re.sub(r'^"?a/', "", line[4:].strip()).rstrip('"')
        elif line.startswith("+++ "):
            new = line[4:].strip()
            add(old if new == "/dev/null" else re.sub(r'^"?b/', "", new).rstrip('"'))
        elif line.startswith("rename to "):
            add(line[len("rename to "):])
        elif line.startswith("Binary files "):
            m = re.match(r'Binary files (?:/dev/null|"?a/.+?"?) and "?b/(.+?)"? differ$', line)
            if m:
                add(m.group(1))
    return files


def _issue_record(ref: dict, config: dict) -> dict:
    cfg = dict(config)
    if ref["source"] == "github" and ref.get("repo"):
        cfg["GH_ORG"], _, cfg["GH_REPO"] = ref["repo"].partition("/")
    details = fetch_issue_details(ref, cfg)
    rec: dict = {"key": ref["key"], "source": ref["source"], "url": ref.get("url", "")}
    if details is None:
        return {**rec, "fetch_error": "not found or credentials missing", "acceptance_criteria": [],
                "has_acceptance_criteria": None}
    if details.get("access_denied"):
        return {**rec, "access_denied": True, "acceptance_criteria": [], "has_acceptance_criteria": None}
    body = details.get("body", "")
    if not isinstance(body, str):  # ADF document from a v3 endpoint
        from scripts.common.jira_api import extract_adf_text
        body = extract_adf_text(body)
    ac, hints = extract_ac_items(body)
    return {**rec, "title": details.get("title", ""), "body": body, "labels": details.get("labels", []),
            "has_acceptance_criteria": bool(ac), "acceptance_criteria": ac, "test_hints": hints,
            "design_doc_links": details.get("design_doc_links", [])}


def _issue_only_context(ref: str, config: dict) -> dict | None:
    """Context for a bare issue key / issue URL (no PR)."""
    m = _ATLASSIAN_BROWSE.search(ref)
    key = m.group(1) if m else (ref.strip() if re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", ref.strip()) else "")
    if key:
        base = (config.get("JIRA_BASE_URL") or "").rstrip("/")
        issue_ref = {"key": key, "source": "jira", "url": f"{base}/browse/{key}" if base else ""}
    else:
        g = _GH_ISSUE_URL.search(ref)
        if not g:
            return None
        issue_ref = {"key": f"#{g.group(2)}", "source": "github", "repo": g.group(1), "url": ref}
    issue = _issue_record(issue_ref, config)
    return {"kind": "issue", "tracker": "jira" if issue_ref["source"] == "jira" else "github", "pr": None,
            "issues": [issue], "ac_source": f"issue:{key or issue_ref['key']}" if issue["acceptance_criteria"] else "none"}


def _default_repo_url(config: dict) -> str:
    """Clone URL of the configured repository (for a bare PR number), from the default tracker."""
    if (config.get("DEFAULT_TRACKER") or "github").lower() in ("jira", "bitbucket", "jira/bitbucket"):
        ws, repo = config.get("BITBUCKET_WORKSPACE", ""), config.get("BITBUCKET_REPO", "")
        if not (ws and repo):
            raise ValueError("a bare PR number needs BITBUCKET_WORKSPACE and BITBUCKET_REPO (or pass the PR URL)")
        return f"https://bitbucket.org/{ws}/{repo}.git"
    org, repo = config.get("GH_ORG", ""), config.get("GH_REPO", "")
    if not (org and repo):
        raise ValueError("a bare PR number needs GH_ORG and GH_REPO (or pass the PR URL)")
    return f"https://github.com/{org}/{repo}.git"


def build_context(ref: str, config: dict, diff_dir: Path | None = None,
                  max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> dict:
    """Build the normalized context. Raises ValueError with an actionable message when unusable."""
    ref = _strip_slack_url(ref.strip())
    number, clone_url = parse_pr_ref(ref)
    if ref.isdigit():  # a bare PR number refers to the configured repository
        number, clone_url = ref, _default_repo_url(config)
    if not clone_url or not number:
        issue_ctx = _issue_only_context(ref, config)
        if issue_ctx:
            return issue_ctx
        raise ValueError(f"cannot interpret {ref!r} as a GitHub/Bitbucket PR URL or an issue key/URL")
    config = dict(config)
    config["DEFAULT_TRACKER"] = ""  # let the URL decide, not the org default
    apply_repo_override(config, clone_url)
    tracker = "bitbucket" if "bitbucket.org" in clone_url else "github"
    missing = missing_credentials(tracker, config)
    if missing:
        raise ValueError(f"missing credentials: {', '.join(missing)}")

    prs = fetch_prs_by_numbers(config, [int(number)])
    if not prs:
        raise ValueError(f"PR {number} not found or not accessible on {tracker} (check token scope and repo)")
    pr = prs[0]

    diff, diff_err = fetch_pr_diff(config, tracker, number, max_diff_bytes)
    raw = diff.encode("utf-8", "replace")
    truncated = len(raw) > max_diff_bytes
    if truncated:
        diff = raw[:max_diff_bytes].decode("utf-8", "ignore")
    diff_path = ""
    if diff and diff_dir:
        diff_dir.mkdir(parents=True, exist_ok=True)
        (diff_dir / "pr.diff").write_text(diff, encoding="utf-8")
        diff_path = str(diff_dir / "pr.diff")
    files = changed_files_from_diff(diff) or list(pr.get("file_paths", []))

    ignored: list[str] = []
    issues = [_issue_record(r, config) for r in collect_issue_refs(pr, config, tracker, ignored)]
    pr_items, pr_hints = extract_ac_items(pr.get("body", "") or "")
    sources = [f"issue:{i['key']}" for i in issues if i.get("acceptance_criteria")]
    if pr_items:
        sources.append("pr_description")

    return {
        "kind": "pr", "tracker": tracker,
        "pr": {"number": pr.get("number"), "title": pr.get("title", ""), "body": pr.get("body", ""),
               "url": pr.get("url", ref), "source_branch": pr.get("headRefName", ""),
               "destination_branch": pr.get("baseRefName", ""), "state": pr.get("state", ""),
               "author": pr.get("author", ""), "repo_url": clone_url},
        "changed_files": files, "diff_path": diff_path, "diff_truncated": truncated,
        "diff_error": diff_err, "pr_acceptance_criteria": pr_items, "pr_test_hints": pr_hints, "issues": issues, "ignored_refs": ignored,
        "ac_source": ",".join(sources) if sources else "none",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ref")
    parser.add_argument("--out", default="")
    parser.add_argument("--diff-dir", default="")
    parser.add_argument("--max-diff-bytes", type=int, default=DEFAULT_MAX_DIFF_BYTES)
    args = parser.parse_args(argv)
    try:
        ctx = build_context(args.ref, load_config(), Path(args.diff_dir) if args.diff_dir else None, args.max_diff_bytes)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    text = json.dumps(ctx, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[pr-context] wrote {args.out} (tracker={ctx['tracker']} issues={len(ctx['issues'])} ac_source={ctx['ac_source']})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
