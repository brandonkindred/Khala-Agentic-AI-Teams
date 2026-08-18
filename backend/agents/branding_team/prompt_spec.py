"""Data-driven system-prompt spec and renderer for branding team agents.

Every ``make_*`` factory in ``branding_team.agents`` declares an
``AgentPromptSpec`` (an opening sentence, a numbered field list, and an
optional closing sentence). ``render_agent_prompt`` turns that spec into
the system-prompt string passed to ``build_agent``, so prompt content lives
as reviewable data instead of a hand-formatted string literal. The numbered
field list is sourced from exactly one of: a hand-written tuple of
``PromptFieldSpec`` entries, or a bound ``structured_output`` Pydantic model
whose fields (name + ``Field(description=...)``) are read directly, so the
prompt can't drift out of sync with the schema it describes.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

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

    The numbered field list comes from exactly one of ``fields`` (a
    hand-written tuple of ``PromptFieldSpec``) or ``structured_output`` (a
    bound Pydantic model whose fields are read directly at render time).

    Preconditions:
        ``opening`` is a non-blank string. Exactly one of ``fields``
        (non-empty) or ``structured_output`` (not ``None``) is provided.
        When given, ``structured_output`` is a ``BaseModel`` subclass
        declaring at least one field. ``closing``, when provided, is a
        non-blank string.
    Postconditions:
        Immutable once constructed; ``__post_init__`` has verified all
        preconditions.
    """

    opening: str
    fields: tuple[PromptFieldSpec, ...] = ()
    structured_output: type[BaseModel] | None = None
    closing: str | None = None

    def __post_init__(self) -> None:
        assert self.opening.strip(), "AgentPromptSpec.opening must be a non-blank string"
        assert bool(self.fields) != (self.structured_output is not None), (
            "AgentPromptSpec requires exactly one of a non-empty fields tuple "
            "or a structured_output model"
        )
        if self.structured_output is not None:
            assert isinstance(self.structured_output, type) and issubclass(
                self.structured_output, BaseModel
            ), "AgentPromptSpec.structured_output must be a BaseModel subclass"
            assert self.structured_output.model_fields, (
                "AgentPromptSpec.structured_output must declare at least one field"
            )
        assert self.closing is None or self.closing.strip(), (
            "AgentPromptSpec.closing must be a non-blank string when provided"
        )


def _field_lines_from_model(model: type[BaseModel]) -> tuple[PromptFieldSpec, ...]:
    """Derive one ``PromptFieldSpec`` per field of *model*, in declaration order.

    Preconditions:
        ``model`` is a ``BaseModel`` subclass declaring at least one field
        (already verified by ``AgentPromptSpec.__post_init__``). Every field
        declares a non-blank ``Field(description=...)``.
    Postconditions:
        Returns one ``PromptFieldSpec`` per entry in ``model.model_fields``,
        in declaration order, with no ``sub_items`` (schema-derived fields
        don't carry that concept).
    """
    field_specs = []
    for name, field_info in model.model_fields.items():
        description = field_info.description
        assert description and description.strip(), (
            f"structured_output field {name!r} must declare a non-blank Field(description=...) "
            "to be rendered as a prompt line"
        )
        field_specs.append(PromptFieldSpec(name, description))
    return tuple(field_specs)


def render_agent_prompt(spec: AgentPromptSpec) -> str:
    """Render *spec* into the exact system-prompt string its fields describe.

    Preconditions:
        ``spec`` is an ``AgentPromptSpec`` instance (its own ``__post_init__``
        has already validated ``opening``/``fields``/``structured_output``/``closing``).
    Postconditions:
        Returns ``spec.opening``, followed by one 1-indexed line per output
        field — drawn directly from ``spec.fields`` when set, otherwise
        derived from ``spec.structured_output``'s Pydantic fields (name +
        ``Field(description=...)``, in declaration order) — formatted as
        ``"{n}. {name} — {description}"`` (em dash, U+2014), each on its own
        line; a field with non-empty ``sub_items`` is followed by one further
        indented ``"   - {item}"`` line per sub-item. ``spec.closing`` follows
        on its own trailing line when set. No trailing newline.
    """
    assert isinstance(spec, AgentPromptSpec), "spec must be an AgentPromptSpec"
    field_specs = spec.fields if spec.fields else _field_lines_from_model(spec.structured_output)
    lines = [spec.opening]
    for index, field in enumerate(field_specs, start=1):
        lines.append(f"{index}. {field.name}{_FIELD_SEPARATOR}{field.description}")
        lines.extend(f"{_SUB_ITEM_INDENT}{item}" for item in field.sub_items)
    if spec.closing is not None:
        lines.append(spec.closing)
    return "\n".join(lines)
