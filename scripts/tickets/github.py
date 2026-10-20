"""GitHub side of the ticket tools (the `gh` CLI): list, read, normalize and write a generated section into the body."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from scripts.common.http_retry import Retryable, call_with_retry
from scripts.common.redact import redact
from scripts.common.shell import run_cmd
from scripts.tickets import sections, story_points
from scripts.tickets.model import TicketError, new_ticket, unreadable
from scripts.tickets.sections import Section

FIELDS = "number,title,url,labels,assignees,state,body,milestone,comments"
LIST_COMMENT_CAP = 100  # `gh issue list` returns at most this many comments per issue; `gh issue view` returns all of them
BODY_LIMIT = 65536      # GitHub's maximum issue body length
_TRANSIENT = re.compile(r"rate limit|HTTP 429|HTTP 50[234]|timed? ?out|temporarily unavailable", re.I)
_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)", re.I)
_IN_PROGRESS = re.compile(r"\b(?:in[ -]?progress|doing|wip)\b", re.I)
_POINTS_FIELD = re.compile(r"^(?:story[ -]?points?|points?|estimate)$", re.I)
GH_TIMEOUT = 120  # seconds; a hung gh must not block the job forever


def parse_ref(ref: str, default_repo: str = "") -> tuple[str, str]:
    """(owner/name, number) of ``https://github.com/o/r/issues/N``, ``o/r#N``, ``#N`` or ``N``; the number needs a repo."""
    m = _URL.search(ref)
    if m:
        return f"{m.group(1)}/{m.group(2)}", m.group(3)
    m = re.fullmatch(r"([^/\s#]+/[^/\s#]+)#(\d+)", ref.strip())
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r"#?(\d+)", ref.strip())
    if m and default_repo:
        return default_repo, m.group(1)
    raise TicketError(f"cannot interpret {ref!r} as a GitHub issue (use a URL, owner/repo#N, or set --repo / GH_ORG and GH_REPO)", "not_found")


def normalize(raw: dict, project_points: tuple[float | None, bool] = (None, True)) -> dict:
    labels = [l["name"] for l in raw.get("labels", [])]
    closed = raw.get("state", "").upper() == "CLOSED"
    category = "done" if closed else "in_progress" if any(_IN_PROGRESS.search(l) for l in labels) else "todo"
    return new_ticket(
        tracker="github", id=f"#{raw['number']}", url=raw.get("url", ""), title=raw.get("title", ""), description=(raw.get("body") or "").strip(),
        status="closed" if closed else "open", status_category=category, assignee=", ".join(a["login"] for a in raw.get("assignees", [])) or None,
        labels=labels, sprint=(raw.get("milestone") or {}).get("title"),
        story_points=story_points.github_points(labels, *project_points), comments=[c.get("body", "") for c in raw.get("comments", [])])


