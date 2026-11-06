"""Tests for scripts/qa/evidence.py: evidence reset per render and safe image embedding."""

import base64

from scripts.qa import evidence


class TestReset:
    def test_removes_the_evidence_directory_and_nothing_else(self, tmp_path):
        (tmp_path / "evidence").mkdir()
        (tmp_path / "evidence" / "a.png").write_bytes(b"x")
        (tmp_path / "report.md").write_text("keep")
        evidence.reset(tmp_path)
        assert not (tmp_path / "evidence").exists() and (tmp_path / "report.md").read_text() == "keep"

    def test_missing_directory_is_fine(self, tmp_path):
        evidence.reset(tmp_path)


class TestEmbed:
    def _img(self, tmp_path, name="a.png", data=b"\x89PNGdata"):
        d = tmp_path / "evidence"
        d.mkdir(exist_ok=True)
        (d / name).write_bytes(data)

    def test_small_images_become_data_uris(self, tmp_path):
        self._img(tmp_path)
        out = evidence.embed_images('<img src="evidence/a.png" alt="x">', tmp_path)
        assert f'src="data:image/png;base64,{base64.b64encode(b"\x89PNGdata").decode()}"' in out

    def test_images_over_the_per_file_cap_stay_links(self, tmp_path):
        self._img(tmp_path, data=b"0" * 100)
        assert evidence.embed_images('<img src="evidence/a.png">', tmp_path, per_file=50) == '<img src="evidence/a.png">'

    def test_the_total_cap_stops_further_embedding(self, tmp_path):
        self._img(tmp_path, "a.png", b"0" * 60)
        self._img(tmp_path, "b.png", b"0" * 60)
        out = evidence.embed_images('<img src="evidence/a.png"><img src="evidence/b.png">', tmp_path, per_file=100, total=100)
        assert out.count("data:image") == 1 and 'src="evidence/b.png"' in out

    def test_text_files_missing_files_and_paths_outside_evidence_are_left_alone(self, tmp_path):
        self._img(tmp_path, "log.txt", b"text")
        (tmp_path / "secret.png").write_bytes(b"x")
        html = '<img src="evidence/log.txt"><img src="evidence/missing.png"><img src="evidence/../secret.png">'
        assert evidence.embed_images(html, tmp_path) == html

    def test_a_symlink_pointing_outside_evidence_is_not_inlined(self, tmp_path):
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"private")
        (tmp_path / "evidence").mkdir()
        (tmp_path / "evidence" / "link.png").symlink_to(outside)
        html = '<img src="evidence/link.png">'
        assert evidence.embed_images(html, tmp_path) == html
