"""Thin integration coverage locking the additive-pass runner-only construction invariant.

The three once-per-submission additive code-review passes
(``architecture_consistency_pass``, ``side_effect_impact_pass``,
``merged_architecture_side_effect_pass``) were migrated onto the shared
``submission_pass_runner`` so budgeting, chunking, ``Agent`` construction, and
overflow recovery live in one place. This file does not re-test that
mechanics (``test_submission_pass_runner.py`` and each pass's own
"...through_public_entry_point" tests already do); it locks in the migration
invariant so a future change cannot silently reintroduce a direct ``Agent``
construction or a duplicated recovery helper in a pass module without failing
a test.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, List, Tuple

import code_review_agent.architecture_consistency_pass as arch_mod
import code_review_agent.merged_architecture_side_effect_pass as merged_mod
import code_review_agent.side_effect_impact_pass as side_mod
import code_review_agent.submission_pass_runner as runner_mod
import pytest
from code_review_agent.models import CodeReviewInput
from strands.types.exceptions import ContextWindowOverflowException

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture

_PASS_MODULES = (arch_mod, side_mod, merged_mod)

# Private helpers that own overflow detection / bisect / shrink recovery.
# These must live only on the runner -- a pass module redefining any of them
# would be exactly the duplication issue #5504 guards against.
_RECOVERY_HELPER_NAMES = (
    "_is_overflow_shaped",
    "_recover_from_overflow",
    "_shrink_items",
    "_shrink_budgets",
    "_pack_batches",
)


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        overview="Layered service architecture.",
        architecture_document="Documented boundaries between layers.",
    )


def _input(**overrides: Any) -> CodeReviewInput:
    files = overrides.pop("files", {"app/main.py": "def bar():\n    return 1\n"})
    return CodeReviewInput(files=files, task_description="wire up bar", **overrides)


# --------------------------------------------------------------------------- static checks


def test_pass_modules_do_not_import_or_construct_agent() -> None:
    """No additive pass module imports or constructs ``strands.Agent`` directly.

    The runner's own source must still contain the construction site --
    otherwise this test would pass vacuously if that site ever moved
    somewhere this grep does not look.
    """
    runner_source = inspect.getsource(runner_mod)
    assert "Agent(" in runner_source, (
        "sanity check failed: submission_pass_runner no longer appears to construct Agent anywhere"
    )

    for module in _PASS_MODULES:
        source = inspect.getsource(module)
        assert "Agent(" not in source, f"{module.__name__} must not construct Agent directly"
        assert "import Agent" not in source, f"{module.__name__} must not import strands.Agent"
        assert "strands.Agent" not in source, f"{module.__name__} must not reference strands.Agent"


def test_recovery_helpers_live_only_in_the_runner() -> None:
    """The runner's private overflow/bisect/shrink helpers are not duplicated in any pass module."""
    for name in _RECOVERY_HELPER_NAMES:
        assert getattr(runner_mod, name, None) is not None, f"runner is missing {name}"
        for module in _PASS_MODULES:
            assert getattr(module, name, None) is None, (
                f"{module.__name__} must not define its own {name}; it should come only "
                "from submission_pass_runner"
            )


# --------------------------------------------------------------------------- dynamic checks


def _agent_spy_class(reply: str, calls: List[Tuple[Any, str, list]]) -> type:
    """Build a `strands.Agent` stand-in that records construction and returns ``reply``."""

    class _Spy:
        def __init__(self, *, model: Any, system_prompt: str, tools: list) -> None:
            calls.append((model, system_prompt, tools))

        def __call__(self, prompt: str) -> str:
            return reply

    return _Spy


_ARCH_ENTRY: Callable[[Any, CodeReviewInput], Any] = (
    arch_mod.find_architecture_and_redundancy_issues
)
_SIDE_ENTRY: Callable[[Any, CodeReviewInput], Any] = side_mod.find_side_effect_impact_issues
_MERGED_ENTRY: Callable[[Any, CodeReviewInput], Any] = (
    merged_mod.find_architecture_and_side_effect_issues
)


@pytest.mark.parametrize(
    "entry_point, reply, build_input",
    [
        (_ARCH_ENTRY, '{"findings": []}', lambda: _input(architecture=_arch())),
        (_SIDE_ENTRY, '{"findings": []}', lambda: _input()),
        (
            _MERGED_ENTRY,
            '{"architecture_findings": [], "side_effect_findings": []}',
            lambda: _input(architecture=_arch()),
        ),
    ],
    ids=["architecture_consistency", "side_effect_impact", "merged_architecture_side_effect"],
)
def test_agent_construction_routes_through_the_shared_runner(
    monkeypatch: pytest.MonkeyPatch,
    entry_point: Callable[[Any, CodeReviewInput], Any],
    reply: str,
    build_input: Callable[[], CodeReviewInput],
) -> None:
    """Each pass's real public entry point constructs its Agent only via the runner's ``Agent`` symbol."""
    calls: List[Tuple[Any, str, list]] = []
    monkeypatch.setattr(runner_mod, "Agent", _agent_spy_class(reply, calls))

    entry_point(DummyLLMClient(), build_input())

    assert calls, "expected the pass to construct at least one Agent via the shared runner"


def test_overflow_recovery_reaches_the_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass's real public entry point recovers from an overflow-shaped failure via the runner.

    A thin, single overflow-recovery path through this consolidated file --
    the exhaustive bisect/shrink matrix stays owned by
    ``test_submission_pass_runner.py`` and each pass's own
    "...through_public_entry_point" test.
    """
    calls: List[Tuple[Any, str, list]] = []

    class _FlakyOnceSpy:
        def __init__(self, *, model: Any, system_prompt: str, tools: list) -> None:
            calls.append((model, system_prompt, tools))

        def __call__(self, prompt: str) -> str:
            if len(calls) == 1:
                raise ContextWindowOverflowException("simulated overflow")
            return '{"findings": []}'

    monkeypatch.setattr(runner_mod, "Agent", _FlakyOnceSpy)

    result = arch_mod.find_architecture_and_redundancy_issues(
        DummyLLMClient(), _input(architecture=_arch())
    )

    assert result == []
    assert len(calls) > 1, "expected the shared runner to retry after the simulated overflow"