class GitHubTickets:
    def __init__(self, repo: str, project_number: str = ""):
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo or ""):
            raise TicketError(f"a GitHub repository is required as owner/name, got {repo!r} (--repo, or GH_ORG and GH_REPO)")
        self.repo, self.project_number = repo, project_number

    def _gh(self, *args: str, once: bool = False) -> str:
        """Run gh; rate limits and transient server errors are retried (Retry-After is not available from the CLI, so backoff).
        ``once`` is for commands that must not run twice (posting a comment): only an explicit rate limit, which means the request was
        refused, is retried; a timeout or server error may have been applied."""
        def attempt() -> str:
            try:
                return run_cmd(["gh", *args], timeout=GH_TIMEOUT).stdout
            except subprocess.TimeoutExpired as e:
                if once:
                    raise TicketError(f"gh {' '.join(args[:2])} timed out; it may or may not have been applied") from e
                raise Retryable(f"gh {' '.join(args[:2])} timed out after {GH_TIMEOUT}s") from e
            except subprocess.CalledProcessError as e:
                detail = redact((e.stderr or e.stdout or "").strip()[:300])
                if _TRANSIENT.search(detail) and (not once or re.search(r"rate limit|HTTP 429", detail, re.I)):
                    raise Retryable(f"gh {' '.join(args[:2])}: {detail}") from e
                kind = "not_found" if "Could not resolve" in detail or "not found" in detail.lower() else "access" if re.search(r"HTTP 40[13]", detail) else "failed"
                raise TicketError(f"gh {' '.join(args[:2])} failed: {detail}", kind) from e
            except OSError as e:
                raise TicketError(f"cannot run gh: {e}") from e
        try:
            return call_with_retry(attempt)
        except Retryable as e:
            raise TicketError(str(e)) from e

    def _json(self, text: str, what: str):
        try:
            return json.loads(text or "null")
        except ValueError as e:
            raise TicketError(f"gh returned something that is not JSON for {what}") from e

    def _project_points(self, number: str) -> tuple[float | None, bool]:
        """(value, readable) of the configured Projects number field; (None, True) when the issue has no value there."""
        if not self.project_number:
            return None, True
        owner, name = self.repo.split("/")
        query = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){projectItems(first:50){pageInfo{hasNextPage} nodes{project{number} "
                 "fieldValues(first:50){pageInfo{hasNextPage} nodes{... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}}}}}}}}}")
        try:
            out = self._gh("api", "graphql", "-f", f"query={query}", "-F", f"o={owner}", "-F", f"r={name}", "-F", f"n={number}")
            items = self._json(out, "project items")["data"]["repository"]["issue"]["projectItems"]
        except (TicketError, KeyError, TypeError):
            return None, False
        if (items.get("pageInfo") or {}).get("hasNextPage"):
            return None, False  # more items than one page: not knowing is honest, "unset" would be a guess
        try:
            for item in items["nodes"]:
                if str((item.get("project") or {}).get("number")) != str(self.project_number):
                    continue
                if (item["fieldValues"].get("pageInfo") or {}).get("hasNextPage"):
                    return None, False
                for v in item["fieldValues"]["nodes"]:
                    if v and _POINTS_FIELD.match((v.get("field") or {}).get("name", "")):
                        return float(v["number"]), True
        except (KeyError, TypeError, ValueError, AttributeError):
            return None, False  # an unexpected response shape is "could not read", not "unset"
        return None, True

    def _normalize(self, raw: dict) -> dict:
        return normalize(raw, self._project_points(str(raw["number"])))

    def search(self, *, label: str = "", milestone: str = "", include_done: bool = False, limit: int = 200) -> list[dict]:
        args = ["issue", "list", "--repo", self.repo, "--state", "all" if include_done else "open", "--limit", str(limit), "--json", FIELDS]
        if label:
            args += ["--label", label]
        if milestone:
            args += ["--milestone", milestone]
        raws = self._json(self._gh(*args), "the issue list") or []
        return [self._normalize(self._complete(r)) for r in raws]

    def _complete(self, raw: dict) -> dict:
        """The list call caps comments per issue; an issue at the cap is read again with the call that returns them all."""
        if len(raw.get("comments", [])) < LIST_COMMENT_CAP:
            return raw
        return self._json(self._gh("issue", "view", str(raw["number"]), "--repo", self.repo, "--json", FIELDS), f"issue #{raw['number']}")

    def get(self, number: str) -> dict:
        try:
            return self._normalize(self._json(self._gh("issue", "view", number, "--repo", self.repo, "--json", FIELDS), f"issue #{number}"))
        except TicketError as e:
            return unreadable("github", f"#{number}", str(e), e.kind == "access")

    # -- writing ----------------------------------------------------------------------------------------------
    def body(self, number: str) -> str:
        return (self._json(self._gh("issue", "view", number, "--repo", self.repo, "--json", "body"), f"issue #{number}") or {}).get("body") or ""

    def read_text(self, number: str) -> str:
        return self.body(number)

    def has_section(self, number: str, section: Section) -> bool:
        return sections.has_marked(self.body(number), section)

    def read_parts(self, number: str, section: Section) -> tuple[str, str | None]:
        return sections.split_marked(self.body(number), section)

    def write_section(self, number: str, section: Section, markdown: str, replace: bool) -> None:
        """Append or swap the marked block. The body is read again here and the write is refused if the ticket changed
        meanwhile in a way that makes ``replace`` wrong, or if the new body would exceed GitHub's limit."""
        body = self.body(number)
        if sections.has_marked(body, section) != replace:
            raise TicketError(f"#{number} changed while it was being written (the section {'disappeared' if replace else 'appeared'}); nothing was written")
        new = sections.replace_marked(body, section, markdown) if replace else sections.append_marked(body, section, markdown)
        if len(new) > BODY_LIMIT:
            raise TicketError(f"the new body of #{number} would be {len(new)} characters; GitHub allows {BODY_LIMIT}. Nothing was written")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(new, encoding="utf-8")
            self._gh("issue", "edit", number, "--repo", self.repo, "--body-file", str(path))

    def replaceable(self, number: str, section: Section) -> bool:
        return True  # a marked block is always ours

    def rendered(self, section: Section, markdown: str) -> str:
        return sections.wrap(section, markdown)

    def comment(self, number: str, text: str) -> None:
        self._gh("issue", "comment", number, "--repo", self.repo, "--body", text, once=True)
