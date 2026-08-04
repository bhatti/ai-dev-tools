"""Slack Bolt Socket Mode router for Formicary workflows.

Listens for Slack mentions and thread replies, resolves the user's intent via
the Registry, and submits / resumes Formicary jobs accordingly.

Required environment variables:
    SLACK_APP_TOKEN   xapp-... Socket Mode app-level token (connections:write scope)
    SLACK_BOT_TOKEN   xoxb-... bot OAuth token (see scopes below)
    FORMICARY_URL     default http://localhost:7777
    FORMICARY_TOKEN   Formicary bearer token
    DEFAULT_TRACKER   "jira" or "github" — used when message has no URL/issue key
                      (e.g. bare "standup" picks ai-standup-jira vs ai-standup-gh)
                      default: "jira"

Optional:
    SLACK_BOT_NAME                  Display name used in @bot help examples
                                    (default: "@bot" — set to your bot's actual name)
    ANTHROPIC_DEFAULT_HAIKU_MODEL   Claude model used for classify_intent
                                    (default: claude-haiku-4-5-20251001-v1:0)

Required Slack app configuration (api.slack.com/apps):

    App-level token (xapp-...)  — scope: connections:write
        Settings → Socket Mode → Enable Socket Mode
        → App-Level Tokens → Generate token → add connections:write scope

    Bot token (xoxb-...)  — OAuth & Permissions → Bot Token Scopes:
        app_mentions:read   — receive @mentions
        channels:history    — read channel messages (for thread replies)
        channels:read       — resolve channel info
        groups:history      — same for private channels
        groups:read         — same for private channels
        chat:write          — post messages and thread replies
        users:read          — look up user display names (optional, nice to have)

    Event Subscriptions → Subscribe to bot events:
        app_mention         — fired on @bot mentions
        message.channels    — fired on public channel messages (thread replies)
        message.groups      — fired on private channel messages (required for private channels)

    After any scope or event change: Install App → Reinstall to Workspace

Run:
    python -m scripts.slack.router
    python scripts/slack/router.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from scripts.slack.formicary_client import FormicaryClient
from scripts.slack.registry import Registry

# ---------------------------------------------------------------------------
# Slack mrkdwn normalisation
# ---------------------------------------------------------------------------

_SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]*)>")
_SLACK_URL_RE = re.compile(r"<(https?://[^>]+)>")
_JIRA_BROWSE_RE = re.compile(r"https?://[^/]+/browse/([A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE)


def _normalize_slack_text(text: str) -> str:
    """Convert Slack mrkdwn links to plain text that the router can parse.

    Slack auto-links Jira keys like CRIBL-40452 into:
      <https://company.atlassian.net/browse/CRIBL-40452|Consider how...>
    We want to extract just the Jira key (CRIBL-40452) from the URL, not the
    display text which may be the issue title instead of the key.
    For non-Jira URLs we keep the raw URL so PR URLs still route correctly.
    """
    def replace_link(m: re.Match) -> str:
        url = m.group(1)
        # If it's a Jira browse URL, extract the issue key
        jira_match = _JIRA_BROWSE_RE.search(url)
        if jira_match:
            return jira_match.group(1)
        # Otherwise keep the URL (e.g. GitHub PR URLs)
        return url

    text = _SLACK_LINK_RE.sub(replace_link, text)
    # Also handle bare <url> without display text
    text = _SLACK_URL_RE.sub(lambda m: m.group(1), text)
    return text


# ---------------------------------------------------------------------------
# Globals initialised at import time (before main())
# ---------------------------------------------------------------------------

_registry: Registry | None = None
_formicary: FormicaryClient | None = None


def _get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry.from_default()
    return _registry


def _get_client() -> FormicaryClient:
    global _formicary
    if _formicary is None:
        _formicary = FormicaryClient.from_env()
    return _formicary


# ---------------------------------------------------------------------------
# Intent classification via Claude CLI
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT_TEMPLATE = """You are a routing assistant. Extract structured fields from the user message.

Message: {text}

