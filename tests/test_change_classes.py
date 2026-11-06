"""Tests for scripts/common/change_classes.py (what kind of code a path is) and the shared module/blast helpers."""

import pytest

from scripts.common import change_classes as cc


@pytest.mark.parametrize("path,expected", [
    ("api/v1/openapi.yaml", {"contract"}),
    ("proto/payments/v1/payment.proto", {"contract"}),
    ("schemas/user.schema.json", {"contract"}),
    ("graphql/schema.graphql", {"contract"}),
    ("src/handlers/orders.go", {"api"}),
    ("app/routes/users.py", {"api"}),
    ("internal/models/invoice.go", {"model"}),
    ("src/types/status_enum.ts", {"enum", "model"}),
    ("db/migrations/0042_drop_legacy.sql", {"migration"}),
    ("alembic/versions/ab12_add_col.py", {"migration"}),
    ("config/settings.yaml", {"config"}),
    ("src/feature_flags/defaults.py", {"config"}),
    (".github/workflows/ci.yml", {"infra"}),
    ("deploy/helm/values.yaml", {"infra"}),
    ("infra/terraform/main.tf", {"infra"}),
    ("sdk/python/client.py", {"public_surface"}),
    ("cli/main.go", {"public_surface"}),
    ("services/billing/charge.py", set()),
])
def test_classify_path(path, expected):
    assert set(cc.classify_path(path)) >= expected
    if not expected:
        assert cc.classify_path(path) == []


def test_tests_and_docs_are_never_contract_or_api():
    assert cc.classify_path("tests/api/test_routes.py") == []
    assert cc.classify_path("docs/api/overview.md") == []
    assert cc.classify_path("src/handlers/orders_test.go") == []


def test_classify_files_groups_paths_by_class():
    got = cc.classify_files(["proto/a.proto", "db/migrations/1.sql", "src/handlers/x.go", "README.md"])
    assert got["contract"] == ["proto/a.proto"] and got["migration"] == ["db/migrations/1.sql"] and got["api"] == ["src/handlers/x.go"]
    assert "README.md" not in {p for v in got.values() for p in v}


@pytest.mark.parametrize("path,area", [
    ("services/auth/login.py", "auth"), ("lib/oauth/token.go", "auth"), ("pkg/billing/invoice.py", "billing"), ("payments/charge.ts", "billing"),
    ("security/policy.yaml", "security"), ("crypto/keys.go", "security"), ("deploy/k8s/app.yaml", "sre"), ("ops/alerts/slo.yaml", "sre"),
    ("terraform/network.tf", "sre"), ("services/search/index.py", None),
])
def test_sensitive_area(path, area):
    assert cc.sensitive_area(path) == area


def test_high_blast_areas_are_the_ones_that_force_high():
    assert set(cc.HIGH_BLAST_AREAS) >= {"auth", "billing", "security", "sre"}


def test_external_surface_is_contract_public_surface_or_api():
    assert cc.is_external({"contract": ["a"]}) and cc.is_external({"public_surface": ["a"]}) and cc.is_external({"api": ["a"]})
    assert not cc.is_external({"migration": ["a"], "config": ["b"]})
