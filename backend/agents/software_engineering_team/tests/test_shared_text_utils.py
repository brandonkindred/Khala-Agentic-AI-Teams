"""Unit tests for the shared line-anchored config-section probe."""

import pytest

from software_engineering_team.shared import text_utils
from software_engineering_team.shared.text_utils import has_section_header, toml_has_section

# toml_has_section uses a real parser only when stdlib tomllib (3.11+) or the
# tomli backport is importable; on 3.10-without-tomli it falls back to the text
# scan. The parser-path assertions below assert parser behaviour, so skip them
# where no parser is present (the fallback is covered by the has_section_header
# tests above).
_TOML_AVAILABLE = text_utils._toml is not None
_toml_only = pytest.mark.skipif(not _TOML_AVAILABLE, reason="no tomllib/tomli parser available")


class TestHasSectionHeader:
    """Tests for has_section_header."""

    def test_exact_header_on_its_own_line_matches(self):
        assert has_section_header("[tool.ruff]\nline-length = 120", "[tool.ruff]") is True

    def test_header_with_leading_whitespace_matches(self):
        assert has_section_header("  [tool.ruff]\n  line-length = 120", "[tool.ruff]") is True

    def test_prefix_header_matches_longer_section(self):
        # "[tool.pytest" must cover "[tool.pytest.ini_options]".
        assert (
            has_section_header("[tool.pytest.ini_options]\nminversion = 7", "[tool.pytest") is True
        )

    def test_commented_out_header_does_not_match(self):
        assert (
            has_section_header("# [tool.ruff]\n[tool.poetry]\nname = 'app'", "[tool.ruff]") is False
        )

    def test_indented_comment_does_not_match(self):
        assert has_section_header("  # [flake8]\n[metadata]\n", "[flake8]") is False

    def test_header_inside_inline_value_string_does_not_match(self):
        # Line-anchoring means a header embedded mid-line in a value string
        # (not at line start) does not match — better than a raw substring scan.
        assert has_section_header('note = "[tool.ruff] is configured"\n', "[tool.ruff]") is False

    def test_header_at_line_start_inside_multiline_string_still_matches(self):
        # Documented residual: the probe is line-anchored, not a real parser, so
        # a header that begins a line inside a multi-line string value can still
        # match. Contrived for these section headers; the real build/lint gate
        # catches the false positive downstream.
        assert has_section_header('description = """\n[tool.ruff]\n"""\n', "[tool.ruff]") is True

    def test_blank_and_comment_only_text_returns_false(self):
        assert has_section_header("# just a comment\n\n# [flake8]\n", "[flake8]") is False

    def test_empty_text_returns_false(self):
        assert has_section_header("", "[tool.ruff]") is False

    def test_missing_header_returns_false(self):
        assert has_section_header("[metadata]\nname = app\n", "[tool.ruff]") is False


class TestTomlHasSection:
    """Tests for toml_has_section (real-parser path + text fallback)."""

    def test_real_table_present_matches_exact_header(self):
        assert toml_has_section("[tool.ruff]\nline-length = 120\n", "[tool.ruff]") is True

    def test_prefix_header_matches_nested_table(self):
        # "[tool.pytest" must cover a real [tool.pytest.ini_options] table.
        assert (
            toml_has_section("[tool.pytest.ini_options]\nminversion = 7\n", "[tool.pytest") is True
        )

    def test_absent_table_returns_false(self):
        assert toml_has_section("[tool.poetry]\nname = 'app'\n", "[tool.ruff]") is False

    def test_empty_text_returns_false(self):
        assert toml_has_section("", "[tool.ruff]") is False

    def test_invalid_toml_falls_back_to_text_scan_and_matches(self):
        # Duplicate key makes this invalid TOML, so the parse fails and we fall
        # back to the line-anchored text scan, which still sees [tool.ruff] at
        # line start.
        assert (
            toml_has_section("[tool.ruff]\nline-length = 120\nline-length = 200\n", "[tool.ruff]")
            is True
        )

    def test_invalid_toml_without_header_returns_false(self):
        # Invalid TOML (duplicate key) with no [tool.ruff] line: the text
        # fallback finds no matching header.
        assert (
            toml_has_section('[tool.poetry]\nname = "app"\nname = "other"\n', "[tool.ruff]")
            is False
        )

    @_toml_only
    def test_header_inside_multiline_string_is_not_a_false_positive(self):
        # The win over has_section_header: a [tool.ruff] line that lives inside
        # a multi-line string value is a string, not a table — the parser must
        # not report the table as present.
        text = '[tool.poetry]\nname = "app"\ndescription = """\n[tool.ruff]\n"""\n'
        assert toml_has_section(text, "[tool.ruff]") is False

    @_toml_only
    def test_real_table_alongside_multiline_string_with_header_matches(self):
        # A genuine [tool.ruff] table plus a decoy header inside a string: the
        # real table is detected (True), driven by the parser, not the decoy.
        text = (
            '[tool.ruff]\nline-length = 120\n[tool.poetry]\ndescription = """\n[tool.ruff]\n"""\n'
        )
        assert toml_has_section(text, "[tool.ruff]") is True
