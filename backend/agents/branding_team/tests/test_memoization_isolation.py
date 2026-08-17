"""Regression guard: orchestrator.py and the conversation layer stay
unwired from the Story 2a memoization primitives (phase_input_hash,
PhaseOutputCache). Consuming them is separate follow-on work (stories 2b
and 2c) -- this test makes that boundary a structural, enforced fact
instead of a one-time assertion, so a future wiring change fails here
until this file is deliberately updated alongside it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import pytest

from branding_team import orchestrator as branding_orchestrator
from branding_team.api import conversation as api_conversation
from branding_team.api.routes import conversations as conversations_routes
from branding_team.assistant import agent as assistant_agent
from branding_team.assistant import prompts as assistant_prompts
from branding_team.assistant import store as assistant_store

_FORBIDDEN_SYMBOL_NAMES = {"phase_input_hash", "PhaseOutputCache"}
_FORBIDDEN_MODULE_SUBSTRINGS = ("memoization", "phase_output_cache")

_GUARDED_MODULES: tuple[tuple[str, ModuleType], ...] = (
    ("orchestrator.py", branding_orchestrator),
    ("api/conversation.py", api_conversation),
    ("api/routes/conversations.py", conversations_routes),
    ("assistant/agent.py", assistant_agent),
    ("assistant/store.py", assistant_store),
    ("assistant/prompts.py", assistant_prompts),
)


def _memoization_references(tree: ast.Module) -> list[str]:
    """Return a description of every memoization-primitive reference found in ``tree``."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_name = getattr(node, "module", None) or ""
            for alias in node.names:
                dotted = f"{module_name}.{alias.name}" if module_name else alias.name
                if any(sub in dotted for sub in _FORBIDDEN_MODULE_SUBSTRINGS):
                    violations.append(f"import: {dotted}")
                if alias.name in _FORBIDDEN_SYMBOL_NAMES:
                    violations.append(f"imported name: {alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_SYMBOL_NAMES:
            violations.append(f"attribute reference: {node.attr}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_SYMBOL_NAMES:
            violations.append(f"name reference: {node.id}")
    return violations


@pytest.mark.parametrize(
    "label,module", _GUARDED_MODULES, ids=[label for label, _ in _GUARDED_MODULES]
)
def test_guarded_file_does_not_reference_memoization_primitives(
    label: str, module: ModuleType
) -> None:
    """Preconditions: ``module.__file__`` is readable Python source.
    Postconditions: no import of, or reference to, ``phase_input_hash`` or
    ``PhaseOutputCache`` is found in ``module``'s source.
    """
    source_path = Path(module.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    violations = _memoization_references(tree)
    assert not violations, f"{label} must stay unwired from memoization: {violations}"
