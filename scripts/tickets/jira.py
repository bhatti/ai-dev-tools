"""Jira side of the ticket tools: search, read, normalize and write a generated section into the description."""

from __future__ import annotations

import re

from scripts.common import adf, jira_api
from scripts.common.jira_api import JiraError
from scripts.tickets import story_points
from scripts.tickets.model import TicketError, new_ticket, unreadable
from scripts.tickets.sections import Section

_FIELDS = ["summary", "description", "status", "issuetype", "assignee", "labels", "parent", "issuelinks", "comment", "environment"]
_KEY = re.compile(r"[A-Z][A-Z0-9_]+-\d+")
_CATEGORY = {"new": "todo", "indeterminate": "in_progress", "done": "done"}


def jql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_jql(*, sprint: str = "", label: str = "", project: str = "", jql: str = "", include_done: bool = False) -> str:
    """JQL for the requested scope; an explicit ``jql`` is used as is."""
    if jql:
        return jql
    parts = [f"sprint = {jql_quote(sprint)}"] if sprint else []
    if label:
        parts.append(f"labels = {jql_quote(label)}")
    if project:
        parts.append(f"project = {jql_quote(project)}")
    if not parts:
        raise ValueError("a Jira scope needs --sprint, --label or --jql")
    if not include_done:
        parts.append("statusCategory != Done")
    return " AND ".join(parts) + " ORDER BY rank ASC"


def key_of(ref: str) -> str | None:
    m = _KEY.search(ref)
    return m.group(0) if m else None


def _blockers(links: list[dict]) -> list[str]:
    out = []
    for link in links or []:
        other = link.get("inwardIssue")
        if other and "blocked by" in (link.get("type") or {}).get("inward", "").lower():
            category = ((other.get("fields") or {}).get("status") or {}).get("statusCategory", {}).get("key", "")
            if category != "done":
                out.append(other.get("key", ""))
    return [k for k in out if k]


def normalize(issue: dict, base_url: str, point_fields: list[tuple[str, str]] | None) -> dict:
    f = issue.get("fields") or {}
    status = f.get("status") or {}
    parent = f.get("parent")
    return new_ticket(
        tracker="jira", id=issue["key"], url=f"{base_url.rstrip('/')}/browse/{issue['key']}", title=f.get("summary") or "",
        description=adf.to_text(f.get("description")).strip(), status=status.get("name", ""),
        status_category=_CATEGORY.get((status.get("statusCategory") or {}).get("key", ""), "todo"),
        issue_type=(f.get("issuetype") or {}).get("name", "issue"), assignee=(f.get("assignee") or {}).get("displayName"),
        labels=f.get("labels") or [], is_subtask=bool((f.get("issuetype") or {}).get("subtask")), blocked_by=_blockers(f.get("issuelinks")),
        parent={"id": parent["key"], "title": (parent.get("fields") or {}).get("summary", "")} if parent else None,
        story_points=story_points.jira_points(f, point_fields), environment=adf.to_text(f.get("environment")).strip(),
        comments=[adf.to_text(c.get("body")).strip() for c in (f.get("comment") or {}).get("comments", [])])


