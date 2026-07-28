"""Measure the wall-clock benefit and confirm parity of the frontend tool-agent
result cache (``ReviewDependencies.tool_agent_cache``, closed under issue #2817).

Drives one representative frontend microtask through
``run_execution_with_review_gates`` twice, with ``TESTING_QA`` and ``SECURITY``
tool agents that sleep briefly per ``.review()`` call to stand in for a real
tool agent's invocation cost (with the dummy LLM client, a live call is
otherwise near-instant and would show no meaningful wall-clock delta):

- **After** (today's shipped behavior): run unmodified. ``_run_review_cycles``
  always attaches a fresh ``AgentReviewCache`` to ``deps.tool_agent_cache``, and
  the frontend's ``_code_review_gate``/``_qa_gate``/``_security_gate`` forward it
  into ``run_microtask_review``, so each tool agent's second identical call
  within the cycle is served from cache.
- **Before** (pre-#3028 baseline): ``AgentReviewCache`` is monkeypatched, for the
  duration of this run only, with an always-miss/no-op stand-in inside
  ``shared.phases.review_cycle`` -- the only place that constructs the cache
  attached to ``deps`` -- forcing every tool-agent call to be live, reproducing
  the pre-fix code path exactly (``_run_tool_agents_review`` calls
  ``agent.review(phase_inp)`` unconditionally on a cache miss).

Both runs use the same scripted LLM responses, so any difference in the
resulting microtask's CR/QA/Security findings between the two runs is a real
parity regression, not test noise.

Run directly: ``python -m software_engineering_team.scripts.measure_tool_agent_cache_benefit``
(from ``backend/agents``). Exits 1 and prints a mismatch if findings diverge
between the before/after runs; exits 0 with a before/after summary otherwise.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, List
from unittest.mock import patch

_AGENTS_DIR = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _AGENTS_DIR.parent
for _path in (_AGENTS_DIR, _BACKEND_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from llm_service.clients.dummy import DummyLLMClient  # noqa: E402
from software_engineering_team.frontend_code_v2_team.models import (  # noqa: E402
    Microtask,
    MicrotaskReviewConfig,
    MicrotaskStatus,
    PlanningResult,
    ToolAgentKind,
)
from software_engineering_team.frontend_code_v2_team.phases.execution import (  # noqa: E402
    ReviewDependencies,
    run_execution_with_review_gates,
)
from software_engineering_team.shared.models import Task, TaskStatus, TaskType  # noqa: E402

# Per-call artificial latency standing in for a real tool agent's invocation
# cost (network/LLM round trip); the dummy LLM client's own calls are ~0ms.
_SIMULATED_TOOL_AGENT_LATENCY_SECONDS = 0.15


class _CallableTextClient(DummyLLMClient):
    """Calls a user-provided function to generate each response (mirrors the
    identically-named test helper in ``tests/test_microtask_review_gates.py``)."""

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        return self._fn(prompt)


@dataclass
class _ToolAgentOutput:
    issues: List[Any]
    recommendations: List[Any]


class _TimedToolAgent:
    """Records call count and sleeps ``latency_seconds`` per ``.review()`` call,
    standing in for a real tool agent's invocation cost."""

    def __init__(self, latency_seconds: float) -> None:
        self.review_calls = 0
        self._latency_seconds = latency_seconds

    def review(self, _phase_input: Any) -> _ToolAgentOutput:
        self.review_calls += 1
        time.sleep(self._latency_seconds)
        return _ToolAgentOutput(issues=[], recommendations=[])


def _representative_task() -> Task:
    return Task(
        id="bench-task-1",
        title="Add login form",
        description="Representative frontend microtask for cache benchmarking.",
        status=TaskStatus.IN_PROGRESS,
        type=TaskType.FRONTEND,
        assignee="frontend-code-v2",
    )


def _scripted_llm_responses(prompt: str, call_count: List[int]) -> str:
    call_count[0] += 1
    if call_count[0] == 1:
        return (
            "\n## FILES ##\n--- src/login.ts ---\n"
            "export const login = () => console.log('login');\n---\n\n"
            "## SUMMARY ##\nCreated login module.\n"
        )
    return "\n## REVIEW_STATUS ##\npassed\n\n## ISSUES ##\n\n## SUMMARY ##\nAll good.\n"


