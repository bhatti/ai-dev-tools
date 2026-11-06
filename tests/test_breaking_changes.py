"""Tests for scripts/analyze/breaking_changes.py: backward-incompatible change signals from diffs and PR text (synthetic diffs only)."""

from scripts.analyze import breaking_changes as bc


def diff(path, *lines):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,9 +1,9 @@\n" + "\n".join(lines) + "\n"


def kinds(d):
    return sorted({s["kind"] for s in bc.analyze_diff(d)})


class TestProto:
    def test_removed_field_is_high_confidence(self):
        s = bc.analyze_diff(diff("proto/pay.proto", " message Pay {", "-  string currency = 3;", "   int64 amount = 1;", " }"))
        assert [(x["kind"], x["confidence"]) for x in s] == [("field_removed", "high")] and "currency" in s[0]["evidence"]

    def test_renumbered_and_retyped_fields(self):
        assert kinds(diff("a.proto", "-  string id = 1;", "+  string id = 2;")) == ["field_renumbered"]
        assert kinds(diff("a.proto", "-  int32 count = 4;", "+  string count = 4;")) == ["field_type_changed"]

    def test_added_field_and_moved_fields_are_not_breaking(self):
        assert kinds(diff("a.proto", "+  string note = 9;")) == []
        assert kinds(diff("a.proto", "-  string id = 1;", "+  string id = 1;")) == []

    def test_enum_value_removed_and_changed_rpc_removed(self):
        assert kinds(diff("a.proto", "-  PENDING = 2;")) == ["enum_value_removed"]
        assert kinds(diff("a.proto", "-  ACTIVE = 1;", "+  ACTIVE = 5;")) == ["enum_value_changed"]
        assert kinds(diff("a.proto", "-  rpc Cancel(CancelRequest) returns (CancelReply);")) == ["rpc_removed"]


class TestSchemas:
    def test_removed_endpoint_in_openapi(self):
        assert kinds(diff("api/openapi.yaml", "-  /v1/orders/{id}/cancel:", "-    post:", "   /v1/orders:")) == ["endpoint_removed"]

    def test_new_required_property_json_schema(self):
        d = diff("schemas/order.schema.json", '-  "required": ["id"],', '+  "required": ["id", "currency"],')
        s = bc.analyze_diff(d)
        assert [x["kind"] for x in s] == ["required_added"] and "currency" in s[0]["evidence"]

    def test_new_required_item_in_yaml_list(self):
        d = diff("api/openapi.yaml", "       required:", "         - id", "+        - currency")
        assert kinds(d) == ["required_added"]

    def test_removed_enum_item_in_yaml(self):
        d = diff("api/openapi.yaml", "       enum:", "         - open", "-        - archived")
        assert kinds(d) == ["enum_value_removed"]

    def test_property_removed_and_type_changed(self):
        assert kinds(diff("schemas/o.schema.json", '-    "legacyId": {"type": "string"},')) == ["schema_field_removed"]
        assert kinds(diff("api/openapi.yaml", "-        type: string", "+        type: integer")) == ["type_changed"]


class TestRoutes:
    def test_removed_route(self):
        assert kinds(diff("app/routes.py", '-@app.get("/v1/items")', " def items():")) == ["route_removed"]

    def test_renamed_path_and_changed_method(self):
        assert "route_removed" in kinds(diff("app/routes.py", '-@app.get("/v1/items")', '+@app.get("/v2/items")'))
        assert kinds(diff("app/routes.py", '-@app.get("/v1/items")', '+@app.post("/v1/items")')) == ["route_method_changed"]

    def test_go_router_registration(self):
        assert kinds(diff("srv/router.go", '-\tr.DELETE("/v1/items/:id", h.Delete)')) == ["route_removed"]

    def test_added_route_is_fine(self):
        assert kinds(diff("app/routes.py", '+@app.get("/v1/new")')) == []

    def test_status_code_change(self):
        assert kinds(diff("app/handlers.py", "-    return jsonify(err), 400", "+    return jsonify(err), 422")) == ["status_code_changed"]


class TestEnumsAndSymbols:
    def test_removed_enum_constant_in_code(self):
        assert kinds(diff("src/types/status_enum.py", "-    ARCHIVED = 3", "     ACTIVE = 1")) == ["enum_value_removed"]

    def test_changed_enum_value(self):
        assert kinds(diff("src/status_enum.ts", "-  ACTIVE = 'active',", "+  ACTIVE = 'enabled',")) == ["enum_value_changed"]

    def test_function_signature_changes(self):
        assert kinds(diff("sdk/client.py", "-def fetch(id, limit=10):", "+def fetch(id, limit=10, scope):")) == ["required_param_added"]
        assert kinds(diff("sdk/client.py", "-def fetch(id, limit=10):", "+def fetch(id):")) == ["param_removed"]

    def test_removed_public_function_but_not_private_or_tests(self):
        assert kinds(diff("sdk/client.py", "-def fetch(id):")) == ["public_symbol_removed"]
        assert kinds(diff("sdk/client.py", "-def _helper(id):")) == []
        assert kinds(diff("tests/test_client.py", "-def fetch(id):")) == []


