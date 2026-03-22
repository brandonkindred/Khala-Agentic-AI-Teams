"""Unit tests for deterministic validators."""


import pytest
from shared.brand_spec import BrandSpec, FormattingConfig, ReadabilityConfig, VoiceConfig
from validators.checks import (
    check_banned_patterns,
    check_banned_phrases,
    check_paragraph_length,
    check_required_sections,
)
from validators.runner import run_validators


@pytest.fixture
def brand_spec():
    """Minimal brand spec for validator tests (no YAML; prompt file is text-only)."""
    return BrandSpec(
        voice=VoiceConfig(
            banned_phrases=["delve into", "unlock the power of"],
            banned_patterns=["em_dash"],
        ),
        formatting=FormattingConfig(
            min_paragraph_sentences=2,
            max_paragraph_sentences=5,
            require_sections=True,
            required_section_headings=["Hook", "Wrap up"],
        ),
        readability=ReadabilityConfig(target_grade_level=8, max_grade_level=10),
    )


def test_check_banned_phrases_pass(brand_spec):
    draft = "This is a clean draft with no banned phrases."
    result = check_banned_phrases(draft, brand_spec)
    assert result.status == "PASS"


def test_check_banned_phrases_fail(brand_spec):
    banned = brand_spec.voice.banned_phrases or []
    if not banned:
        pytest.skip("No banned phrases in brand_spec")
    draft = f"This text contains {banned[0]} which should fail."
    result = check_banned_phrases(draft, brand_spec)
    assert result.status == "FAIL"
    assert result.details and "matches" in result.details


def test_check_banned_patterns_em_dash(brand_spec):
    draft = "This sentence—has an em dash."
    result = check_banned_patterns(draft, brand_spec)
    assert result.status == "FAIL"


def test_check_banned_patterns_pass(brand_spec):
    draft = "This sentence has no em dash. It uses a comma, instead."
    result = check_banned_patterns(draft, brand_spec)
    assert result.status == "PASS"


def test_check_paragraph_length_pass(brand_spec):
    draft = """## Hook

First sentence here. Second sentence here. Third sentence here.

## Wrap up

Final thought one. Final thought two."""
    result = check_paragraph_length(draft, brand_spec)
    assert result.status == "PASS"


def test_check_paragraph_length_fail(brand_spec):
    draft = """## Hook

One sentence only.

## Wrap up

Another single sentence."""
    result = check_paragraph_length(draft, brand_spec)
    assert result.status == "FAIL"


def test_check_required_sections_pass(brand_spec):
    required = brand_spec.formatting.required_section_headings or ["Hook", "Wrap up"]
    headings = "\n\n".join(f"# {h}\n\nContent." for h in required)
    draft = headings
    result = check_required_sections(draft, brand_spec)
    assert result.status == "PASS"


def test_check_required_sections_fail(brand_spec):
    draft = "# Introduction\n\nNo Hook or Wrap up here."
    result = check_required_sections(draft, brand_spec)
    assert result.status == "FAIL"


def test_run_validators(tmp_path):
    """Run validators with default BrandSpec (no structured file)."""
    brand_spec = BrandSpec(
        formatting=FormattingConfig(
            min_paragraph_sentences=2,
            max_paragraph_sentences=5,
            require_sections=True,
            required_section_headings=["Hook", "Wrap up"],
        ),
    )
    draft = """# Hook

This is a good opening. It has two sentences at least.

# Explain the idea

We explain things clearly. Short sentences work best. No em dashes here.

# Wrap up

We wrap up nicely. The end."""
    report = run_validators(draft, brand_spec, work_dir=tmp_path)
    assert report.status in ("PASS", "FAIL")
    assert len(report.checks) >= 5
    assert (tmp_path / "validator_report.json").exists()
