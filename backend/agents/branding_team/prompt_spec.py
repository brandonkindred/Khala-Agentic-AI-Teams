"""Data-driven system-prompt spec and renderer for branding team agents.

Each ``make_*`` factory in ``branding_team.agents`` hand-writes a system
prompt that lists the Pydantic fields its ``structured_output`` model
expects. ``AgentPromptSpec`` captures that prompt as data (an opening
sentence, a numbered field list, and an optional closing sentence) and
``render_agent_prompt`` renders it back to the exact prose string, so the
prompt content lives as reviewable data instead of a hand-formatted string
literal duplicated across factories.
"""

from __future__ import annotations

from dataclasses import dataclass

_FIELD_SEPARATOR = " — "


@dataclass(frozen=True)
class PromptFieldSpec:
    """One numbered output-field line in an agent's system prompt.

    Preconditions:
        ``name`` and ``description`` are non-blank strings.
    Postconditions:
        Immutable once constructed; ``__post_init__`` has verified both
        preconditions.
    """

    name: str
    description: str

    def __post_init__(self) -> None:
        assert self.name.strip(), "PromptFieldSpec.name must be a non-blank string"
        assert self.description.strip(), "PromptFieldSpec.description must be a non-blank string"


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
        dash, U+2014), each on its own line, followed by ``spec.closing`` on
        its own trailing line when set. No trailing newline.
    """
    assert isinstance(spec, AgentPromptSpec), "spec must be an AgentPromptSpec"
    lines = [spec.opening]
    lines.extend(
        f"{index}. {field.name}{_FIELD_SEPARATOR}{field.description}"
        for index, field in enumerate(spec.fields, start=1)
    )
    if spec.closing is not None:
        lines.append(spec.closing)
    return "\n".join(lines)
