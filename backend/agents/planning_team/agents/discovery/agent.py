"""Discovery agent: LLM extraction of problem, opportunity, personas, criteria.

The brief+spec is digested via section-aware map-reduce (``spec_digest.map_reduce``);
no input is truncated. The LLM is the agent's only declared tool (§3), injected into
:meth:`DiscoveryAgent.run`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from planning_team.agents.discovery.models import DiscoveryInput, DiscoveryOutput
from planning_team.agents.discovery.prompts import build_prompt
from planning_team.models import ClientContext
from planning_team.phases._util import as_client_context, assemble_material
from planning_team.spec_digest import map_reduce
from shared_llm_recovery import extract_json_object

logger = logging.getLogger(__name__)


def _discovery_map(section: str, llm: Any, idx: int, total: int) -> Optional[Dict[str, Any]]:
    """Map step: extract discovery facts from one section of the brief+spec.

    Contract: ``llm`` must be an ``llm_service.LLMClient`` exposing
    ``complete_text(prompt, *, objective, temperature, think)`` (see
    ``llm_service.interface``); the ``think``/``objective`` kwargs are part of that
    interface and validated in tests via ``create_autospec(LLMClient)``.
    """
    response = llm.complete_text(
        build_prompt(section),
        temperature=0.0,
        think=True,
        objective=f"extract discovery facts (section {idx + 1}/{total})",
    )
    return extract_json_object(
        response,
        required_keys=(
            "problem_summary",
            "opportunity_statement",
            "target_users",
            "success_criteria",
            "tech_constraints",
            "assumptions",
        ),
    )


def _discovery_reduce(parts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce step: merge per-section discovery dicts.

    Scalars take the first non-empty value; lists are an order-preserving union with
    case-insensitive dedupe so a multi-section spec contributes all its personas /
    criteria / assumptions without duplicates.
    """
    if len(parts) == 1:
        return dict(parts[0])

    def first_str(key: str) -> str:
        # Take the first non-empty string. A non-str *scalar* (number) is coerced to
        # str so a wrong-typed-but-meaningful value isn't silently discarded; containers
        # (dict/list) are skipped since str() of them would be noise, not content.
        for p in parts:
            v = p.get(key)
            if isinstance(v, str):
                if v.strip():
                    return v
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                return str(v)
        return ""

    def union(key: str) -> list:
        seen: set = set()
        out: list = []
        for p in parts:
            raw = p.get(key)
            if not isinstance(raw, list):
                continue
            for v in raw:
                if not isinstance(v, str):
                    continue  # these fields are lists of strings; skip malformed items
                k = v.strip().lower()
                if k not in seen:
                    seen.add(k)
                    out.append(v)
        return out

    return {
        "problem_summary": first_str("problem_summary"),
        "opportunity_statement": first_str("opportunity_statement"),
        "target_users": union("target_users"),
        "success_criteria": union("success_criteria"),
        "tech_constraints": union("tech_constraints"),
        "assumptions": union("assumptions"),
    }


def _as_str_list(value: Any) -> list:
    """Coerce a discovery list field to a clean list of strings.

    Mirrors the multi-section ``_discovery_reduce.union`` filtering so a single-section
    response that returns ``null``, a non-list, or a list with non-string items cannot
    propagate a malformed value into ``ClientContext`` (whose list fields are ``List[str]``).

    Preconditions:
        - ``value`` may be anything (``None``, list, scalar, or a list of mixed types).
    Postconditions:
        - Returns a new ``list`` containing only the ``str`` items of ``value`` (empty when
          ``value`` is not a list); never raises.
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


class DiscoveryAgent:
    """Stateless agent that synthesizes a ``ClientContext`` from brief/spec material.

    Invariants:
        - Holds no mutable state; a single instance is safe to reuse across runs.
    """

    def run(self, input_data: DiscoveryInput, llm: Any) -> DiscoveryOutput:
        """Extract structured discovery facts and fold them into the client context.

        Preconditions:
            - ``llm`` is an ``llm_service.LLMClient`` (see ``_discovery_map`` contract).
            - ``input_data.client_context`` is a ``ClientContext``, dict, or ``None``.
        Postconditions:
            - Returns a ``DiscoveryOutput`` whose ``client_context`` is the prior
              context overlaid with the six discovery fields and whose ``discovery``
              is the raw reduced dict; list fields are normalized to ``list[str]``.
        """
        context = {
            "client_context": input_data.client_context,
            "initial_brief": input_data.initial_brief,
            "spec_content": input_data.spec_content,
        }
        client_context = as_client_context(context.get("client_context"))
        material = assemble_material(context)

        fallback = {
            "problem_summary": material,
            "opportunity_statement": "",
            "target_users": [],
            "success_criteria": [],
            "tech_constraints": [],
            "assumptions": ["LLM extraction failed; using raw input."],
        }
        data = map_reduce(
            material,
            llm,
            content_description="client brief and specification",
            map_fn=_discovery_map,
            reduce_fn=_discovery_reduce,
            fallback=fallback,
        )

        prev = (
            client_context.model_dump()
            if hasattr(client_context, "model_dump")
            else (client_context or {})
        )
        # Normalize the list-typed fields defensively. The multi-section reduce already
        # returns clean string lists (via ``union``), but the single-section path returns the
        # raw LLM dict, where a field may be ``null`` or a non-list (the LLM is asked to leave
        # ``tech_constraints`` empty when none apply, so ``null`` is a realistic reply). Passing
        # such a value straight into ``ClientContext`` would raise ``ValidationError`` and fail
        # the whole planning workflow.
        assumptions = list(prev.get("assumptions") or [])
        assumptions.extend(_as_str_list(data.get("assumptions")))

        merged = {
            **prev,
            "problem_summary": data.get("problem_summary"),
            "opportunity_statement": data.get("opportunity_statement"),
            "target_users": _as_str_list(data.get("target_users")),
            "success_criteria": _as_str_list(data.get("success_criteria")),
            "tech_constraints": _as_str_list(data.get("tech_constraints")),
            "assumptions": assumptions,
        }
        updated_client = ClientContext(**merged)
        return DiscoveryOutput(client_context=updated_client, discovery=data)
