"""Declarative launch specification for team assistants.

Each team's ``TeamAssistantConfig`` optionally carries a :class:`LaunchSpec`
describing how to turn the conversation context into an HTTP request
against the team's real run endpoint. The dispatcher in
``team_assistant.launch_dispatcher`` executes the spec in-process via
``httpx.ASGITransport`` — the same pattern used by
``unified_api.slack_events_handler._call_team_assistant``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class BuiltBody:
    """Payload produced by a :class:`LaunchSpec`'s body builder.

    Exactly one of ``json`` or ``files`` is typically set. When ``files`` is
    provided the dispatcher emits a multipart POST (``data`` + ``files``);
    otherwise it emits a JSON POST.
    """

    json: dict[str, Any] | None = None
    form: dict[str, Any] | None = None
    files: dict[str, tuple[str, bytes, str]] | None = None
    path_override: str | None = None


BodyBuilder = Callable[[dict[str, Any]], BuiltBody]


@dataclass
class LaunchSpec:
    """How to dispatch a team workflow from a ready conversation context."""

    path: str
    body_builder: BodyBuilder
    method: str = "POST"
    # Teams whose run endpoint returns results synchronously (no job_id).
    # The dispatcher still returns 200 but job_id stays None and no job is linked.
    synchronous: bool = False


def declarative_builder(
    required: list[str],
    optional: list[str] | None = None,
    renames: dict[str, str] | None = None,
) -> BodyBuilder:
    """Build a JSON body by copying whitelisted context keys.

    - ``required`` keys are always copied (readiness gating already ensured
      they are present; the builder would KeyError otherwise).
    - ``optional`` keys are copied only when the context value is truthy and
      not an empty string.
    - ``renames`` remaps context keys to request keys (``context_key →
      request_key``).
    """
    optional_list: list[str] = list(optional or [])
    renames_map: dict[str, str] = dict(renames or {})

    def _build(context: dict[str, Any]) -> BuiltBody:
        body: dict[str, Any] = {}
        for key in required:
            body[renames_map.get(key, key)] = context[key]
        for key in optional_list:
            value = context.get(key)
            if value is None or value == "":
                continue
            body[renames_map.get(key, key)] = value
        return BuiltBody(json=body)

    return _build


__all__ = ["BuiltBody", "BodyBuilder", "LaunchSpec", "declarative_builder"]
