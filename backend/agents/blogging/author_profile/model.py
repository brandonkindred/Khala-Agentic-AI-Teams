"""Pydantic v2 model for an author profile.

Kept intentionally permissive: every field has a sensible default so a partial profile
still validates. Templates that reference missing optional fields will fail loudly at
render time via Jinja2's StrictUndefined, which is the desired behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import BaseModel, Field


class Identity(BaseModel):
    full_name: str = ""
    short_name: str = ""
    pronouns: str = ""
    tagline: str = ""


class Professional(BaseModel):
    current_title: str = ""
    current_employer: str = ""
    past_employers: List[str] = Field(default_factory=list)
    founded_companies: List[str] = Field(default_factory=list)
    awards: List[str] = Field(default_factory=list)


class Social(BaseModel):
    medium: str = ""
    linkedin: str = ""
    github: str = ""
    twitter: str = ""
    website: str = ""
    other: dict[str, str] = Field(default_factory=dict)


class Voice(BaseModel):
    archetype: str = ""
    tone_words: List[str] = Field(default_factory=list)
    signature_phrases: List[str] = Field(default_factory=list)
    banned_phrases: List[str] = Field(default_factory=list)
    influences: List[str] = Field(default_factory=list)
    style_notes: List[str] = Field(default_factory=list)


class Background(BaseModel):
    bio: str = ""
    origin_story: str = ""
    expertise: List[str] = Field(default_factory=list)
    audiences: List[str] = Field(default_factory=list)
    notable_projects: List[str] = Field(default_factory=list)


class AuthorProfile(BaseModel):
    """Top-level user/author profile injected into agent prompts at runtime."""

    identity: Identity = Field(default_factory=Identity)
    professional: Professional = Field(default_factory=Professional)
    social: Social = Field(default_factory=Social)
    voice: Voice = Field(default_factory=Voice)
    background: Background = Field(default_factory=Background)
    extra: dict[str, object] = Field(default_factory=dict)

    @property
    def author_name(self) -> str:
        """Convenience accessor — short_name if set, else full_name, else 'the author'."""
        return self.identity.short_name or self.identity.full_name or "the author"

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> AuthorProfile:
        import yaml

        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)
