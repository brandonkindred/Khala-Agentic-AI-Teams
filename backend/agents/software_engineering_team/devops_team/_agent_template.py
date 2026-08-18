"""Config-driven base for devops_team single-shot JSON agents.

Canonical helper decision
-------------------------
``complete_json_with_continuation`` is the canonical helper for
``devops_team``'s single-shot JSON agents.

``run_structured_persona`` (``shared/persona_agent_base.py``) remains the
pattern for the four agents already using it (``security_agent``,
``qa_agent``, ``accessibility_agent``, ``integration_team``). Switching
devops onto ``run_structured_persona`` was considered and deferred for
these reasons:

1. That helper centralizes dataclass construction via Strands
   ``structured_output_model`` and requires a ``fallback_factory`` per
   agent.
2. Several devops outputs carry nested models
   (``DevOpsCompletionPackage``, ``IaCExecutionError``, ``ReviewFinding``)
   that would need verification against Strands'
   ``structured_output_model`` before a switch.
3. The devops standardization effort only asks to standardize on one
   helper, not to migrate away from ``complete_json_with_continuation``.

Resolution status (post pipeline-framework migration)
-----------------------------------------------------
The devops per-task pipeline later moved onto ``BaseTeamLead`` / shared
phase functions. That control-flow migration did not change how
single-shot JSON agents call the LLM, so each original reason above
still applies as an intentional limitation:

1. Still applies — ``run_structured_persona`` still requires a
   ``fallback_factory`` and ``structured_output_model``; devops agents
   still build outputs in ``build_output`` from a parsed dict instead.
2. Still applies — ``DevOpsCompletionPackage``, ``IaCExecutionError``,
   and ``ReviewFinding`` remain nested models in devops outputs; they
   have not been verified against Strands' ``structured_output_model``.
3. Still applies — devops single-shot agents (including subclasses of
   ``DevOpsSingleShotAgent``) continue to call
   ``complete_json_with_continuation``; no helper migration was in
   scope for the pipeline work.

Monkeypatchability
------------------
This module imports and calls ``complete_json_with_continuation`` from
``software_engineering_team.shared.llm`` directly (no per-subclass-module
lookup). Because the import binds the name on this module, the effective
patch target is
``software_engineering_team.devops_team._agent_template.complete_json_with_continuation``,
not the attribute on ``shared.llm``. When a consumer agent is migrated onto
this base, any test that monkeypatches
``…devops_team.<agent>.agent.complete_json_with_continuation`` must retarget
the patch to
``software_engineering_team.devops_team._agent_template.complete_json_with_continuation``
(or continue patching ``shared.llm.Agent``, which fence-recovery helpers
already do via ``_patch_fenced_response``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.llm import complete_json_with_continuation


def _as_bool(value: Any) -> bool:
    """Coerce an LLM-provided flag to a strict boolean.

    JSON booleans already parse to ``bool``; this guards the common schema drift
    where a model emits the STRING ``"false"``/``"true"``. ``bool("false")`` is
    True, so a naive cast would report the flag as set when the model said the
    opposite. Only a real ``True`` or an explicit true-like string
    (``true``/``1``/``yes``, case-insensitive) counts.

    Preconditions:
        - ``value`` is arbitrary parsed-JSON content (bool, str, number, None, ...).
    Postconditions:
        - Returns a bool; anything not unambiguously true (including
          ``"false"``/``"0"``/``"no"``/None/other strings/numbers) returns False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


class DevOpsSingleShotAgent:
    """Shared scaffolding for devops single-shot JSON agents.

    Invariants:
        - Instance state is limited to ``llm`` and the resolved Strands
          ``_model``.
        - ``run`` is stateless across calls aside from that resolved model.
        - Subclasses set a non-empty ``PROMPT`` and override
          ``build_context`` / ``build_output``; ``pre_call`` may short-circuit.
    """

    PROMPT: str = ""
    PROMPT_SEPARATOR: str = "\n\n---\n\n"
    temperature: Optional[float] = 0.1
    think: Optional[bool] = True

    def __init__(self, llm_client: LLMClient) -> None:
        """Resolve the devops-routed Strands model.

        Preconditions: ``llm_client`` is not ``None`` (an ``LLMClient`` or a
        Strands ``Model``).
        Postconditions: ``self.llm`` is the passed client; ``self._model`` is
        the resolved Strands model under ``agent_key="devops"``.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def pre_call(self, input_data: Any) -> Any | None:
        """Optional early-return hook before any LLM call.

        Preconditions: ``input_data`` is whatever the subclass ``run`` accepts.
        Postconditions: returns ``None`` to continue, or a finished output to
        return from ``run`` without calling the LLM. Default: ``None``.
        """
        return None

    def build_context(self, input_data: Any) -> str:
        """Build the context string appended after the prompt separator.

        Preconditions: called only when ``pre_call`` returned ``None``.
        Postconditions: returns a string (possibly empty) concatenated into
        the LLM prompt. Subclasses must override.
        """
        raise NotImplementedError(f"{type(self).__name__}.build_context must be overridden")

    def build_output(self, input_data: Any, data: Dict[str, Any]) -> Any:
        """Construct the agent output from the parsed JSON dict.

        Preconditions: ``data`` is the dict returned by
        ``complete_json_with_continuation``.
        Postconditions: returns the subclass output object. Owns all post-call
        special cases (derived fields, secondary non-LLM objects). Subclasses
        must override.
        """
        raise NotImplementedError(f"{type(self).__name__}.build_output must be overridden")

    def run(self, input_data: Any) -> Any:
        """Run the single-shot LLM call and build the output.

        Preconditions:
            ``self.PROMPT`` is a non-empty string; ``build_context`` and
            ``build_output`` are overridden on the concrete subclass.
        Postconditions:
            If ``pre_call`` returns non-``None``, that value is returned and
            the LLM is not called. Otherwise returns
            ``build_output(input_data, data)`` where ``data`` comes from
            ``complete_json_with_continuation`` with prompt
            ``PROMPT + PROMPT_SEPARATOR + context``. ``temperature`` /
            ``think`` class attrs are passed as kwargs only when not ``None``.
            LLM/parse errors propagate unchanged.
        """
        early = self.pre_call(input_data)
        if early is not None:
            return early

        assert self.PROMPT, f"{type(self).__name__}.PROMPT must be a non-empty string"

        context = self.build_context(input_data)
        prompt = self.PROMPT + self.PROMPT_SEPARATOR + context

        kwargs: Dict[str, Any] = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.think is not None:
            kwargs["think"] = self.think

        data = complete_json_with_continuation(self._model, prompt, **kwargs)
        return self.build_output(input_data, data)