class JiraTickets:
    def __init__(self, config: dict):
        self.config = config
        self._points: list[tuple[str, str]] | None | bool = False  # False: not looked up yet
        self._parents: dict[str, str] = {}

    def point_fields(self) -> list[tuple[str, str]] | None:
        if self._points is False:
            try:
                self._points = story_points.jira_point_fields(self.config)
            except JiraError:
                self._points = None
        return self._points

    def _fields(self) -> list[str]:
        return _FIELDS + [fid for fid, _ in (self.point_fields() or [])]

    def _normalize(self, issue: dict) -> dict:
        comments = (issue.get("fields") or {}).get("comment") or {}
        if comments.get("total", 0) > len(comments.get("comments", [])):  # the payload embeds only a first page of comments
            issue["fields"]["comment"]["comments"] = self._all_comments(issue["key"])
        ticket = normalize(issue, self.config["JIRA_BASE_URL"], self.point_fields())
        if ticket["is_subtask"] and ticket["parent"]:
            ticket["parent"]["description"], ticket["parent"]["error"] = self._parent_description(ticket["parent"]["id"])
        return ticket

    def _all_comments(self, key: str) -> list[dict]:
        try:
            return jira_api.list_comments(self.config, key)
        except JiraError as e:
            raise TicketError(f"the comments of {key} could not be read completely: {e}", "failed") from e

    def _parent_description(self, key: str) -> tuple[str, str]:
        """(the parent's complete description, error text): sub-tasks inherit their Why from it; read once per parent, a failure is not cached."""
        if key not in self._parents:
            try:
                self._parents[key] = adf.to_text(jira_api.fetch_issue(self.config, key, ["description"])["fields"].get("description")).strip()
            except JiraError as e:
                return "", f"the parent {key} could not be read: {e}"
        return self._parents[key], ""

    def _safely(self, issue: dict) -> dict:
        """One issue normalized; a ticket that cannot be read completely or has an unexpected structure is kept as UNREADABLE
        instead of failing the whole batch."""
        try:
            return self._normalize(issue)
        except TicketError as e:
            return unreadable("jira", issue.get("key", "?"), str(e))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            return unreadable("jira", issue.get("key", "?"), f"unexpected ticket structure ({type(e).__name__}: {e})")

    def search(self, jql: str, limit: int) -> list[dict]:
        try:
            issues = jira_api.search_issues_all(self.config, jql, self._fields(), limit)
        except JiraError as e:
            raise TicketError(f"Jira search failed: {e}", "access" if e.status in (401, 403) else "failed") from e
        return [self._safely(i) for i in issues]

    def get(self, ref: str) -> dict:
        key = key_of(ref)
        if not key:
            return unreadable("jira", ref, "not a Jira issue key or URL")
        try:
            issue = jira_api.fetch_issue(self.config, key, self._fields())
        except JiraError as e:
            denied = e.status in (401, 403)
            return unreadable("jira", key, "access denied" if denied else "not found or not visible" if e.status == 404 else str(e), denied)
        return self._safely(issue)

    # -- writing ----------------------------------------------------------------------------------------------
    def read_document(self, ref: str) -> dict | None:
        try:
            return jira_api.fetch_issue(self.config, key_of(ref) or ref, ["description"])["fields"].get("description")
        except JiraError as e:
            raise TicketError(f"cannot read {ref}: {e}", "not_found" if e.status == 404 else "failed") from e

    def read_text(self, ref: str) -> str:
        return adf.to_text(self.read_document(ref))

    def has_section(self, ref: str, section: Section) -> bool:
        return adf.has_section(self.read_document(ref), section.heading)

    def replaceable(self, ref: str, section: Section) -> bool:
        return adf.replaceable(self.read_document(ref), section.heading)

    def rendered(self, section: Section, markdown: str) -> str:
        """The text a reader sees for the section once written: what the readback is compared with."""
        return adf.split_text(adf.append_section(None, section.heading, adf.from_markdown(markdown)), section.heading)[1] or ""

    def read_parts(self, ref: str, section: Section) -> tuple[str, str | None]:
        """(text outside the section, the section's text or None)."""
        return adf.split_text(self.read_document(ref), section.heading)

    def write_section(self, ref: str, section: Section, markdown: str, replace: bool) -> None:
        """Add or swap the section panel. The document is read again here, and the write is refused if the ticket changed
        meanwhile in a way that makes ``replace`` wrong (someone else added the section, or removed it)."""
        key = key_of(ref) or ref
        doc = self.read_document(key)
        if adf.has_section(doc, section.heading) != replace:
            raise TicketError(f"{key} changed while it was being written (the section {'disappeared' if replace else 'appeared'}); nothing was written")
        nodes = adf.from_markdown(markdown)
        try:
            new = adf.replace_section(doc, section.heading, nodes) if replace else adf.append_section(doc, section.heading, nodes)
        except adf.NotReplaceable as e:
            raise TicketError(str(e)) from e
        try:
            jira_api.set_description(self.config, key, new)
        except JiraError as e:
            raise TicketError(f"cannot write {key}: {e}") from e

    def comment(self, ref: str, text: str) -> None:
        try:
            jira_api.add_comment_markdown(self.config, key_of(ref) or ref, text)
        except JiraError as e:
            raise TicketError(f"cannot comment on {ref}: {e}") from e
