"""Unit tests for the shared line-anchored config-section probe."""

from software_engineering_team.shared.text_utils import has_section_header


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