class TestDatabase:
    def test_destructive_and_locking_statements(self):
        assert kinds(diff("db/migrations/1.sql", "+ALTER TABLE orders DROP COLUMN legacy_id;")) == ["db_drop"]
        assert kinds(diff("db/migrations/2.sql", "+ALTER TABLE orders RENAME COLUMN total TO amount;")) == ["db_rename"]
        assert kinds(diff("db/migrations/3.sql", "+ALTER TABLE orders ADD COLUMN currency text NOT NULL;")) == ["db_not_null_no_default"]
        assert kinds(diff("db/migrations/4.sql", "+ALTER TABLE orders ADD COLUMN currency text NOT NULL DEFAULT 'USD';")) == []
        assert kinds(diff("db/migrations/5.sql", "+ALTER TABLE orders ALTER COLUMN total TYPE bigint;")) == ["db_type_changed"]

    def test_orm_migration_helpers(self):
        assert kinds(diff("alembic/versions/ab.py", "+    op.drop_column('orders', 'legacy_id')")) == ["db_drop"]
        assert kinds(diff("db/migrate/20260101_x.rb", "+    remove_column :orders, :legacy_id")) == ["db_drop"]


class TestSemanticDefaults:
    def test_changed_default_timeout_or_limit(self):
        s = bc.analyze_diff(diff("config/settings.yaml", "-request_timeout: 30", "+request_timeout: 5"))
        assert [(x["kind"], x["confidence"]) for x in s] == [("default_changed", "medium")] and "30" in s[0]["evidence"] and "5" in s[0]["evidence"]

    def test_flag_flip_in_code_is_low_confidence(self):
        s = bc.analyze_diff(diff("svc/limits.py", "-MAX_RETRIES = 5", "+MAX_RETRIES = 1"))
        assert [(x["kind"], x["confidence"]) for x in s] == [("default_changed", "low")]

    def test_unrelated_value_changes_are_ignored(self):
        assert kinds(diff("svc/x.py", "-greeting = 'hi'", "+greeting = 'hello'")) == []
        assert kinds(diff("config/settings.yaml", "-request_timeout: 30", "+request_timeout: 30")) == []


class TestTextAndTiers:
    def test_declared_breaking_in_title_body_or_labels(self):
        assert [s["kind"] for s in bc.analyze_text("feat!: drop v1 API", "", [])] == ["declared_breaking"]
        assert [s["kind"] for s in bc.analyze_text("Cleanup", "BREAKING CHANGE: removed the old flag", [])] == ["declared_breaking"]
        assert [s["kind"] for s in bc.analyze_text("Cleanup", "", ["breaking-change"])] == ["declared_breaking"]
        assert [s["kind"] for s in bc.analyze_text("Add X", "Deprecates the legacy endpoint", [])] == ["deprecation"]
        assert bc.analyze_text("Fix typo", "nothing special", []) == []

    def test_tier1_path_only_candidates_are_low_confidence(self):
        s = bc.tier1({"contract": ["a.proto"], "migration": ["m.sql"], "config": ["c.yaml"], "enum": ["e.py"], "api": ["h.go"], "model": ["m.go"]})
        assert {x["kind"] for x in s} == {"contract_changed", "migration_changed", "config_changed", "enum_or_model_changed", "api_changed"}
        assert {x["confidence"] for x in s} == {"low"}

    def test_priority_ranks_contract_and_declared_changes_first(self):
        hi = bc.priority({"contract": ["a.proto"], "migration": ["m.sql"]}, declared=True, blast="high")
        lo = bc.priority({"config": ["c.yaml"]}, declared=False, blast="low")
        assert hi > lo > bc.priority({}, declared=False, blast="low") == 0

    def test_diff_parser_handles_renames_binary_and_empty_input(self):
        assert bc.analyze_diff("") == []
        assert bc.analyze_diff("diff --git a/x.png b/x.png\nBinary files differ\n") == []
        assert bc.parse_diff(diff("a/b.py", "-x", "+y")) == {"a/b.py": {"removed": ["x"], "added": ["y"], "lines": [("-", "x"), ("+", "y")]}}

    def test_signals_are_capped_per_file_and_deduplicated(self):
        many = diff("proto/a.proto", *[f"-  string f{i} = {i + 1};" for i in range(60)])
        out = bc.analyze_diff(many)
        assert len(out) == bc.MAX_SIGNALS_PER_FILE
        assert len({(s["kind"], s["evidence"]) for s in out}) == len(out)


class TestNoise:
    def test_dependency_manifests_and_ci_files_do_not_produce_default_changes(self):
        assert kinds(diff("go.mod", "-go 1.26", "+go 1.27")) == []
        assert kinds(diff("package.json", '-  "max": 1,', '+  "max": 2,')) == []
        assert kinds(diff(".github/workflows/ci.yml", "-    timeout: 30", "+    timeout: 10")) == []

    def test_the_body_of_an_automated_pr_is_not_trusted_for_declared_breaking_changes(self):
        body = "Release notes: BREAKING CHANGE: v3 drops node 14"
        assert bc.analyze_text("Bump foo from 2 to 3", body, [], trust_body=False) == []
        assert [s["kind"] for s in bc.analyze_text("feat!: drop it", body, [], trust_body=False)] == ["declared_breaking"]
        assert [s["kind"] for s in bc.analyze_text("Add x", body, [], trust_body=True)] == ["declared_breaking"]


class TestHostileInput:
    def test_very_long_lines_are_cut_and_do_not_stall_the_parser(self):
        import time
        evil = "-  " + "a " * 20000 + "= 1;"
        start = time.monotonic()
        bc.analyze_diff(diff("proto/a.proto", evil, "+" + evil[1:]))
        assert time.monotonic() - start < 2
        assert all(len(t) <= bc.MAX_LINE_CHARS for t in bc.parse_diff(diff("a.proto", evil))["a.proto"]["removed"])

    def test_credentials_in_evidence_are_redacted(self):
        s = bc.analyze_diff(diff("config/settings.yaml", "-default_api_key: abc", "+default_api_key: password=hunter2hunter2"))
        assert s and "hunter2hunter2" not in repr(s)
