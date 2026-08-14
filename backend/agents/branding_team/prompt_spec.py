"""Data-driven system-prompt spec and renderer for branding team agents.

Every ``make_*`` factory in ``branding_team.agents`` declares an
``AgentPromptSpec`` (an opening sentence, a numbered field list, and an
optional closing sentence). ``render_agent_prompt`` turns that spec into
the system-prompt string passed to ``build_agent``, so prompt content lives
as reviewable data instead of a hand-formatted string literal.
"""

from __future__ import annotations

from dataclasses import dataclass

_FIELD_SEPARATOR = " — "
_SUB_ITEM_INDENT = "   - "


@dataclass(frozen=True)
class PromptFieldSpec:
    """One numbered output-field line in an agent's system prompt.

    ``sub_items``, when non-empty, describes named sub-attributes of this
    field (e.g. the members of each entry in a list-of-objects field) and is
    rendered as indented bullet lines beneath the field's own line.

    Preconditions:
        ``name`` and ``description`` are non-blank strings. Every entry in
        ``sub_items``, when provided, is a non-blank string.
    Postconditions:
        Immutable once constructed; ``__post_init__`` has verified all
        preconditions.
    """

    name: str
    description: str
    sub_items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        assert self.name.strip(), "PromptFieldSpec.name must be a non-blank string"
        assert self.description.strip(), "PromptFieldSpec.description must be a non-blank string"
        assert all(item.strip() for item in self.sub_items), (
            "PromptFieldSpec.sub_items entries must be non-blank strings"
        )


@dataclass(frozen=True)
class AgentPromptSpec:
    """Data-driven description of a branding agent's system prompt.

    Preconditions:
        ``opening`` is a non-blank string. ``fields`` is non-empty. ``closing``,
        when provided, is a non-blank string.
    Postconditions:
        Immutable once constructed; ``__post_init__`` has verified all three
        preconditions.
    """

    opening: str
    fields: tuple[PromptFieldSpec, ...]
    closing: str | None = None

    def __post_init__(self) -> None:
        assert self.opening.strip(), "AgentPromptSpec.opening must be a non-blank string"
        assert self.fields, "AgentPromptSpec.fields must be non-empty"
        assert self.closing is None or self.closing.strip(), (
            "AgentPromptSpec.closing must be a non-blank string when provided"
        )


def render_agent_prompt(spec: AgentPromptSpec) -> str:
    """Render *spec* into the exact system-prompt string its fields describe.

    Preconditions:
        ``spec`` is an ``AgentPromptSpec`` instance (its own ``__post_init__``
        has already validated ``opening``/``fields``/``closing``).
    Postconditions:
        Returns ``spec.opening``, followed by one 1-indexed line per entry in
        ``spec.fields`` formatted as ``"{n}. {name} — {description}"`` (em
        dash, U+2014), each on its own line; a field with non-empty
        ``sub_items`` is followed by one further indented ``"   - {item}"``
        line per sub-item. ``spec.closing`` follows on its own trailing line
        when set. No trailing newline.
    """
    assert isinstance(spec, AgentPromptSpec), "spec must be an AgentPromptSpec"
    lines = [spec.opening]
    for index, field in enumerate(spec.fields, start=1):
        lines.append(f"{index}. {field.name}{_FIELD_SEPARATOR}{field.description}")
        lines.extend(f"{_SUB_ITEM_INDENT}{item}" for item in field.sub_items)
    if spec.closing is not None:
        lines.append(spec.closing)
    return "\n".join(lines)
