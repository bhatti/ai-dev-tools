"""Tests for scripts/common/codeowners.py (GitHub-style CODEOWNERS matching, also used for Bitbucket repos that keep the file)."""

from scripts.common import codeowners as co


def rules(text, tmp_path, where="CODEOWNERS"):
    f = tmp_path / where
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return co.load(str(tmp_path))


class TestLoad:
    def test_missing_file_or_dir(self, tmp_path):
        assert co.load(None) == [] and co.load(str(tmp_path)) == []

    def test_comments_blank_lines_and_trailing_comments_are_ignored(self, tmp_path):
        r = rules("# top\n\n*.md @docs  # trailing\n/billing/ @pay @finance\n", tmp_path)
        assert r == [("*.md", ["@docs"]), ("/billing/", ["@pay", "@finance"])]

    def test_search_order_matches_github_then_other_hosts(self, tmp_path):
        rules("* @root", tmp_path, "CODEOWNERS")
        rules("* @gh", tmp_path, ".github/CODEOWNERS")
        assert co.load(str(tmp_path)) == [("*", ["@gh"])]  # .github wins over the root file, like GitHub

    def test_other_locations(self, tmp_path):
        assert rules("* @bb", tmp_path, ".bitbucket/CODEOWNERS") == [("*", ["@bb"])]

    def test_duplicate_patterns_are_kept_in_order(self, tmp_path):
        assert [p for p, _ in rules("a/ @x\na/ @y\n", tmp_path)] == ["a/", "a/"]


class TestMatching:
    def owners(self, text, path, tmp_path):
        return co.owners_for(path, rules(text, tmp_path))

    def test_last_matching_rule_wins(self, tmp_path):
        assert self.owners("* @all\n/billing/ @pay\n", "billing/x.py", tmp_path) == ["@pay"]
        assert self.owners("/billing/ @pay\n* @all\n", "billing/x.py", tmp_path) == ["@all"]

    def test_directory_patterns(self, tmp_path):
        assert self.owners("/billing/ @pay\n", "billing/a/b.py", tmp_path) == ["@pay"]
        assert self.owners("/billing/ @pay\n", "src/billing/b.py", tmp_path) == []  # anchored
        assert self.owners("billing/ @pay\n", "src/billing/b.py", tmp_path) == ["@pay"]  # unanchored matches at any depth
        assert self.owners("/billing/ @pay\n", "billing", tmp_path) == []  # a trailing slash means a directory

    def test_extension_and_basename_patterns_match_at_any_depth(self, tmp_path):
        assert self.owners("*.proto @api\n", "a/b/c/svc.proto", tmp_path) == ["@api"]
        assert self.owners("Dockerfile @ops\n", "svc/Dockerfile", tmp_path) == ["@ops"]
        assert self.owners("*.proto @api\n", "a/svc.protox", tmp_path) == []

    def test_double_star(self, tmp_path):
        assert self.owners("/docs/**/*.md @docs\n", "docs/a/b/x.md", tmp_path) == ["@docs"]
        assert self.owners("/docs/**/*.md @docs\n", "docs/x.md", tmp_path) == ["@docs"]
        assert self.owners("/src/**/api @api\n", "src/a/b/api/h.go", tmp_path) == ["@api"]

    def test_single_star_does_not_cross_directories(self, tmp_path):
        assert self.owners("/src/*.py @core\n", "src/a.py", tmp_path) == ["@core"]
        assert self.owners("/src/*.py @core\n", "src/sub/a.py", tmp_path) == []

    def test_a_rule_without_owners_clears_ownership(self, tmp_path):
        assert self.owners("* @all\n/vendor/\n", "vendor/x.go", tmp_path) == []

    def test_regex_metacharacters_in_patterns_are_literal(self, tmp_path):
        assert self.owners("/a+b(1)/ @x\n", "a+b(1)/f", tmp_path) == ["@x"]
        assert self.owners("/a.b/ @x\n", "aXb/f", tmp_path) == []

    def test_team_name_strips_the_at_sign_and_org(self):
        assert co.team_name("@org/team-payments") == "team-payments"
        assert co.team_name("@alice") == "alice"
        assert co.team_name("dev@example.com") == "dev@example.com"
