"""Thin wrapper over the Formicary REST API.

Formicary endpoints used:
    POST   /api/jobs/requests              submit a new job
    GET    /api/jobs/requests              list/query jobs
    GET    /api/jobs/requests/:id          get single job
    PUT    /api/jobs/requests/:id          update job params
    POST   /api/jobs/requests/:id/trigger  resume/trigger a paused job

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

    def submit(self, job_type: str, params: dict[str, Any]) -> dict:
        """Submit a new job.  Returns saved job dict (has ``id`` field) or ``{}`` on error."""
        payload = {"job_type": job_type, "params": params}
        try:
            resp = requests.post(
                self._url("/api/jobs/requests"),
                json=payload,
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                self._log_error(f"submit({job_type})", resp)
                return {}
            return resp.json()
        except Exception as exc:
            print(f"[formicary] submit error: {exc}", file=sys.stderr, flush=True)
            return {}

    def find_jobs(
        self,
        state: str = "PAUSED",
        var_filter: dict[str, str] | None = None,
        page_size: int = 50,
    ) -> list[dict]:
        """List jobs matching state and optional variable filter.

        ``var_filter`` is applied client-side by checking each job's ``params`` dict.
        """
        try:
            resp = requests.get(
                self._url("/api/jobs/requests"),
                params={"state": state, "page_size": page_size},
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not resp.ok:
                self._log_error(f"find_jobs(state={state})", resp)
                return []
            data = resp.json()
            # API may return a list directly or wrapped in a "records" / "jobs" key
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

        if not var_filter:
            return jobs

        matched: list[dict] = []
        for job in jobs:
            job_params: dict = job.get("params") or {}
            if all(str(job_params.get(k, "")) == str(v) for k, v in var_filter.items()):
                matched.append(job)
        return matched

    def get_job(self, job_id: str) -> dict | None:
        """Fetch a single job by ID.  Returns ``None`` on error."""
        try:
            resp = requests.get(
                self._url(f"/api/jobs/requests/{job_id}"),
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
        """Resume/trigger a paused job.

        If ``variables`` are provided:
        1. GET the current job to read existing params
        2. Merge new variables into existing params
        3. PUT the job with updated params
        4. POST trigger

        Returns ``True`` on success, ``False`` on any HTTP error.
        """
        if variables:
            current = self.get_job(job_id)
            if current is None:
                print(
                    f"[formicary] resume({job_id}): could not fetch current job",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            merged_params: dict = dict(current.get("params") or {})
            merged_params.update(variables)
            updated = dict(current)
            updated["params"] = merged_params
            try:
                put_resp = requests.put(
                    self._url(f"/api/jobs/requests/{job_id}"),
                    json=updated,
                    headers=self._headers(),
                    timeout=_DEFAULT_TIMEOUT,
                )
                if not put_resp.ok:
                    self._log_error(f"resume PUT({job_id})", put_resp)
                    return False
            except Exception as exc:
                print(f"[formicary] resume PUT error: {exc}", file=sys.stderr, flush=True)
                return False

        try:
            trig_resp = requests.post(
                self._url(f"/api/jobs/requests/{job_id}/trigger"),
                headers=self._headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if not trig_resp.ok:
                self._log_error(f"resume trigger({job_id})", trig_resp)
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
                self._url(f"/api/orgs/{org_id}/configs"),
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
                self._url("/api/jobs/definitions"),
                data=yaml_content.encode(),
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
            )
            if resp.status_code in (200, 201):
                return True
            if resp.status_code == 409:
                put_resp = requests.put(
                    self._url(f"/api/jobs/definitions/{job_type}"),
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
                self._url("/api/jobs/definitions"),
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
