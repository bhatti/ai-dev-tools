# SPDX-License-Identifier: LGPL-2.1-or-later
"""What kind of code a changed path is, from its path alone (language-agnostic heuristics).

Classes: ``contract`` (OpenAPI/proto/GraphQL/JSON schema), ``api`` (routes, handlers, controllers), ``model`` (domain
types), ``enum`` (enum/constant/status files), ``migration`` (database schema changes), ``config`` (settings, flags,
defaults), ``infra`` (CI, deploy, IaC), ``public_surface`` (SDKs, CLIs, public packages). Tests and docs are never classified.
``sensitive_area`` names the areas whose blast radius is always large (auth, security, billing, SRE/infra).
"""

from __future__ import annotations

import re

from scripts.common.pr_classify import is_test_file

HIGH_BLAST_AREAS = ("auth", "security", "billing", "sre")
EXTERNAL_CLASSES = ("contract", "public_surface", "api")

_RULES: list[tuple[str, re.Pattern]] = [
    ("contract", re.compile(r"(?i)(openapi|swagger|asyncapi)[^/]*\.(ya?ml|json)$|\.(proto|graphql|gql|thrift|avsc|xsd|wsdl)$|\.schema\.json$|(^|/)schemas?/[^/]+\.(json|ya?ml)$")),
    ("api", re.compile(r"(?i)(^|/)(api|apis|routes?|routers?|handlers?|controllers?|endpoints?|rest|grpc|rpc|resolvers?)(/|\.[a-z]+$)")),
    ("model", re.compile(r"(?i)(^|/)(models?|entities|entity|dto|dtos|domain|types?)(/|\.[a-z]+$)|(^|/)[^/]*(_model|model_|\.model)\.[a-z]+$")),
    ("enum", re.compile(r"(?i)(^|/)[^/]*(enum|enums|constants?|statuses|status|states?)[^/]*\.(py|go|java|kt|ts|tsx|js|rs|cs|proto|rb|scala|swift)$")),
    ("migration", re.compile(r"(?i)(^|/)(migrations?|alembic|flyway|liquibase|db/changelog|schema_changes)(/|$)|(^|/)schema\.sql$|\.sql$")),
    ("config", re.compile(r"(?i)(^|/)(config|configs|configuration|settings|flags?|feature[-_]?flags?|defaults?)(/|\.[a-z]+$)|\.(toml|ini|properties|conf|env)$")),
    ("infra", re.compile(r"(?i)(^|/)(\.github/workflows|\.circleci|\.gitlab-ci|jenkins|helm|charts?|k8s|kubernetes|terraform|ansible|deploy|deployment|docker|infra|ops)(/|$)|(^|/)(Dockerfile|Jenkinsfile|docker-compose[^/]*\.ya?ml)$|\.tf$")),
    ("public_surface", re.compile(r"(?i)(^|/)(sdks?|clients?|cli|public|pkg/api)(/|$)")),
]
_DOCS = re.compile(r"(?i)\.(md|rst|txt|adoc)$|(^|/)docs?/")
_AREAS: list[tuple[str, re.Pattern]] = [
    ("auth", re.compile(r"(?i)(^|/)(auth|authn|authz|oauth|saml|sso|iam|rbac|acl|permissions?|tokens?|login|sessions?)(/|$|\.|_)")),
    ("billing", re.compile(r"(?i)(^|/)(billing|payments?|invoices?|subscriptions?|pricing|checkout|ledger)(/|$|\.|_)")),
    ("security", re.compile(r"(?i)(^|/)(security|crypto|secrets?|credentials?|encryption|keys?|certs?|tls|vault)(/|$|\.|_)|(^|/)\.env")),
    ("sre", re.compile(r"(?i)(^|/)(sre|ops|alerts?|monitoring|observability|slo|runbooks?|k8s|kubernetes|helm|terraform|ansible|deploy|deployment|infra)(/|$)|\.tf$|(^|/)Dockerfile$")),
]


def classify_path(path: str) -> list[str]:
    """Classes of one changed path ([] for tests, docs and ordinary application code)."""
    if is_test_file(path) or _DOCS.search(path) or re.search(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)", path):
        return []
    return [name for name, rx in _RULES if rx.search(path)]


def classify_files(paths: list[str]) -> dict[str, list[str]]:
    """Paths grouped by class (a path can appear under several classes)."""
    out: dict[str, list[str]] = {}
    for p in paths:
        for c in classify_path(p):
            out.setdefault(c, []).append(p)
    return out


def sensitive_area(path: str) -> str | None:
    """auth / billing / security / sre when the path belongs to such an area, else None."""
    if is_test_file(path) or _DOCS.search(path):
        return None
    return next((name for name, rx in _AREAS if rx.search(path)), None)


def is_external(classes: dict[str, list[str]]) -> bool:
    """True when the change touches something consumers outside the owning team can see or call."""
    return any(classes.get(c) for c in EXTERNAL_CLASSES)
