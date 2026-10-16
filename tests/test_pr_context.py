"""Tests for scripts/common/pr_context.py (no network: fetchers and HTTP are mocked)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts.common import pr_context as pc

GH_URL = "https://github.com/acme/shop/pull/12"
BB_URL = "https://bitbucket.org/acme/shop/pull-requests/12/overview"

DIFF = (
    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    "diff --git a/web/b.ts b/web/b.ts\n--- a/web/b.ts\n+++ b/web/b.ts\n@@ -1 +1 @@\n-a\n+b\n"
)


def _pr(**kw):
    base = {"number": 12, "title": "Add cart limit", "body": "Fixes #7", "url": GH_URL,
            "headRefName": "feature/cart", "baseRefName": "main", "state": "open", "author": "dev",
            "file_paths": ["src/a.py"]}
    base.update(kw)
    return base


class TestSplitAcItems:
    def test_checkboxes_bullets_and_numbers(self):
        text = "- [ ] user can add item\n- [x] limit is 10\n* error on 11th\n1. returns 409\n2) logs event"
        assert pc.split_ac_items(text) == [
            "user can add item", "limit is 10", "error on 11th", "returns 409", "logs event"]

    def test_given_when_then_grouped_into_one_item(self):
        text = "Given a full cart\nWhen I add an item\nThen I see an error\n\nGiven an empty cart\nThen total is 0"
        assert pc.split_ac_items(text) == [
            "Given a full cart When I add an item Then I see an error",
            "Given an empty cart Then total is 0"]

    def test_indented_continuation_joined(self):
        assert pc.split_ac_items("- first line\n  continues here") == ["first line continues here"]

    def test_dedup_and_limit(self):
        assert pc.split_ac_items("- a\n- a\n- b") == ["a", "b"]
        assert len(pc.split_ac_items("\n".join(f"- item {i}" for i in range(80)))) == 50

    def test_empty(self):
        assert pc.split_ac_items("") == []


class TestCollectIssueRefs:
    def test_without_a_project_scope_only_the_prs_own_ticket_is_followed(self):
        pr = _pr(title="PLAT-5 fix, supports UTF-8 and SHA-256", body="also PLAT-9 and PLAT-5 and X86-64", headRefName="bugs/PLAT-2-thing")
        ignored: list[str] = []
        refs = pc.collect_issue_refs(pr, {"JIRA_BASE_URL": "https://j.example/"}, ignored=ignored)
        assert [r["key"] for r in refs] == ["PLAT-2"] and refs[0]["url"] == "https://j.example/browse/PLAT-2"
        assert ignored == ["PLAT-5 (Jira project not scoped: set JIRA_PROJECT to follow more than one key)"]

    def test_with_a_project_scope_branch_title_and_body_keys_are_followed(self):
        pr = _pr(title="PLAT-5 fix, supports UTF-8 and SHA-256", body="also PLAT-9 and PLAT-5 and X86-64 and OTHER-1", headRefName="bugs/PLAT-2-thing")
        assert [r["key"] for r in pc.collect_issue_refs(pr, {"JIRA_PROJECT": "PLAT"})] == ["PLAT-2", "PLAT-5", "PLAT-9"]

    def test_jira_project_filter(self):
        pr = _pr(title="OTHER-1 and PLAT-3", body="", headRefName="")
        refs = pc.collect_issue_refs(pr, {"JIRA_PROJECT": "PLAT"})
        assert [r["key"] for r in refs] == ["PLAT-3"]

    def test_github_refs_stay_inside_the_prs_own_repository_and_exclude_the_pr(self):
        pr = _pr(number=12, title="x", body="Closes other/repo#5, fixes #12, resolves acme/shop#8, fixes #9", headRefName="")
        ignored: list[str] = []
        refs = pc.collect_issue_refs(pr, {"GH_ORG": "acme", "GH_REPO": "shop"}, ignored=ignored)
        assert [(r["repo"], r["key"]) for r in refs] == [("acme/shop", "#8"), ("acme/shop", "#9")]
        assert ignored == ["other/repo#5 (outside this repository)"]

    def test_bare_hash_used_only_without_closing_keywords(self):
        pr = _pr(title="see #3", body="", headRefName="")
        refs = pc.collect_issue_refs(pr, {"GH_ORG": "acme", "GH_REPO": "shop"})
        assert [r["key"] for r in refs] == ["#3"]

    def test_no_refs(self):
        assert pc.collect_issue_refs(_pr(title="t", body="nothing", headRefName=""), {}) == []


class FakePopen:
    """Stands in for subprocess.Popen(stdout=<file>, stderr=<file>): writes the canned output into the given files."""

    def __init__(self, data: bytes, rc: int = 0, stderr: bytes = b"", hang: bool = False):
        self.data, self.rc, self.err, self.hang, self.killed = data, rc, stderr, hang, False

    def __call__(self, argv, stdout=None, stderr=None, **kw):
        self.argv = argv
        stdout.write(self.data)
        stderr.write(self.err)
        return self

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise pc.subprocess.TimeoutExpired("gh", timeout)
        return self.rc

    def kill(self):
        self.killed = True


class FakeResp:
    def __init__(self, data: bytes, ok: bool = True, status: int = 200):
        self._data, self.ok, self.status_code = data, ok, status

    def iter_content(self, size):
        for i in range(0, len(self._data), size):
            yield self._data[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDiff:
    def test_changed_files_from_diff(self):
        assert pc.changed_files_from_diff(DIFF) == ["src/a.py", "web/b.ts"]

    def test_github_diff_uses_gh(self):
        popen = FakePopen(DIFF.encode())
        with patch.object(pc.subprocess, "Popen", popen):
            text, err = pc.fetch_pr_diff({"GH_ORG": "acme", "GH_REPO": "shop"}, "github", "12")
        assert (text, err) == (DIFF, "") and popen.argv == ["gh", "pr", "diff", "12", "-R", "acme/shop"]

    def test_github_diff_failure_reports_error(self):
        with patch.object(pc.subprocess, "Popen", FakePopen(b"", rc=1, stderr=b"HTTP 404: Not Found")):
            text, err = pc.fetch_pr_diff({"GH_ORG": "a", "GH_REPO": "b"}, "github", "1")
        assert text == "" and "exit 1" in err and "404" in err

    def test_github_diff_read_is_capped(self):
        with patch.object(pc.subprocess, "Popen", FakePopen(b"x" * 10_000)):
            text, err = pc.fetch_pr_diff({"GH_ORG": "a", "GH_REPO": "b"}, "github", "1", max_bytes=100)
        assert err == "" and len(text) == 101

    def test_a_hung_gh_is_killed_after_the_timeout(self):
        popen = FakePopen(b"", hang=True)
        with patch.object(pc.subprocess, "Popen", popen):
            text, err = pc.fetch_pr_diff({"GH_ORG": "a", "GH_REPO": "b"}, "github", "1")
        assert text == "" and "timed out" in err and popen.killed

    def test_missing_gh_binary(self):
        with patch.object(pc.subprocess, "Popen", side_effect=FileNotFoundError):
            assert "FileNotFoundError" in pc.fetch_pr_diff({}, "github", "1")[1]

    def test_bitbucket_diff_uses_rest_with_basic_auth(self):
        cfg = {"BITBUCKET_WORKSPACE": "acme", "BITBUCKET_REPO": "shop",
               "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        with patch.object(pc.requests, "get", return_value=FakeResp(DIFF.encode())) as get:
            text, err = pc.fetch_pr_diff(cfg, "bitbucket", "12")
        assert (text, err) == (DIFF, "")
        assert get.call_args.args[0].endswith("/repositories/acme/shop/pullrequests/12/diff")
        assert get.call_args.kwargs["auth"] == ("u", "t") and get.call_args.kwargs["stream"] is True

    def test_bitbucket_diff_read_is_capped(self):
        cfg = {"BITBUCKET_WORKSPACE": "a", "BITBUCKET_REPO": "b", "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        with patch.object(pc.requests, "get", return_value=FakeResp(b"y" * 500_000)):
            text, _ = pc.fetch_pr_diff(cfg, "bitbucket", "1", max_bytes=1000)
        assert 1001 <= len(text) <= 65536 + 1 and len(text) < 500_000

    def test_bitbucket_diff_http_error(self):
        with patch.object(pc.requests, "get", return_value=FakeResp(b"", ok=False, status=401)):
            _, err = pc.fetch_pr_diff({"BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}, "bitbucket", "1")
        assert "401" in err


class TestMissingCredentials:
    def test_bitbucket_names_only(self):
        assert pc.missing_credentials("bitbucket", {}) == ["BITBUCKET_USERNAME", "BITBUCKET_TOKEN"]
        assert pc.missing_credentials("bitbucket", {"BITBUCKET_USERNAME": "u", "BITBUCKET_APP_PASSWORD": "p"}) == []

    def test_github_has_no_requirements(self):
        assert pc.missing_credentials("github", {}) == []


def _issue(ac="- [ ] limit is 10\n- [ ] error on 11th", **kw):
    body = f"## Acceptance Criteria\n{ac}\n" if ac else "just prose"
    d = {"title": "Cart limit", "body": body, "labels": ["bug"], "design_doc_links": []}
    d.update(kw)
    return d


class TestBuildContext:
    def _build(self, ref, cfg, pr, diff=(DIFF, ""), issue=None, tmp_path=None):
        with patch.object(pc, "fetch_prs_by_numbers", return_value=[pr] if pr else []), \
             patch.object(pc, "fetch_pr_diff", return_value=diff), \
             patch.object(pc, "fetch_issue_details", return_value=issue):
            return pc.build_context(ref, cfg, tmp_path)

    def test_github_pr_with_linked_issue_ac(self, tmp_path):
        ctx = self._build(GH_URL, {}, _pr(), issue=_issue(), tmp_path=tmp_path)
        assert ctx["kind"] == "pr" and ctx["tracker"] == "github"
        assert ctx["pr"]["source_branch"] == "feature/cart" and ctx["pr"]["destination_branch"] == "main"
        assert ctx["changed_files"] == ["src/a.py", "web/b.ts"]
        assert ctx["issues"][0]["acceptance_criteria"] == ["limit is 10", "error on 11th"]
        assert ctx["ac_source"] == "issue:#7"
        assert (tmp_path / "pr.diff").read_text() == DIFF and ctx["diff_path"].endswith("pr.diff")

    def test_slack_wrapped_bitbucket_url_and_pr_description_ac(self):
        cfg = {"BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        pr = _pr(number=12, body="## Acceptance Criteria\n- returns 429 over quota\n- logs a warning\n")
        ctx = self._build(f"<{BB_URL}|bitbucket.org/acme/shop/pull-requests/12/overview>", cfg, pr)
        assert ctx["tracker"] == "bitbucket"
        assert ctx["pr_acceptance_criteria"] == ["returns 429 over quota", "logs a warning"]
        assert ctx["ac_source"] == "pr_description"

    def test_bitbucket_missing_credentials_is_actionable_and_leaks_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))  # no acli on this PATH
        with pytest.raises(ValueError) as e:
            pc.build_context(BB_URL, {})
        assert "BITBUCKET_USERNAME" in str(e.value) and "BITBUCKET_TOKEN" in str(e.value) and "acli" in str(e.value)

    def test_pr_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            self._build(GH_URL, {}, None)

    def test_no_ac_anywhere(self):
        ctx = self._build(GH_URL, {}, _pr(body="Fixes #7"), issue=_issue(ac=""))
        assert ctx["ac_source"] == "none" and ctx["issues"][0]["has_acceptance_criteria"] is False

    def test_access_denied_issue_is_not_reported_as_missing_ac(self):
        ctx = self._build(GH_URL, {}, _pr(), issue={"access_denied": True})
        assert ctx["issues"][0]["access_denied"] is True
        assert ctx["issues"][0]["has_acceptance_criteria"] is None

    def test_issue_fetch_failure_recorded(self):
        ctx = self._build(GH_URL, {}, _pr(), issue=None)
        assert "fetch_error" in ctx["issues"][0]

    def test_diff_truncated_and_error_reported(self):
        big = "diff --git a/f b/f\n" + "x" * 5000
        with patch.object(pc, "fetch_prs_by_numbers", return_value=[_pr()]), \
             patch.object(pc, "fetch_pr_diff", return_value=(big, "")), \
             patch.object(pc, "fetch_issue_details", return_value=None):
            ctx = pc.build_context(GH_URL, {}, max_diff_bytes=100)
        assert ctx["diff_truncated"] is True
        ctx = self._build(GH_URL, {}, _pr(), diff=("", "gh pr diff exit 1: nope"))
        assert ctx["diff_error"] and ctx["changed_files"] == ["src/a.py"]  # falls back to PR file list

    def test_jira_key_ref_is_issue_only(self):
        cfg = {"JIRA_BASE_URL": "https://j.example"}
        with patch.object(pc, "fetch_issue_details", return_value=_issue()):
            ctx = pc.build_context("PLAT-42", cfg)
        assert ctx["kind"] == "issue" and ctx["pr"] is None
        assert ctx["issues"][0]["url"] == "https://j.example/browse/PLAT-42"
        assert ctx["ac_source"] == "issue:PLAT-42"

    def test_github_issue_url_is_issue_only(self):
        with patch.object(pc, "fetch_issue_details", return_value=_issue()):
            ctx = pc.build_context("https://github.com/acme/shop/issues/7", {})
        assert ctx["kind"] == "issue" and ctx["tracker"] == "github"

    def test_garbage_ref_rejected(self):
        with pytest.raises(ValueError, match="cannot interpret"):
            pc.build_context("not-a-ref", {})


class TestCli:
    def test_error_exit_code_and_json(self, capsys):
        with patch.object(pc, "load_config", return_value={}):
            rc = pc.main(["not-a-ref"])
        assert rc == 2 and "error" in json.loads(capsys.readouterr().out)

    def test_writes_out_file(self, tmp_path, capsys):
        out = tmp_path / "ctx.json"
        ctx = {"kind": "pr", "tracker": "github", "issues": [], "ac_source": "none"}
        with patch.object(pc, "load_config", return_value={}), patch.object(pc, "build_context", return_value=ctx):
            rc = pc.main([GH_URL, "--out", str(out)])
        assert rc == 0 and json.loads(out.read_text())["tracker"] == "github"


JIRA_PLAIN_HEADING = """Acceptance Criteria