Reply with ONLY valid JSON — no markdown fences, no explanation:
{{
  "intent": "<one of: review, implement, standup, risk scan, security review, sre review, pr queue, pr feedback, unknown>",
  "target_kind": "<one of: github, jira, any>",
  "entity_id": "<PR URL, issue key, or empty string>"
}}"""

_HAIKU_DEFAULT = "claude-haiku-4-5-20251001-v1:0"


def classify_intent(text: str, config: dict[str, str]) -> tuple[str, str, str]:
    """Call Claude Haiku via CLI to extract intent/target/entity from free text.

    Returns (intent, target_kind, entity_id).
    Falls back to ("unknown", "any", "") on any error.
    """
    model = config.get(
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", _HAIKU_DEFAULT),
    )
    prompt = _CLASSIFY_PROMPT_TEMPLATE.format(text=text)
    try:
        result = subprocess.run(
            ["claude", "--print", "--model", model, "--max-turns", "1", prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout.strip()
        # Claude may wrap in markdown fences — strip them
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        intent = data.get("intent", "unknown") or "unknown"
        target_kind = data.get("target_kind", "any") or "any"
        entity_id = data.get("entity_id", "") or ""
        return (intent, target_kind, entity_id)
    except Exception as exc:
        print(f"[router] classify_intent error: {exc}", file=sys.stderr, flush=True)
        return ("unknown", "any", "")


# ---------------------------------------------------------------------------
# Core routing logic
# ---------------------------------------------------------------------------

def _build_params(
    entry_id_var: str,
    entity_id: str,
    skill: str,
    channel: str,
    thread_ts: str,
    prompt: str = "",
    user_text: str = "",
    default_tracker: str = "",
) -> dict[str, str]:
    params: dict[str, str] = {
        "SlackChannel": channel,
        "SlackThreadTs": thread_ts,
    }
    if entry_id_var and entity_id:
        params[entry_id_var] = entity_id
    if skill:
        params["Skill"] = skill
    # Pass DEFAULT_TRACKER for non-cron adhoc jobs (pr-queue, risk-scan, etc.) so the
    # container's gather scripts know which tracker to use without guessing from env vars.
    # Standup cron jobs derive their tracker from the job definition itself, not this param.
    if default_tracker and default_tracker != "any" and skill:
        params["DefaultTracker"] = default_tracker
    # Canned prompt from registry takes priority; fall back to user's message text.
    # Interpolate {PRUrl} and {entity_id} placeholders in canned prompts.
    resolved_prompt = prompt or user_text or ""
    if resolved_prompt and entity_id:
        resolved_prompt = resolved_prompt.replace("{PRUrl}", entity_id)
        resolved_prompt = resolved_prompt.replace("{entity_id}", entity_id)
    params["Prompt"] = resolved_prompt
    return params


def handle_new_request(
    text: str,
    channel: str,
    ts: str,
    say: Any,
    config: dict[str, str],
) -> None:
    """Resolve intent from text, submit a Formicary job, and reply."""
    registry = _get_registry()
    client = _get_client()

    # 1. Try structured verb parse (cheap, no LLM)
    words = text.split()
    result: tuple[str, str, str] | None = None
    if words:
        verb = words[0].lstrip("/")
        rest = " ".join(words[1:])
        result = registry.parse_verb(verb, rest)

    # 2. Fall back to LLM classification
    if result is None:
        result = classify_intent(text, config)

    intent, target_kind, entity_id = result

    # 3a. Help command — short-circuit before workflow resolution
    if intent == "__help__":
        bot_name = config.get("SLACK_BOT_NAME", os.environ.get("SLACK_BOT_NAME", "@bot"))
        say(text=registry.help_message(bot_name=bot_name), thread_ts=ts)
        return

    # 3b. Resolve workflow entry — use DEFAULT_TRACKER to break ties when target is ambiguous
    default_tracker = config.get("DEFAULT_TRACKER", os.environ.get("DEFAULT_TRACKER", "any")).lower()
    entry = registry.resolve(intent, target_kind, default_tracker=default_tracker)
    if entry is None:
        say(
            text=(
                "I don't have a workflow for that yet.\n"
                "Type `@bot help` to see available commands and how to add new skills."
            ),
            thread_ts=ts,
        )
        return

    # 4. Check required vars
    missing = registry.missing_required_vars(entry, entity_id)
    if missing:
        say(
            text=f"To run `{entry.name}` I need: {', '.join(missing)}. Please include them in your message.",
            thread_ts=ts,
        )
        return

    # 5. Submit (or trigger for cron) the job.
    params = _build_params(entry.id_var, entity_id, entry.skill, channel, ts,
                           prompt=entry.prompt, user_text=text,
                           default_tracker=default_tracker)
    if entry.cron:
        # Cron jobs always have a PENDING request — trigger it with the current params
        # (SlackChannel, SlackThreadTs) so the job runs with the right Slack context.
        job = client.trigger_pending_or_submit(entry.job_type, params)
    else:
        user_key = f"{entry.job_type}:{ts}" if ts else ""
        job = client.submit(entry.job_type, params, user_key=user_key)
    if not job:
        say(
            text=f"Failed to submit `{entry.name}` — check Formicary connectivity.",
            thread_ts=ts,
        )
        return
    if job.get("_already_executing"):
        job_id = job.get("id", "unknown")
        public_url = (
            config.get("FORMICARY_PUBLIC_URL")
            or os.environ.get("FORMICARY_PUBLIC_URL")
            or config.get("FORMICARY_URL", os.environ.get("FORMICARY_URL", ""))
        ).rstrip("/")
        job_url = f"{public_url}/dashboard/jobs/requests/{job_id}" if public_url else ""
        link = f"  <{job_url}|View job>" if job_url else ""
        say(
            text=f"`{entry.name}` is already running — results will be posted here when it finishes.{link}",
            thread_ts=ts,
        )
        return
    if job.get("_no_cron_slot"):
        say(
            text=(
                f"`{entry.name}` has no scheduled slot right now. "
                "Go to Formicary → Job Definitions → disable then re-enable "
                f"`{entry.job_type}` to restore the cron schedule, then try again."
            ),
            thread_ts=ts,
        )
        return

    job_id = job.get("id", "unknown")
    # Use FORMICARY_PUBLIC_URL for clickable links (may differ from internal FORMICARY_URL)
    public_url = (
        config.get("FORMICARY_PUBLIC_URL")
        or os.environ.get("FORMICARY_PUBLIC_URL")
        or config.get("FORMICARY_URL", os.environ.get("FORMICARY_URL", ""))
    ).rstrip("/")
    job_url = f"{public_url}/dashboard/jobs/requests/{job_id}" if public_url else ""
    link = f"  <{job_url}|View job>" if job_url else ""
    say(
        text=f"Started `{entry.name}` (job `{job_id}`) — I'll post updates here.{link}",
        thread_ts=ts,
    )


def handle_thread_reply(
    event: dict,
    say: Any,
    config: dict[str, str],
) -> None:
    """Resume a PAUSED job waiting in this thread, or treat as new request."""
    registry = _get_registry()  # noqa: F841 (used below if falling through)
    client = _get_client()
    thread_ts: str = event.get("thread_ts", "")
    text: str = event.get("text", "")
    channel: str = event.get("channel", "")
    ts: str = event.get("ts", thread_ts)

    jobs = client.find_jobs(state="PAUSED", var_filter={"SlackThreadTs": thread_ts})
    if jobs:
        job = jobs[0]
        job_id: str = job.get("id", "")
        ok = client.resume(job_id, variables={"ReplyText": text})
        if ok:
            say(text="Got it, resuming.", thread_ts=thread_ts)
        else:
            say(text="Couldn't resume the paused job — check Formicary.", thread_ts=thread_ts)
        return

    # No paused job — treat as a new request in the thread context
    handle_new_request(text, channel, ts, say, config)


# ---------------------------------------------------------------------------
# Bolt app wiring
# ---------------------------------------------------------------------------

def build_app(config: dict[str, str] | None = None) -> App:
    """Construct and wire the Bolt app.  Separated for testability."""
    if config is None:
        config = dict(os.environ)
        # Merge org configs from Formicary (camelCase keys converted to UPPER_SNAKE_CASE).
        # Env vars take precedence — org configs only fill in what's missing.
        try:
            client = _get_client()
            org_config = client.get_org_configs()
            for k, v in org_config.items():
                if not config.get(k):
                    config[k] = v
        except Exception as exc:
            print(f"[router] could not load org configs: {exc}", file=sys.stderr, flush=True)

    app = App(token=config.get("SLACK_BOT_TOKEN", os.environ.get("SLACK_BOT_TOKEN", "")))

    @app.event("app_mention")
    def on_app_mention(event: dict, say: Any) -> None:  # type: ignore[misc]
        _dispatch(event, say, config)

    @app.event("message")
    def on_message(event: dict, say: Any) -> None:  # type: ignore[misc]
        # Ignore bot messages and message_changed subtypes
        if event.get("bot_id") or event.get("subtype"):
            return
        _dispatch(event, say, config)

    @app.action("review_decision")
    def on_block_action(ack: Any, action: dict, respond: Any) -> None:  # type: ignore[misc]
        ack()
        try:
            # Bolt passes the individual action dict, not the full payload
            value: str = action.get("value", "")
            if ":" not in value:
                return
            job_id, decision = value.split(":", 1)
            client = _get_client()
            ok = client.resume(job_id, variables={"Decision": decision})
            if ok:
                respond(text=f"Decision recorded: {decision}")
            else:
                respond(text=f"Failed to record decision for job {job_id}.")
        except Exception as exc:
            print(f"[router] on_block_action error: {exc}", file=sys.stderr, flush=True)
            respond(text="Something went wrong processing your action.")

    return app


def _dispatch(event: dict, say: Any, config: dict[str, str]) -> None:
    """Route event to thread-reply handler or new-request handler."""
    thread_ts: str | None = event.get("thread_ts")
    ts: str = event.get("ts", "")
    channel: str = event.get("channel", "")
    text: str = event.get("text", "")

    # Strip bot mention prefix (<@UXXXXX> ...)
    if text.startswith("<@"):
        idx = text.find(">")
        if idx != -1:
            text = text[idx + 1:].strip()

    # Normalise Slack mrkdwn links: <url|display> → url (or Jira key)
    text = _normalize_slack_text(text)

    if thread_ts and thread_ts != ts:
        # This is a reply inside an existing thread
        handle_thread_reply(
            {**event, "text": text},
            say,
            config,
        )
    else:
        handle_new_request(text, channel, ts, say, config)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the Bolt Socket Mode listener."""
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not app_token:
        print(
            "[router] SLACK_APP_TOKEN not set — cannot start Socket Mode",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    app = build_app()
    print("[router] Starting Slack router in Socket Mode …", flush=True)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
