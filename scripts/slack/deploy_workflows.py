"""Deploy workflow YAML definitions to Formicary.

Finds ai-*.yaml files in the formicary/docs/examples/ directory and upserts
each one via the Formicary REST API (reuses FormicaryClient.deploy_definition).

Also syncs key org configs (FormicaryPublicUrl, DefaultTracker, SlackChannel)
from environment variables so the Slack router always has the right values.

Usage:
    python -m scripts.slack.deploy_workflows [--dir PATH] [--file FILE] [--dry-run] [--list]
    python -m scripts.slack.deploy_workflows --set-configs  # push env vars as org configs only
    python -m scripts.slack.deploy_workflows --trigger ai-standup-jira  # run a cron job now

Environment:
    FORMICARY_URL         default http://localhost:7777
    FORMICARY_TOKEN       Formicary bearer token
    FORMICARY_PUBLIC_URL  public URL for clickable Slack job links (e.g. http://localhost:7777)
    DEFAULT_TRACKER       "jira" or "github" (default: jira)
    SLACK_CHANNEL         default Slack channel
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
import yaml

from scripts.slack.formicary_client import FormicaryClient

_DEFAULT_EXAMPLES_DIR = (
    Path(os.environ.get("FORMICARY_YAML_DIR", ""))
    if os.environ.get("FORMICARY_YAML_DIR")
    else Path(__file__).resolve().parents[3] / "formicary" / "docs" / "examples"
)
_DEFAULT_TIMEOUT = 20


def _validate(path: Path) -> str | None:
    """Return raw YAML content if valid, else None (prints error)."""
    try:
        content = path.read_text()
        data = yaml.safe_load(content)
        if not isinstance(data, dict) or "job_type" not in data:
            print(f"  ✗ {path.name}: not a valid workflow YAML (missing job_type)", file=sys.stderr)
            return None
        return content
    except yaml.YAMLError as e:
        print(f"  ✗ {path.name}: YAML parse error: {e}", file=sys.stderr)
        return None


def _push_org_config(base_url: str, token: str, org_id: str, name: str, value: str) -> bool:
    """Upsert a single org config key/value. Uses PUT if config already exists, POST otherwise."""
    import re
    secret = bool(re.search(r"(?i)(token|secret|key|password|api|credential|private)", name))
    payload = {
        "name": name,
        "value": value,
        "kind": "string",
        "secret": secret,
        "configurable_id": org_id,
        "configurable_type": "organizations",
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = base_url.rstrip("/")
    try:
        # Try POST first; if 409/400 (already exists) try PUT with the existing config's id
        resp = requests.post(
            f"{base}/api/v1/orgs/{org_id}/configs",
            json=payload,
            headers=headers,
            timeout=_DEFAULT_TIMEOUT,
        )
        if resp.ok:
            return True
        # Already exists — find it and PUT
        if resp.status_code in (400, 409) or "already" in resp.text.lower() or "exists" in resp.text.lower() or "duplicate" in resp.text.lower():
            list_resp = requests.get(
                f"{base}/api/v1/orgs/{org_id}/configs",
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
            )
            if list_resp.ok:
                items = list_resp.json()
                if not isinstance(items, list):
                    items = items.get("records") or items.get("Records") or []
                existing = next((c for c in items if c.get("name") == name), None)
                if existing:
                    put_payload = dict(existing)
                    put_payload["value"] = value
                    put_resp = requests.put(
                        f"{base}/api/v1/orgs/{org_id}/configs/{existing['id']}",
                        json=put_payload,
                        headers=headers,
                        timeout=_DEFAULT_TIMEOUT,
                    )
                    if put_resp.ok:
                        return True
                    print(f"  ✗ config {name} PUT: {put_resp.status_code}: {put_resp.text[:80]}", file=sys.stderr)
                    return False
        print(f"  ✗ config {name}: {resp.status_code}: {resp.text[:80]}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"  ✗ config {name}: {exc}", file=sys.stderr)
        return False


def _resolve_org_id(token: str) -> str:
    import base64
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
        return payload.get("org_id", "")
    except Exception:
        return ""


def sync_configs(base_url: str, token: str, dry_run: bool = False) -> None:
    """Push env-sourced org configs to Formicary."""
    org_id = _resolve_org_id(token)
    if not org_id:
        print("  ✗ could not resolve org_id from token", file=sys.stderr)
        return

    # Derive FORMICARY_PUBLIC_URL from FORMICARY_URL if not explicitly set.
    # The deploy script runs against localhost so FORMICARY_URL is already the right public address.
    public_url = os.environ.get("FORMICARY_PUBLIC_URL") or os.environ.get("FORMICARY_URL", "")

    # Map: value → Formicary org config name (only set if value is non-empty)
    mappings = [
        (public_url,                         "FormicaryPublicUrl"),
        (os.environ.get("FORMICARY_URL",""), "FormicaryUrl"),
        (os.environ.get("DEFAULT_TRACKER",""),"DefaultTracker"),
        (os.environ.get("SLACK_CHANNEL",""), "SlackChannel"),
    ]
    for value, config_name in mappings:
        if not value:
            continue
        if dry_run:
            print(f"  [dry-run] would set {config_name}={value}")
            continue
        if _push_org_config(base_url, token, org_id, config_name, value):
            print(f"  ✓ config {config_name}={value}")


def _trigger_now(client: FormicaryClient, base_url: str, token: str, job_type: str) -> None:
    """Trigger a pending cron job to run immediately via POST /api/v1/jobs/requests/:id/trigger.

    Cron jobs always have a PENDING request already scheduled — find and trigger it.
    Only submits a new request if no PENDING exists (non-cron jobs).
    """
    jobs = client.find_jobs(state="WAITING", job_type=job_type)
    if jobs:
        job_id = jobs[0].get("id")
        print(f"Found PENDING request {job_id} for {job_type}")
    else:
        print(f"No PENDING request for {job_type}, submitting ...")
        job = client.submit(job_type, {})
        if not job or not job.get("id"):
            print(f"✗ submit failed for {job_type}", file=sys.stderr)
            sys.exit(1)
        job_id = job["id"]

    resp = requests.post(
        f"{base_url.rstrip('/')}/api/v1/jobs/requests/{job_id}/trigger",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.ok:
        print(f"✓ triggered {job_type} (job {job_id}) — running now")
        print(f"  {base_url.rstrip('/')}/dashboard/jobs/requests/{job_id}")
    else:
        print(f"✗ trigger failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy workflow YAMLs to Formicary")
    parser.add_argument("--dir", default=str(_DEFAULT_EXAMPLES_DIR),
                        help="Directory containing ai-*.yaml files")
    parser.add_argument("--file", help="Deploy a single specific YAML file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate YAML only — do not deploy")
    parser.add_argument("--list", action="store_true",
                        help="List registered job definitions and exit")
    parser.add_argument("--set-configs", action="store_true",
                        help="Push env vars as org configs (FORMICARY_PUBLIC_URL, DEFAULT_TRACKER, SLACK_CHANNEL)")
    parser.add_argument("--trigger", metavar="JOB_TYPE",
                        help="Trigger a pending cron job to run immediately (e.g. ai-standup-jira)")
    parser.add_argument("--server", default=os.environ.get("FORMICARY_URL", "http://localhost:7777"),
                        help="Formicary base URL (env: FORMICARY_URL)")
    args = parser.parse_args()

    token = os.environ.get("FORMICARY_TOKEN", "")
    if not token and not args.dry_run and not args.list:
        print("✗ FORMICARY_TOKEN not set — export it before running", file=sys.stderr)
        sys.exit(1)

    client = FormicaryClient(base_url=args.server, token=token)

    if args.trigger:
        _trigger_now(client, args.server, token, args.trigger)
        return

    if args.list:
        defs = client.list_definitions()
        if not defs:
            print("No job definitions found.")
            return
        for d in sorted(defs, key=lambda x: x.get("job_type", "")):
            print(f"  {d.get('job_type', '?')}")
        return

    # Always sync configs before deploying so the router has the right values
    print(f"Syncing org configs to {args.server} ...")
    sync_configs(args.server, token, dry_run=args.dry_run)

    if args.set_configs and not args.file and not args.dir:
        return

    if args.file:
        files = [Path(args.file)]
    else:
        d = Path(args.dir)
        if not d.is_dir():
            print(f"✗ Directory not found: {d}", file=sys.stderr)
            sys.exit(1)
        files = sorted(f for f in d.glob("ai-*.yaml") if "k8s" not in f.name)
        if not files:
            print(f"No ai-*.yaml files found in {d}", file=sys.stderr)
            sys.exit(1)

    print(f"\nDeploying {len(files)} workflow(s) to {args.server} ...")
    ok = 0
    for f in files:
        content = _validate(f)
        if content is None:
            continue
        job_type = yaml.safe_load(content).get("job_type", f.name)
        if args.dry_run:
            print(f"  [dry-run] {job_type} ({f.name}) — valid")
            ok += 1
            continue
        if client.deploy_definition(content):
            print(f"  ✓ {job_type} ({f.name})")
            ok += 1
        else:
            print(f"  ✗ {job_type} ({f.name}) — failed (see above)", file=sys.stderr)

    failed = len(files) - ok
    print(f"\n{ok}/{len(files)} deployed" + (f", {failed} failed" if failed else "") + ".")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