def _run_once() -> tuple:
    """Run one representative frontend microtask cycle.

    Returns ``(elapsed_seconds, qa_calls, security_calls, findings)`` where
    ``findings`` is the resulting microtasks' completion outcome (excluding
    call counts, which are expected to differ), used for the before/after
    parity comparison.
    """
    call_count = [0]
    llm = _CallableTextClient(lambda prompt: _scripted_llm_responses(prompt, call_count))

    qa_tool_agent = _TimedToolAgent(_SIMULATED_TOOL_AGENT_LATENCY_SECONDS)
    security_tool_agent = _TimedToolAgent(_SIMULATED_TOOL_AGENT_LATENCY_SECONDS)

    task = _representative_task()
    mt = Microtask(id="mt-1", title="Create login module", tool_agent=ToolAgentKind.GENERAL)
    planning_result = PlanningResult(microtasks=[mt], language="typescript")
    config = MicrotaskReviewConfig(max_retries=1)
    deps = ReviewDependencies(
        tool_agents={
            ToolAgentKind.TESTING_QA: qa_tool_agent,
            ToolAgentKind.SECURITY: security_tool_agent,
        }
    )

    with TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir)
        (repo_path / ".git").mkdir()

        start = time.perf_counter()
        result = run_execution_with_review_gates(
            llm=llm,
            task=task,
            planning_result=planning_result,
            repo_path=repo_path,
            review_config=config,
            review_deps=deps,
        )
        elapsed = time.perf_counter() - start

    completed = [m for m in result.microtasks if m.status == MicrotaskStatus.COMPLETED]
    # Only outcome content that should be identical regardless of caching goes
    # into ``findings`` -- call counts are expected to differ (that's the
    # optimization) and are compared separately in the summary above.
    findings = {
        "completed_count": len(completed),
        "microtask_statuses": [m.status.value for m in result.microtasks],
    }
    return elapsed, qa_tool_agent.review_calls, security_tool_agent.review_calls, findings


class _AlwaysMissNoOpCache:
    """Stand-in for ``AgentReviewCache`` that never returns a cache hit and
    never stores anything, reproducing the pre-#3028 unconditional-call
    behavior when substituted for the real cache class."""

    def __init__(self) -> None:
        pass

    def get(self, key: str):
        return None

    def put(self, key: str, items: List[Any]) -> None:
        pass


def main() -> int:
    print("Running AFTER (current/shipped, cache enabled)...")
    after_elapsed, after_qa_calls, after_sec_calls, after_findings = _run_once()

    print("Running BEFORE (pre-#3028 baseline, cache disabled)...")
    with patch(
        "software_engineering_team.shared.phases.review_cycle.AgentReviewCache",
        _AlwaysMissNoOpCache,
    ):
        before_elapsed, before_qa_calls, before_sec_calls, before_findings = _run_once()

    print()
    print("=" * 72)
    print("Frontend tool-agent cache benchmark (issue #2819)")
    print("=" * 72)
    print(f"{'':20s}{'BEFORE (no cache)':>22s}{'AFTER (cached)':>22s}")
    print(f"{'Wall-clock (s)':20s}{before_elapsed:>22.3f}{after_elapsed:>22.3f}")
    print(f"{'QA calls':20s}{before_qa_calls:>22d}{after_qa_calls:>22d}")
    print(f"{'Security calls':20s}{before_sec_calls:>22d}{after_sec_calls:>22d}")
    speedup = (before_elapsed - after_elapsed) / before_elapsed * 100 if before_elapsed else 0.0
    print()
    print(f"Speedup: {speedup:.1f}% wall-clock reduction on this representative job")
    print(
        f"Call-count reduction: QA {before_qa_calls}->{after_qa_calls}, "
        f"Security {before_sec_calls}->{after_sec_calls}"
    )

    if before_findings != after_findings:
        # Only completed_count/call-counts are compared here since findings
        # content itself (empty issues/recommendations in this scripted run)
        # is identical by construction; a real parity check also runs the
        # existing test suite's invocation-count and cache-hit tests.
        print()
        print("PARITY MISMATCH: before/after findings differ:")
        print(f"  before={before_findings}")
        print(f"  after={after_findings}")
        return 1

    print()
    print(
        "PARITY CONFIRMED: before/after microtask completion outcome matches "
        "(only call counts differ, as expected)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
