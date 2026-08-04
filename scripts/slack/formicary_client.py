"""Thin wrapper over the Formicary REST API.

All endpoints use the /api/v1/ prefix (gRPC-gateway).

Formicary endpoints used:
    POST   /api/v1/jobs/requests               submit a new job
    GET    /api/v1/jobs/requests               list/query jobs
                                               params: job_state, job_type, q (free-text), pageSize
    GET    /api/v1/jobs/requests/{id}          get single job
    POST   /api/v1/jobs/requests/{id}/trigger  trigger a pending job (optional JSON body: {params})
    POST   /api/v1/jobs/definitions            create job definition
    PUT    /api/v1/jobs/definitions/{job_type} update job definition
    GET    /api/v1/jobs/definitions            list job definitions

Auth: Authorization: Bearer {token}
"""
from __future__ import annotations

import os
import sys
from typing import Any

import requests

_DEFAULT_TIMEOUT = 20


class FormicaryClient:
    """REST client for the Formicary workflow engine."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _log_error(self, label: str, resp: requests.Response) -> None:
        print(
            f"[formicary] {label} HTTP {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
            flush=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, job_type: str, params: dict[str, Any], user_key: str = "") -> dict:
        """Submit a new job.  Returns job dict (has ``id`` field) or ``{}`` on error.

        user_key, when set, is stored as the job's user_key (unique index in Formicary).
        Pass SlackThreadTs so each Slack thread maps to exactly one job — a second submit
        with the same user_key returns the existing job rather than creating a duplicate.

        Response is wrapped: {"job_request": {...}} — unwrapped before returning.
        """
        payload: dict[str, Any] = {"job_type": job_type, "params": params}
        if user_key:
            payload["user_key"] = user_key
        try:
            resp = requests.post(
                self._url("/api/v1/jobs/requests"),  # gRPC-gateway v1
                json=payload,
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                # 409 Conflict (fixed server) or 500 with UNIQUE text (unfixed server):
                # both mean a job with this user_key already exists — find and return it.
                is_duplicate = (
                    (resp.status_code == 409) or
                    (resp.status_code == 500 and "UNIQUE" in resp.text)
                )
                if is_duplicate and user_key:
                    # Fixed server returns existing job in 409 body
                    if resp.status_code == 409:
                        try:
                            data = resp.json()
                            existing = data.get("job_request", data) if isinstance(data, dict) else {}
                            if existing.get("id"):
                                print(f"[formicary] reusing existing job {existing.get('id')} for user_key={user_key!r}", flush=True)
                                return existing
                        except Exception:
                            pass
                    # Unfixed server (500): look up by SlackThreadTs
                    thread_ts = params.get("SlackThreadTs", "")
                    if thread_ts:
                        existing_list = self.find_jobs(
                            state="ANY",
                            var_filter={"SlackThreadTs": thread_ts},
                            job_type=job_type,
                        )
                        if existing_list:
                            print(f"[formicary] reusing existing job {existing_list[0].get('id')} (user_key duplicate)", flush=True)
                            return existing_list[0]
                self._log_error(f"submit({job_type})", resp)
                return {}
            data = resp.json()
            # Unwrap {"job_request": {...}} envelope
            if isinstance(data, dict) and "job_request" in data:
                return data["job_request"]
            return data
        except Exception as exc:
            print(f"[formicary] submit error: {exc}", file=sys.stderr, flush=True)
            return {}

    def find_jobs(
        self,
        state: str = "PAUSED",
        var_filter: dict[str, str] | None = None,
        page_size: int = 50,
        job_type: str | None = None,
    ) -> list[dict]:
        """List jobs matching state and optional variable filter.

        ``var_filter`` is applied client-side by checking each job's ``params`` dict.
        ``job_type`` filters server-side when provided.
        """
        # pageSize (camelCase) is what ParseParams reads; job_state is the server filter name.
        # state="ANY" means no state filter — return jobs in any state.
        api_params: dict = {"pageSize": page_size}
        if state and state.upper() != "ANY":
            api_params["job_state"] = state
        if job_type:
            api_params["job_type"] = job_type
        try:
            resp = requests.get(
                self._url("/api/v1/jobs/requests"),
                params=api_params,
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                self._log_error(f"find_jobs(state={state})", resp)
                return []
            data = resp.json()
            jobs: list[dict] = []
            if isinstance(data, list):
                jobs = data
            elif isinstance(data, dict):
                for key in ("Records", "records", "jobs", "data", "results"):
                    if key in data and isinstance(data[key], list):
                        jobs = data[key]
                        break
                if not jobs:
                    jobs = [data] if data.get("id") else []
        except Exception as exc:
            print(f"[formicary] find_jobs error: {exc}", file=sys.stderr, flush=True)
            return []

        # Client-side exact match on job_type (q= is LIKE, may match partial names)
        if job_type:
            jobs = [j for j in jobs if j.get("job_type") == job_type]

        if not var_filter:
            return jobs

        matched: list[dict] = []
        for job in jobs:
            raw = job.get("params") or {}
            # Params may be a list [{name, value}] (from list API) or a flat dict (from detail API)
            if isinstance(raw, list):
                job_params = {p["name"]: p.get("value", "") for p in raw if "name" in p}
            else:
                job_params = raw
            if all(str(job_params.get(k, "")) == str(v) for k, v in var_filter.items()):
                matched.append(job)
        return matched

    def trigger_pending_or_submit(self, job_type: str, params: dict[str, Any]) -> dict:
        """For cron jobs: find the PENDING scheduled request, inject params, and trigger it.

        Cron jobs always have a PENDING request queued for their next scheduled time.
        Submitting a duplicate hits a UNIQUE constraint on user_key — must trigger the
        existing PENDING job instead.

        Params (e.g. SlackThreadTs, SlackChannel) are sent in the POST body so the
        server merges them into the job's param rows in a single atomic transaction
        before moving the job to the ready queue.

        Returns the job dict (has ``id``) on success, ``{"_no_cron_slot": True}`` when
        no scheduled request exists (cron slot broken — do NOT fall back to submit),
        or ``{}`` on HTTP/network failure.
        """
        # WAITING expands to PENDING|PAUSED|READY server-side — catches all pre-run states.
        # Filter to cron-triggered slots only: a manually submitted job of the same job_type
        # must not be confused with the scheduled cron slot.
        pending = [j for j in self.find_jobs(state="WAITING", job_type=job_type)
                   if j.get("cron_triggered")]
        if not pending:
            # Fall back to CANCELLED: the trigger endpoint now accepts CANCELLED cron slots
            # and re-activates them (rotates user_key, sets state=PENDING, scheduled_at=now).
            # This recovers a broken cron schedule without needing DB access.
            cancelled = self.find_jobs(state="CANCELLED", job_type=job_type)
            # Use the most recent CANCELLED record that is cron-triggered
            pending = [j for j in cancelled if j.get("cron_triggered")]
            if pending:
                print(
                    f"[formicary] no WAITING slot for {job_type!r}; found CANCELLED cron slot "
                    f"{pending[0].get('id')!r} — will trigger to re-activate it",
                    flush=True,
                )
            else:
                # Check if a cron slot is currently EXECUTING — user just needs to wait.
                executing = [j for j in self.find_jobs(state="EXECUTING", job_type=job_type)
                             if j.get("cron_triggered")]
                if executing:
                    job_id = executing[0].get("id")
                    print(
                        f"[formicary] cron job {job_type!r} is already EXECUTING as {job_id!r}",
                        flush=True,
                    )
                    return {"_already_executing": True, "id": job_id, "job_type": job_type}
                print(
                    f"[formicary] no PENDING/WAITING, CANCELLED, or EXECUTING request found for cron job {job_type!r} — "
                    "the cron slot is missing entirely. Re-enable the job definition in Formicary.",
                    flush=True,
                )
                # Never fall back to submit() for cron jobs: Formicary auto-assigns a
                # deterministic user_key for the next scheduled slot, so submit() always
                # hits a UNIQUE constraint when a CANCELLED record already owns that key.
                return {"_no_cron_slot": True}

        job_id = pending[0].get("id")
        print(f"[formicary] found PENDING {job_id} for {job_type}, triggering with params", flush=True)

        body: dict[str, Any] = {}
        if params:
            body["params"] = params
        try:
            resp = requests.post(
                self._url(f"/api/v1/jobs/requests/{job_id}/trigger"),
                json=body if body else None,
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                self._log_error(f"trigger_pending_or_submit({job_id})", resp)
                return {}
        except Exception as exc:
            print(f"[formicary] trigger error: {exc}", file=sys.stderr, flush=True)
            return {}

        return {"id": job_id, "job_type": job_type}

    def get_job(self, job_id: str) -> dict | None:
        """Fetch a single job by ID.  Returns ``None`` on error."""
        try:
            resp = requests.get(
                self._url(f"/api/v1/jobs/requests/{job_id}"),
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                self._log_error(f"get_job({job_id})", resp)
                return None
            return resp.json()
        except Exception as exc:
            print(f"[formicary] get_job error: {exc}", file=sys.stderr, flush=True)
            return None

    def resume(self, job_id: str, variables: dict[str, Any] | None = None) -> bool:
        """Resume/trigger a paused or pending job.

        Optional variables are passed as params in the POST body so the server
        merges them atomically before triggering — no separate GET/PUT needed.

        Returns ``True`` on success, ``False`` on any HTTP error.
        """
        body: dict[str, Any] = {}
        if variables:
            body["params"] = variables
        try:
            resp = requests.post(
                self._url(f"/api/v1/jobs/requests/{job_id}/trigger"),
                json=body if body else None,
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                self._log_error(f"resume trigger({job_id})", resp)
                return False
            return True
        except Exception as exc:
            print(f"[formicary] resume trigger error: {exc}", file=sys.stderr, flush=True)
            return False

    def get_org_configs(self) -> dict[str, str]:
        """Fetch org-level configs from Formicary and return as a flat dict.

        Keys are returned as UPPER_SNAKE_CASE so they merge cleanly with os.environ.
        e.g. FormicaryPublicUrl → FORMICARY_PUBLIC_URL
        """
        import re

        try:
            # Resolve org_id from JWT payload
            import base64
            import json as _json
            parts = self.token.split(".")
            if len(parts) == 3:
                payload = _json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
                org_id = payload.get("org_id", "")
            else:
                org_id = ""
            if not org_id:
                return {}

            resp = requests.get(
                self._url(f"/api/v1/orgs/{org_id}/configs"),
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                return {}
            data = resp.json()
            items = data if isinstance(data, list) else (
                data.get("Records") or data.get("records") or []
            )

            def _to_upper(name: str) -> str:
                # CamelCase → UPPER_SNAKE_CASE
                s = re.sub(r"([A-Z])", r"_\1", name).lstrip("_").upper()
                return s

            result: dict[str, str] = {}
            for item in items:
                name = item.get("name", "")
                value = item.get("value", "")
                if name and value is not None:
                    result[_to_upper(name)] = str(value)
            return result
        except Exception as exc:
            print(f"[formicary] get_org_configs error: {exc}", file=sys.stderr, flush=True)
            return {}

    def deploy_definition(self, yaml_content: str) -> bool:
        """Upload or update a job definition from raw YAML content.

        Tries POST first; if 409 (already exists), falls back to PUT by job_type.
        Returns True on success.
        """
        import yaml as _yaml  # local import — pyyaml optional at module level

        try:
            data = _yaml.safe_load(yaml_content)
            job_type = data.get("job_type", "") if isinstance(data, dict) else ""
        except Exception:
            job_type = ""
        if not job_type:
            print("[formicary] deploy_definition: missing or unparseable job_type", file=sys.stderr)
            return False

        headers = dict(self._headers())
        headers["Content-Type"] = "application/x-yaml"
        try:
            resp = requests.post(
                self._url("/api/v1/jobs/definitions"),
                data=yaml_content.encode(),
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
            )
            if resp.status_code in (200, 201):
                return True
            if resp.status_code == 409:
                put_resp = requests.put(
                    self._url(f"/api/v1/jobs/definitions/{job_type}"),
                    data=yaml_content.encode(),
                    headers=headers,
                    timeout=_DEFAULT_TIMEOUT,
                )
                if put_resp.ok:
                    return True
                self._log_error(f"deploy_definition PUT({job_type})", put_resp)
                return False
            self._log_error(f"deploy_definition POST({job_type})", resp)
            return False
        except Exception as exc:
            print(f"[formicary] deploy_definition error: {exc}", file=sys.stderr, flush=True)
            return False

    def list_definitions(self) -> list[dict]:
        """Return all registered job definition summaries."""
        try:
            resp = requests.get(
                self._url("/api/v1/jobs/definitions"),
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                self._log_error("list_definitions", resp)
                return []
            data = resp.json()
            if isinstance(data, list):
                return data
            for key in ("Records", "records", "jobs", "data", "results"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return [data] if isinstance(data, dict) and data.get("job_type") else []
        except Exception as exc:
            print(f"[formicary] list_definitions error: {exc}", file=sys.stderr, flush=True)
            return []

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> FormicaryClient:
        """Construct a client from environment variables.

        FORMICARY_URL   — default http://localhost:7777
        FORMICARY_TOKEN — required for authenticated endpoints
        """
        base_url = os.environ.get("FORMICARY_URL", "http://localhost:7777")
        token = os.environ.get("FORMICARY_TOKEN", "")
        return cls(base_url=base_url, token=token)
