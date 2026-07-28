"""``DeepthoughtWorkflow`` — deterministic orchestration of the recursive tree.

The workflow re-expresses ``DeepthoughtAgent.execute`` / ``DeepthoughtOrchestrator``
as a Temporal workflow: every LLM boundary is an activity
(:mod:`deepthought.temporal.activities`), and the recursion is driven here as
deterministic workflow code. An activity may bundle more than one sequential
provider call under a single durable boundary — e.g. ``analyse_activity``
wraps ``DeepthoughtAgent._analyse``'s two-pass reasoning-then-formatting split
(:func:`llm_service.complete_json_via_reasoning`) as one activity, not two.
Cross-cutting state that thread mode kept in shared
objects — the per-run knowledge base (dedup), the agent budget, and the event
log — lives as workflow-instance state, mutated between ``await`` points on the
single-threaded, replay-deterministic workflow event loop (so no locks are
needed).

Determinism notes:
- Agent ids come from ``workflow.uuid4()`` (never ``uuid.uuid4``).
- Fan-out is ``asyncio.gather`` over child coroutines (never a thread pool).
- The pure tree-shaping helpers come from ``deepthought.reasoning`` (no I/O), so
  the same rules run here as in thread mode.
- The cross-request ``ResultCache`` is intentionally NOT consulted here: it is
  TTL/wall-clock based and cannot run deterministically inside a workflow.
"""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import workflow

from deepthought.temporal.constants import (
    ANALYSE_ACTIVITY_OPTS,
    DECOMPOSED_PIPELINE_PATCH,
    JOB_ACTIVITY_OPTS,
    LLM_ACTIVITY_OPTS,
    RUN_PIPELINE_RETRY_POLICY,
    RUN_PIPELINE_TIMEOUT,
)

with workflow.unsafe.imports_passed_through():
    from deepthought.models import (
        AgentEvent,
        AgentEventType,
        AgentResult,
        AgentSpec,
        DecompositionStrategy,
        DeepthoughtResponse,
        KnowledgeEntry,
        QueryAnalysis,
    )
    from deepthought.reasoning import (
        ANALYSIS_KB_SUMMARY_CHARS,
        DEFAULT_AGENT_BUDGET,
        DIRECT_KB_SUMMARY_CHARS,
        build_child_specs,
        build_finding_entry,
        compute_structural_confidence,
        find_similar_entries,
        format_answer,
        render_knowledge_summary,
    )
    from deepthought.temporal import activities
    from deepthought.temporal.phase_models import (
        AnalysePayload,
        DeliberatePayload,
        ForceDirectAnswerPayload,
        SynthesisePayload,
        child_summaries,
    )

_ROOT_ROLE = (
    "General analyst who assesses complex questions and identifies "
    "what specialist knowledge is needed to provide a comprehensive answer"
)


