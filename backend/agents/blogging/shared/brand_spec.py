"""
Brand spec schema and loader for the blogging pipeline.

Loads and validates brand_spec.yaml as the single source of truth for
voice, formatting, readability, and content rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
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

    require_disclaimer_for: List[str] = Field(default_factory=lambda: ["medical", "legal", "financial"])


class ContentRules(BaseModel):
    """Content rules section."""

    claims_policy: ClaimsPolicy = Field(default_factory=ClaimsPolicy)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


class ExamplesConfig(BaseModel):
    """On-brand and off-brand examples."""

    on_brand: List[str] = Field(default_factory=list)
    off_brand: List[str] = Field(default_factory=list)


class BrandSpec(BaseModel):
    """Full brand spec schema."""

    brand: BrandIdentity = Field(default_factory=BrandIdentity)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    readability: ReadabilityConfig = Field(default_factory=ReadabilityConfig)
    formatting: FormattingConfig = Field(default_factory=FormattingConfig)
    content_rules: ContentRules = Field(default_factory=ContentRules)
    examples: ExamplesConfig = Field(default_factory=ExamplesConfig)
    definition_of_done: List[str] = Field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Export as dict for YAML or JSON serialization."""
        if hasattr(self, "model_dump"):
            return self.model_dump(exclude_none=True)
        return self.dict(exclude_none=True)  # Pydantic v1

    def to_prompt_summary(self) -> str:
        """Format brand spec as text for injection into agent prompts."""
        parts = [
            f"Brand: {self.brand.name}",
            f"Audience: {self.brand.audience}",
            f"Purpose: {self.brand.purpose}",
            "",
            "Voice and tone: " + ", ".join(self.voice.tone),
        ]
        if self.voice.style_notes:
            parts.append("Style notes:")
            for n in self.voice.style_notes:
                parts.append(f"  - {n}")
        if self.voice.banned_phrases:
            parts.append("")
            parts.append("Banned phrases (never use): " + ", ".join(f'"{p}"' for p in self.voice.banned_phrases))
        if self.voice.banned_patterns:
            parts.append("Banned patterns: " + ", ".join(self.voice.banned_patterns))
        parts.extend([
            "",
            f"Readability: target grade level {self.readability.target_grade_level}, max {self.readability.max_grade_level}",
            "",
            "Formatting:",
            f"  - Paragraphs: {self.formatting.min_paragraph_sentences}-{self.formatting.max_paragraph_sentences} sentences",
            f"  - No em dashes: {self.formatting.disallow_em_dash}",
            f"  - Avoid excessive bullets: {self.formatting.avoid_excessive_bullets}",
        ])
        if self.definition_of_done:
            parts.append("")
            parts.append("Definition of done:")
            for item in self.definition_of_done:
                parts.append(f"  - {item}")
        if self.examples.on_brand or self.examples.off_brand:
            parts.append("")
            parts.append("Examples (match on-brand; avoid off-brand):")
            for s in self.examples.on_brand:
                parts.append(f"  On-brand: \"{s}\"")
            for s in self.examples.off_brand:
                parts.append(f"  Off-brand (do not write like this): \"{s}\"")
        return "\n".join(parts)


def load_brand_spec(path: str | Path) -> BrandSpec:
    """
    Load and validate brand_spec.yaml from the given path.

    Args:
        path: Path to brand_spec.yaml file.

    Returns:
        Validated BrandSpec instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is invalid or fails validation.
    """
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Brand spec not found: {p}")

    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if data is None:
        data = {}

    if hasattr(BrandSpec, "model_validate"):
        return BrandSpec.model_validate(data)
    return BrandSpec.parse_obj(data)  # Pydantic v1
