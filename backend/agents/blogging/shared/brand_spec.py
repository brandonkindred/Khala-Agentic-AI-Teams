"""
Brand spec for the blogging pipeline.

Loads the full brand prompt from brand_spec_prompt.md (plain text). No YAML;
the prompt file is read as-is and passed to draft/editor and compliance agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BrandIdentity(BaseModel):
    """Brand identity section."""

    name: str = ""
    audience: str = ""
    purpose: str = ""


class VoiceConfig(BaseModel):
    """Voice and tone configuration."""

    tone: List[str] = Field(default_factory=lambda: ["friendly", "direct", "practical"])
    style_notes: List[str] = Field(default_factory=list)
    banned_phrases: List[str] = Field(default_factory=list)
    banned_patterns: List[str] = Field(default_factory=list)


class ReadabilityConfig(BaseModel):
    """Readability targets."""

    target_grade_level: int = 8
    max_grade_level: int = 10


class FormattingConfig(BaseModel):
    """Formatting rules."""

    require_sections: bool = True
    min_paragraph_sentences: int = 2
    max_paragraph_sentences: int = 5
    prefer_short_paragraphs: bool = True
    disallow_em_dash: bool = True
    avoid_excessive_bullets: bool = True
    required_section_headings: Optional[List[str]] = None


class ClaimsPolicy(BaseModel):
    """Claims policy for factual assertions."""

    require_allowed_claims: bool = False
    require_citations_for_factual_claims: bool = True


class SafetyConfig(BaseModel):
    """Safety and disclaimer requirements."""

    require_disclaimer_for: List[str] = Field(
        default_factory=lambda: ["medical", "legal", "financial"]
    )


class ContentRules(BaseModel):
    """Content rules section."""

    claims_policy: ClaimsPolicy = Field(default_factory=ClaimsPolicy)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


class ExamplesConfig(BaseModel):
    """On-brand and off-brand examples."""

    on_brand: List[str] = Field(default_factory=list)
    off_brand: List[str] = Field(default_factory=list)


class BrandSpec(BaseModel):
    """In-memory brand spec (e.g. default/empty for validators when using prompt file only)."""

    brand: BrandIdentity = Field(default_factory=BrandIdentity)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    readability: ReadabilityConfig = Field(default_factory=ReadabilityConfig)
    formatting: FormattingConfig = Field(default_factory=FormattingConfig)
    content_rules: ContentRules = Field(default_factory=ContentRules)
    examples: ExamplesConfig = Field(default_factory=ExamplesConfig)
    definition_of_done: List[str] = Field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Export as dict for JSON serialization."""
        if hasattr(self, "model_dump"):
            return self.model_dump(exclude_none=True)
        return self.dict(exclude_none=True)  # Pydantic v1


_BRAND_SPEC_PROMPT_RELATIVE = Path("docs") / "brand_spec_prompt.md"
_DEFAULT_MIN_CONFIGURED_CHARS = 400


def brand_spec_prompt_configured(
    *,
    blogging_root: Optional[Path] = None,
    min_content_chars: int = _DEFAULT_MIN_CONFIGURED_CHARS,
) -> bool:
    """
    True when ``docs/brand_spec_prompt.md`` exists under the blogging package and has
    enough content to be treated as the canonical brand source.

    Used by the API health endpoint so the UI can hide audience/tone fields when the
    pipeline will rely on the brand spec file instead.
    """
    root = blogging_root or Path(__file__).resolve().parent.parent
    path = root / _BRAND_SPEC_PROMPT_RELATIVE
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return len(text) >= min_content_chars


def load_brand_spec_prompt(path: str | Path) -> str:
    """
    Load the full brand spec prompt from brand_spec_prompt.md.

    Reads the file as UTF-8 and returns its contents. Use this for draft/editor
    and compliance agents; validators use a default BrandSpec when only the
    prompt file is available.

    Args:
        path: Path to brand_spec_prompt.md.

    Returns:
        File contents as string (stripped).

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Brand spec prompt not found: {p}")
    return p.read_text(encoding="utf-8").strip()