* Only users with sufficient permissions can delete nodes
* Only disconnected nodes can be deleted
** Away nodes CAN be deleted
* Disconnected nodes can be deleted individually or in bulk
* Warn user with confirmation.  Deleting nodes should require the user to type in “DELETE”
"""


class TestExtractSections:
    def test_plain_heading_with_jira_wiki_bullets_and_nesting(self):
        ac, hints = pc.extract_ac_items(JIRA_PLAIN_HEADING)
        assert ac == [
            "Only users with sufficient permissions can delete nodes",
            "Only disconnected nodes can be deleted (Away nodes CAN be deleted)",
            "Disconnected nodes can be deleted individually or in bulk",
            "Warn user with confirmation. Deleting nodes should require the user to type in “DELETE”"]
        assert hints == []

    @pytest.mark.parametrize("heading", ["## Acceptance Criteria", "h2. Acceptance Criteria", "*Acceptance Criteria*", "**Acceptance criteria:**",
                                          "Acceptance Criteria:", "### Definition of Done"])
    def test_heading_styles(self, heading):
        ac, _ = pc.extract_ac_items(f"Intro text\n\n{heading}\n- first thing\n- second thing\n\n## Notes\nunrelated")
        assert ac == ["first thing", "second thing"]

    def test_section_stops_at_next_heading_and_bold_title(self):
        text = "## Acceptance Criteria\n- a\n- b\n*Dispatch / status wiring*\n* not an AC bullet"
        assert pc.extract_ac_items(text)[0] == ["a", "b"]  # a bold-only title line ends the section
        ac, _ = pc.extract_ac_items("## Acceptance Criteria\n- a\n- b\n## Summary\n- c")
        assert ac == ["a", "b"]

    def test_how_to_test_is_returned_as_hints_not_criteria(self):
        text = "## Acceptance Criteria\n- limit is 10\n\n## How to test\n1. Add 11 items\n2. See the error banner\n"
        ac, hints = pc.extract_ac_items(text)
        assert ac == ["limit is 10"] and hints == ["Add 11 items", "See the error banner"]

    def test_long_sentences_are_not_mistaken_for_headings(self):
        text = "Requirements are described in the linked design document and may change.\n- not a criterion"
        assert pc.extract_ac_items(text)[0] == []

    def test_falls_back_to_checkbox_and_gwt_detectors(self):
        assert pc.extract_ac_items("Done when:\n- [ ] one\n- [x] two\n")[0] == ["one", "two"]
        gwt = pc.extract_ac_items("Given a cart\nWhen I add an item\nThen it is listed\nGiven an empty cart\nThen total is 0")[0]
        assert len(gwt) == 2 and gwt[0].startswith("Given a cart When I add")

    def test_markup_is_cleaned(self):
        assert pc.clean_markup("Use {{DELETE /workers/{id}}} and *bold* and [docs|http://x]") == "Use DELETE /workers/{id} and bold and docs"

    def test_empty(self):
        assert pc.extract_ac_items("") == ([], [])


class TestTrackerAwareRefs:
    def test_bitbucket_pr_never_resolves_hash_numbers_against_github_defaults(self):
        pr = _pr(title="ABC-1: fix", body="see #9 and fixes #13, also PLAT-7", headRefName="")
        cfg = {"GH_ORG": "bhatti", "GH_REPO": "todo-sample", "JIRA_BASE_URL": "https://j"}
        refs = pc.collect_issue_refs(pr, cfg, tracker="bitbucket")
        assert [r["key"] for r in refs] == ["ABC-1"] and all(r["source"] == "jira" for r in refs)  # PLAT-7 is body-only: unscoped
        assert [r["key"] for r in pc.collect_issue_refs(pr, cfg, tracker="github")][1:] == ["#13"]

    def test_test_hints_flow_into_context(self):
        pr = _pr(body="## How to test\n- curl /items\n")
        with patch.object(pc, "fetch_prs_by_numbers", return_value=[pr]), patch.object(pc, "fetch_pr_diff", return_value=("", "")), \
             patch.object(pc, "fetch_issue_details", return_value=None):
            ctx = pc.build_context(GH_URL, {})
        assert ctx["pr_test_hints"] == ["curl /items"]


class TestChangedFilesFromDiff:
    def test_spaces_quotes_deletes_renames_and_binaries(self):
        diff = (
            'diff --git a/sp ace.txt b/sp ace.txt\n--- a/sp ace.txt\n+++ b/sp ace.txt\n@@ -1 +1 @@\n-a\n+b\n'
            'diff --git "a/q\\303\\251.txt" "b/q\\303\\251.txt"\n--- "a/q\\303\\251.txt"\n+++ "b/q\\303\\251.txt"\n@@ -1 +1 @@\n-a\n+b\n'
            'diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n'
            'diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n'
            'diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n'
        )
        assert pc.changed_files_from_diff(diff) == ["sp ace.txt", "q\\303\\251.txt", "gone.py", "new.py", "img.png"]


class TestScopeAndBareNumbers:
    def test_untrusted_body_cannot_make_the_tool_fetch_other_repositories(self, tmp_path):
        pr = _pr(body="Fixes evil/private#9, closes acme/shop#7")
        with patch.object(pc, "fetch_prs_by_numbers", return_value=[pr]), patch.object(pc, "fetch_pr_diff", return_value=("", "")), \
             patch.object(pc, "fetch_issue_details", return_value=_issue()) as fetch:
            ctx = pc.build_context(GH_URL, {})
        assert [c.args[0]["repo"] for c in fetch.call_args_list] == ["acme/shop"] and ctx["ignored_refs"] == ["evil/private#9 (outside this repository)"]

    def test_bare_number_uses_the_configured_repository(self):
        with patch.object(pc, "fetch_prs_by_numbers", return_value=[_pr()]) as fetch, patch.object(pc, "fetch_pr_diff", return_value=("", "")), \
             patch.object(pc, "fetch_issue_details", return_value=None):
            ctx = pc.build_context("12", {"GH_ORG": "acme", "GH_REPO": "shop"})
        assert ctx["tracker"] == "github" and fetch.call_args.args[1] == [12]

    def test_bare_number_without_configuration_says_what_is_missing(self):
        with pytest.raises(ValueError, match="GH_ORG and GH_REPO"):
            pc.build_context("12", {})
        with pytest.raises(ValueError, match="BITBUCKET_WORKSPACE"):
            pc.build_context("12", {"DEFAULT_TRACKER": "bitbucket"})

    def test_out_directory_is_created(self, tmp_path):
        out = tmp_path / "deep" / "er" / "ctx.json"
        ctx = {"kind": "pr", "tracker": "github", "issues": [], "ac_source": "none"}
        with patch.object(pc, "load_config", return_value={}), patch.object(pc, "build_context", return_value=ctx):
            assert pc.main([GH_URL, "--out", str(out)]) == 0
        assert out.exists()

    def test_hostile_markup_finishes(self):
        import signal

        def boom(*_):
            raise TimeoutError

        old = signal.signal(signal.SIGALRM, boom)
        signal.alarm(10)
        try:
            pc.clean_markup("{{" * 40_000)
            pc.extract_ac_items("*" * 100_000 + "\n" + "- " * 50_000)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)


FAKE_ACLI = r'''#!/bin/sh
# canned acli: prints JSON by sub-command; exits 1 for anything else
args="$*"
case "$args" in
  *"bitbucket whoami"*|*"jira user me"*) echo '{"ok":true}' ;;
  *"bitbucket pr get acme widgets 7"*) cat <<'J'
{"id":7,"title":"Limit carts","description":"Fixes ABC-12\n\n## Acceptance Criteria\n- returns 409 over the limit","state":"OPEN","author":{"display_name":"Ada"},"source":{"branch":{"name":"feat/cart"}},"destination":{"branch":{"name":"main"}},"links":{"html":{"href":"https://bitbucket.org/acme/widgets/pull-requests/7"}}}
J
;;
  *"bitbucket pr diff acme widgets 7"*) printf 'diff --git a/cart.py b/cart.py\n--- a/cart.py\n+++ b/cart.py\n@@ -1 +1 @@\n-x\n+y\n' ;;
  *"jira issue get --json ABC-12"*) cat <<'J'
{"key":"ABC-12","fields":{"summary":"Cart limit","description":"## Acceptance Criteria\n- UI shows Cart limit reached","labels":["qa"]}}
J
;;
  *) echo "unexpected: $args" >&2; exit 1 ;;
esac
'''


class TestAcliFallback:
    """acli is used only when REST tokens are absent. A real executable on PATH stands in for the CLI."""

    def _install(self, tmp_path, monkeypatch, script=FAKE_ACLI):
        acli = tmp_path / "acli"
        acli.write_text(script)
        acli.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    CFG = {"BITBUCKET_WORKSPACE": "acme", "BITBUCKET_REPO": "widgets", "JIRA_PROJECT": "ABC"}
    URL = "https://bitbucket.org/acme/widgets/pull-requests/7"

    def test_pr_diff_and_linked_issue_come_through_acli_when_no_tokens(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch)
        ctx = pc.build_context(self.URL, self.CFG, tmp_path / "d")
        assert ctx["pr"]["title"] == "Limit carts" and ctx["pr"]["source_branch"] == "feat/cart" and ctx["pr"]["destination_branch"] == "main"
        assert ctx["changed_files"] == ["cart.py"] and ctx["diff_error"] == ""
        assert [i["key"] for i in ctx["issues"]] == ["ABC-12"]
        assert ctx["issues"][0]["acceptance_criteria"] == ["UI shows Cart limit reached"]
        assert "pr_description" in ctx["ac_source"]

    def test_rest_tokens_win_over_acli(self, tmp_path, monkeypatch):
        marker = tmp_path / "acli-was-called"
        self._install(tmp_path, monkeypatch, f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        cfg = {**self.CFG, "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        with patch.object(pc, "fetch_prs_by_numbers", return_value=[_pr()]) as rest, \
                patch.object(pc, "fetch_pr_diff", return_value=("", "")), patch.object(pc, "fetch_issue_details", return_value=None):
            pc.build_context(self.URL, cfg)
        assert rest.called and not marker.exists()      # with tokens, acli is never even probed

    def test_unauthenticated_acli_means_no_fallback(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch, "#!/bin/sh\necho 'not logged in' >&2; exit 1\n")
        with pytest.raises(ValueError, match="missing credentials"):
            pc.build_context(self.URL, self.CFG)

    def test_acli_that_authenticates_but_cannot_find_the_pr_is_not_found(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch, FAKE_ACLI.replace('*"bitbucket pr get acme widgets 7"*)', '*"bitbucket pr get never"*)'))
        with pytest.raises(ValueError, match="not found"):
            pc.build_context(self.URL, self.CFG)

    def test_acli_diff_failure_is_reported_not_hidden(self, tmp_path, monkeypatch):
        self._install(tmp_path, monkeypatch, FAKE_ACLI.replace('*"bitbucket pr diff acme widgets 7"*)', '*"bitbucket pr diff never"*)'))
        ctx = pc.build_context(self.URL, self.CFG, tmp_path / "d")
        assert ctx["pr"]["title"] == "Limit carts" and ctx["diff_error"].startswith("acli pr diff exit 1") and ctx["changed_files"] == []