@workflow.defn(name="DeepthoughtWorkflow")
class DeepthoughtWorkflow:
    """Durable, per-step orchestration of one deepthought run.

    Invariants:
        - ``_spawned`` never exceeds ``_budget``; each ``_run_agent`` node was
          admitted by ``_register_spawn`` exactly once.
        - ``_kb`` grows monotonically and is the sole dedup source for the run.
        - All job-store writes happen in ``start_job_activity`` /
          ``finalize_job_activity``, never in the workflow body.
    """

    def __init__(self) -> None:
        self._budget: int = DEFAULT_AGENT_BUDGET
        self._spawned: int = 0
        self._max_depth_reached: int = 0
        self._kb: list[KnowledgeEntry] = []
        self._events: list[AgentEvent] = []
        self._job_id: str = ""
        # Latched once a cancellation check returns True, so later fan-outs
        # short-circuit without re-polling the job store.
        self._cancelled: bool = False
        # Latched after the first cancellation-poll failure so a job-store outage
        # logs one warning per run instead of one per fan-out.
        self._cancel_poll_warned: bool = False

    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Execute the deepthought pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to an existing job row.
            - ``request`` is a ``DeepthoughtRequest.model_dump()`` payload.
        Postconditions:
            - Returns the ``DeepthoughtResponse`` dict on success (job COMPLETED),
              ``{}`` if the job was cancelled before it started, or re-raises after
              recording FAILED.
        """
        # Backward compatibility: histories started before the decomposition
        # recorded a single ``run_pipeline_activity`` call and must replay that
        # exact path. Only new runs (patched=True) take the decomposed path.
        if not workflow.patched(DECOMPOSED_PIPELINE_PATCH):
            return await workflow.execute_activity(
                activities.run_pipeline_activity,
                args=[job_id, request],
                start_to_close_timeout=RUN_PIPELINE_TIMEOUT,
                retry_policy=RUN_PIPELINE_RETRY_POLICY,
            )

        self._job_id = job_id
        started = await workflow.execute_activity(
            activities.start_job_activity, job_id, **JOB_ACTIVITY_OPTS
        )
        if not started:
            return {}

        try:
            strategy = await self._resolve_strategy(request)
            root_spec = AgentSpec(
                agent_id=str(workflow.uuid4()),
                name="general_analyst",
                role_description=_ROOT_ROLE,
                focus_question=request["message"],
                depth=0,
                parent_id=None,
            )
            # Count the root as the first agent (matches the orchestrator).
            self._register_spawn(root_spec)
            root_result = await self._run_agent(
                root_spec, "", request, strategy, request.get("max_depth", 10)
            )

            response = DeepthoughtResponse(
                answer=format_answer(root_result),
                agent_tree=root_result,
                total_agents_spawned=self._spawned,
                max_depth_reached=self._max_depth_reached,
                knowledge_entries=self._kb,
                events=self._events,
            ).model_dump()
        except Exception as e:  # noqa: BLE001 — record FAILED then re-raise
            await workflow.execute_activity(
                activities.finalize_job_activity,
                args=[job_id, {}, False, str(e)],
                **JOB_ACTIVITY_OPTS,
            )
            raise

        # Persist success OUTSIDE the try: the run genuinely succeeded, so a
        # failure to write COMPLETED (e.g. the response exceeds a payload limit or
        # the job service blips) must fail the workflow task for Temporal to retry
        # — it must NEVER be re-recorded by the except branch as a run failure.
        await workflow.execute_activity(
            activities.finalize_job_activity,
            args=[job_id, response, True, ""],
            **JOB_ACTIVITY_OPTS,
        )
        return response

    # ------------------------------------------------------------------
    # Strategy
    # ------------------------------------------------------------------

    async def _resolve_strategy(self, request: dict[str, Any]) -> str:
        """Use the explicit strategy if provided, else LLM-classify (AUTO)."""
        strategy = request.get("decomposition_strategy") or DecompositionStrategy.AUTO.value
        if strategy != DecompositionStrategy.AUTO.value:
            return strategy
        return await workflow.execute_activity(
            activities.classify_strategy_activity, request, **LLM_ACTIVITY_OPTS
        )

    # ------------------------------------------------------------------
    # Recursive node execution
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        spec: AgentSpec,
        parent_question: str,
        request: dict[str, Any],
        strategy: str,
        max_depth: int,
    ) -> AgentResult:
        """Deterministic re-expression of ``DeepthoughtAgent.execute`` for one node."""
        # Once cancellation has been detected anywhere in the tree, every remaining
        # node (including leaves) short-circuits on the latched flag alone — no
        # further LLM calls and no extra job-store polls.
        if self._cancelled:
            return self._cancel_node(spec)

        self._emit(spec, AgentEventType.AGENT_ANALYSING, "Analysing question")

        # Knowledge-base near-duplicate reuse (dedup). The cross-request
        # ResultCache is deliberately not consulted in Temporal mode.
        similar = find_similar_entries(self._kb, spec.focus_question)
        if similar and spec.depth > 0:
            best = max(similar, key=lambda e: e.confidence)
            self._emit(
                spec, AgentEventType.KNOWLEDGE_REUSED, f"Reusing finding from {best.agent_name}"
            )
            return AgentResult(
                agent_id=spec.agent_id,
                agent_name=spec.name,
                depth=spec.depth,
                focus_question=spec.focus_question,
                answer=best.finding,
                confidence=best.confidence,
                child_results=[],
                was_decomposed=False,
                reused_from_cache=True,
            )

        analysis = await self._analyse(spec, parent_question, request, strategy, max_depth)

        # Direct-answer path.
        if analysis.can_answer_directly or spec.depth >= max_depth:
            self._emit(spec, AgentEventType.AGENT_ANSWERING, "Answering directly")
            answer = analysis.direct_answer or ""
            if not answer and spec.depth >= max_depth:
                answer = await workflow.execute_activity(
                    activities.force_direct_answer_activity,
                    ForceDirectAnswerPayload(
                        spec=spec,
                        parent_question=parent_question,
                        original_query=request["message"],
                        knowledge_summary=render_knowledge_summary(
                            self._kb, DIRECT_KB_SUMMARY_CHARS
                        ),
                    ).model_dump(mode="json"),
                    **LLM_ACTIVITY_OPTS,
                )
            confidence = compute_structural_confidence(
                was_decomposed=False, self_assessed=analysis.confidence, child_results=[]
            )
            self._store_finding(spec, answer, confidence)
            result = AgentResult(
                agent_id=spec.agent_id,
                agent_name=spec.name,
                depth=spec.depth,
                focus_question=spec.focus_question,
                answer=answer,
                confidence=confidence,
                child_results=[],
                was_decomposed=False,
            )
            self._emit(spec, AgentEventType.AGENT_COMPLETE, "Direct answer complete")
            return result

        # Decomposition path — spawn children, deliberate, synthesise.
        self._emit(
            spec,
            AgentEventType.AGENT_DECOMPOSING,
            f"Spawning {len(analysis.skill_requirements)} specialists",
        )
        child_specs = build_child_specs(analysis.skill_requirements, spec, workflow.uuid4)
        child_results = await self._run_children(child_specs, spec, request, strategy, max_depth)

        # If cancellation latched during the fan-out, the children are already
        # truncated placeholders — skip the (wasted) deliberate + synthesise LLM
        # calls over them and short-circuit this node too.
        if self._cancelled:
            return self._cancel_node(spec)

        self._emit(spec, AgentEventType.AGENT_DELIBERATING, "Reviewing specialist results")
        deliberation_notes = ""
        if len(child_results) >= 2:
            deliberation_notes = await workflow.execute_activity(
                activities.deliberate_activity,
                DeliberatePayload(
                    spec=spec,
                    original_query=request["message"],
                    children=child_summaries(child_results),
                ).model_dump(mode="json"),
                **LLM_ACTIVITY_OPTS,
            )

        self._emit(spec, AgentEventType.AGENT_SYNTHESISING, "Synthesising results")
        synthesised = await workflow.execute_activity(
            activities.synthesise_activity,
            SynthesisePayload(
                spec=spec,
                original_query=request["message"],
                deliberation_notes=deliberation_notes,
                children=child_summaries(child_results),
            ).model_dump(mode="json"),
            **LLM_ACTIVITY_OPTS,
        )

        confidence = compute_structural_confidence(
            was_decomposed=True,
            self_assessed=0.0,
            child_results=child_results,
            deliberation_notes=deliberation_notes,
        )
        self._store_finding(spec, synthesised, confidence)
        result = AgentResult(
            agent_id=spec.agent_id,
            agent_name=spec.name,
            depth=spec.depth,
            focus_question=spec.focus_question,
            answer=synthesised,
            confidence=confidence,
            child_results=child_results,
            was_decomposed=True,
            deliberation_notes=deliberation_notes,
        )
        self._emit(spec, AgentEventType.AGENT_COMPLETE, "Synthesis complete")
        return result

    async def _analyse(
        self,
        spec: AgentSpec,
        parent_question: str,
        request: dict[str, Any],
        strategy: str,
        max_depth: int,
    ) -> QueryAnalysis:
        """Run the analyse activity and parse its result back into a model."""
        data = await workflow.execute_activity(
            activities.analyse_activity,
            AnalysePayload(
                spec=spec,
                parent_question=parent_question,
                original_query=request["message"],
                conversation_history=request.get("conversation_history", []),
                decomposition_strategy=strategy,
                knowledge_summary=render_knowledge_summary(self._kb, ANALYSIS_KB_SUMMARY_CHARS),
                max_depth=max_depth,
            ).model_dump(mode="json"),
            **ANALYSE_ACTIVITY_OPTS,
        )
        return QueryAnalysis.model_validate(data)

    async def _run_children(
        self,
        specs: list[AgentSpec],
        parent_spec: AgentSpec,
        request: dict[str, Any],
        strategy: str,
        max_depth: int,
    ) -> list[AgentResult]:
        """Run child nodes concurrently, honouring the global agent budget.

        Budget is reserved deterministically in list order (rather than raced
        across threads as in thread mode), so vetoed children are stable across
        replay. Results are returned in input order.
        """
        if not specs:
            return []

        # Stop spawning further specialists if the job was cancelled mid-run
        # (/deepthought/jobs/{id}/cancel only marks the store, so the workflow
        # cooperatively polls). One check per decomposition fan-out, latched.
        if self._cancelled or await self._is_cancelled():
            self._cancelled = True
            return [self._cancel_node(cs) for cs in specs]

        results: list[AgentResult | None] = [None] * len(specs)
        runnable: list[tuple[int, AgentSpec]] = []
        for i, cs in enumerate(specs):
            if self._register_spawn(cs):
                runnable.append((i, cs))
            else:
                results[i] = self._truncated_result(
                    cs, "Agent budget exceeded — analysis truncated."
                )

        gathered = await asyncio.gather(
            *(
                self._run_child_guarded(cs, parent_spec, request, strategy, max_depth)
                for _, cs in runnable
            )
        )
        for (i, _cs), res in zip(runnable, gathered):
            results[i] = res
        return [r for r in results if r is not None]

    async def _run_child_guarded(
        self,
        spec: AgentSpec,
        parent_spec: AgentSpec,
        request: dict[str, Any],
        strategy: str,
        max_depth: int,
    ) -> AgentResult:
        """Run one child, substituting an error result on failure.

        ``asyncio.CancelledError`` is a ``BaseException`` (not ``Exception``), so
        workflow cancellation propagates out of ``gather`` instead of being
        swallowed here. The full traceback is logged (not just the agent name) so a
        genuine bug is not silently degraded to an "Error analysing" leaf.
        """
        try:
            return await self._run_agent(
                spec, parent_spec.focus_question, request, strategy, max_depth
            )
        except Exception:
            workflow.logger.exception("Child agent %s failed", spec.name)
            return self._truncated_result(spec, f"Error analysing: {spec.focus_question}")

    async def _is_cancelled(self) -> bool:
        """Poll the job store (via activity) for cooperative cancellation, fail-open.

        Cancellation is a best-effort optimisation, so a job-store outage during the
        poll must NOT fail an otherwise-healthy run — treat any poll error as
        'not cancelled, continue'. (``asyncio.CancelledError`` is a ``BaseException``
        and still propagates, so Temporal workflow cancellation is unaffected.)
        """
        try:
            return await workflow.execute_activity(
                activities.is_cancelled_activity, self._job_id, **JOB_ACTIVITY_OPTS
            )
        except Exception:
            # Warn once per run so a sustained job-store outage doesn't emit one
            # warning per fan-out for a run it is otherwise surviving by design.
            if not self._cancel_poll_warned:
                self._cancel_poll_warned = True
                workflow.logger.warning("Cancellation poll failed; treating job as not cancelled")
            return False

    def _cancel_node(self, spec: AgentSpec) -> AgentResult:
        """Emit a cancellation event and return this node's truncated result.

        The single entry point for every cancellation short-circuit (top-of-node
        latch, post-fan-out check, and per-child in ``_run_children``), so the
        'stop work on cancel' behaviour and its audit event stay in one place.
        """
        self._emit(spec, AgentEventType.AGENT_CANCELLED, "Run cancelled")
        return self._truncated_result(spec, "Run cancelled.")

    @staticmethod
    def _truncated_result(spec: AgentSpec, answer: str) -> AgentResult:
        """Build a childless, zero-confidence result for a node that did not run."""
        return AgentResult(
            agent_id=spec.agent_id,
            agent_name=spec.name,
            depth=spec.depth,
            focus_question=spec.focus_question,
            answer=answer,
            confidence=0.0,
            child_results=[],
            was_decomposed=False,
        )

    # ------------------------------------------------------------------
    # Deterministic workflow state (budget / events / knowledge base)
    # ------------------------------------------------------------------

    def _register_spawn(self, spec: AgentSpec) -> bool:
        """Admit a new agent, or veto it when the budget is exhausted."""
        if self._spawned >= self._budget:
            self._emit(
                spec,
                AgentEventType.BUDGET_WARNING,
                f"Budget exhausted ({self._budget}), agent vetoed",
            )
            return False
        self._spawned += 1
        if spec.depth > self._max_depth_reached:
            self._max_depth_reached = spec.depth
        return True

    def _emit(self, spec: AgentSpec, event_type: AgentEventType, detail: str) -> None:
        """Append a streaming event to the run's event log (workflow state)."""
        self._events.append(
            AgentEvent(
                event_type=event_type,
                agent_id=spec.agent_id,
                agent_name=spec.name,
                depth=spec.depth,
                detail=detail,
            )
        )

    def _store_finding(self, spec: AgentSpec, answer: str, confidence: float) -> None:
        """Record this node's finding in the run knowledge base (workflow state)."""
        self._kb.append(build_finding_entry(spec, answer, confidence))
