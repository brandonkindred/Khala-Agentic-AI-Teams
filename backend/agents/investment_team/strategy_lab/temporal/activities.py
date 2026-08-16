"""Temporal activities for the Strategy Lab (fine-grained, per-side-effect).

Every activity wraps exactly one side-effecting call already made by
``StrategyLabOrchestrator`` / the strategy-lab agent classes / the
investment team's API layer: an LLM call, a sandboxed backtest execution, a
market-data fetch, or a durable-store write. Activities never re-implement
business logic — each one reconstructs the relevant Pydantic model(s) from a
JSON-shaped payload, calls the existing method verbatim, and translates the
result (and any failure) back to a wire-shaped ``dict`` / a temporalio
``ApplicationError``.

Sandbox-safety note: unlike ``workflows.py``, this module is never replayed
by the temporalio workflow sandbox (activities always run in the activity
executor), so top-level ``os.getenv`` usage would be safe here. Every
activity still uses **lazy imports** for ``investment_team``/``strategy_lab``
modules regardless, mirroring
``market_research_team/temporal/workflows.py`` — this keeps the module
(and its ``ACTIVITIES`` list) importable without pulling in the full
strategy-lab dependency graph (strands, market-data providers, ...) at
worker-process boot.

Generation fencing: ``persist_run_state_activity`` and
``finalize_cycle_record_activity`` are the two ``@activity.defn``-decorated
activities that write durable state tied to a run, so they're the two that
check a fencing token (``shared.fencing.check_fencing_token``) before
writing. A restart mints a new "generation" for the fresh incarnation it
dispatches (``investment_team.api.main.restart_strategy_lab_run``); a write
carrying an older generation than the run's current persisted one is
rejected — closing the window where an already-dispatched, non-heartbeating
activity from a just-terminated workflow finishes *after* a restart and
silently commits stale progress or a stale cycle record. This is honestly a
check-then-write, not an atomic compare-and-swap: the fencing read and the
eventual write are two separate job-service calls, so a restart racing
exactly between them is (rarely) still possible.

A third fencing-checked write path exists, deliberately *not* an
``@activity.defn`` (``ADR-012``,
``system_design/adr/ADR-012-strategy-lab-design-attempt-checkpoint-contract.md``):
``persist_design_attempt_checkpoint``, called inline from inside
``run_design_attempt_activity``'s own execution at the design/synthesis
boundary. It reuses the same ``_check_generation_fencing`` helper and the
same check-then-write non-atomicity accounting as the two paragraphs above.

That "rarely" claim only holds for ``persist_run_state_activity``, whose
check sits immediately adjacent to its (fast, synchronous) write.
``finalize_cycle_record_activity`` is different: its write happens deep
inside ``_finalize_strategy_lab_cycle_record``, after a market-data fetch
and a paper-trading execution that can take a real amount of time — a
substantially wider window for a restart to land in between the check and
the write. It checks BOTH before and after that call (the second check
can't undo an already-committed write, but it stops the surrounding
workflow from trusting a result that raced a restart) — this narrows, but
does not fully close, that specific activity's window. A genuinely atomic
conditional write against the generation would require the shared
record-persistence layer (used verbatim by thread mode too) to become
generation-aware, which is out of scope here. Neither check stops the
stale activity's *computation* itself, which can still run to completion
(burning time/cost) before its write is checked/rejected; cooperative
cancellation would close both remaining gaps (the wasted compute AND
``finalize_cycle_record_activity``'s wider write window, by stopping
execution outright once terminated) and is tracked as a separate,
deliberately deferred optimization.

Cooperative cancellation for ``run_design_attempt_activity`` specifically is
no longer deferred: it heartbeats via a background thread
(``shared.concurrency.BackgroundHeartbeat``) while ``_run_design_attempt``
runs, and checks ``activity.is_cancelled()`` at every ``emit`` checkpoint
threaded through that pipeline (design/synthesis/refinement/alignment/
verification/analysis), exiting via ``CancelledError`` as soon as the owning
workflow is terminated/cancelled. This closes the wasted-compute half of the
gap for that one activity; ``finalize_cycle_record_activity`` (no
phase-granular checkpoint hook to reuse, and a fencing-checked write path
this change deliberately does not touch) is unchanged and still covered by
the paragraph above.

More honest edges: the fencing checks read via ``run_state.
get_run_generation_strict``, which fails CLOSED (raises, rejecting the
write) on a transient durable-read failure rather than defaulting to the
most permissive generation — a lenient default there would let a read
failure mask a genuinely higher current generation. That raised lookup
failure is kept RETRYABLE for a check with nothing committed yet (only an
actual ``StaleFencingTokenError`` is non-retryable there), so a momentary
job-service outage doesn't permanently fail the workflow — EXCEPT
``finalize_cycle_record_activity``'s post-check, whose lookup failure is
non-retryable AT THE TEMPORAL LEVEL only once a few bounded local retries
(``_POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS``) have already failed: by that
point the write already committed, so a Temporal-level retry of the WHOLE
ACTIVITY would re-execute ``_finalize_strategy_lab_cycle_record``'s
non-idempotent side effects (a fresh paper-trading session, orphaning the
first) a second time — but a local retry of just the cheap read doesn't
re-trigger that write at all, so it absorbs a momentary blip for free before
that non-retryable fallback ever applies.

A run created before generation fencing shipped has no persisted
``generation`` field at all; its first post-upgrade restart mints
generation 2 rather than 1 (see ``restart_strategy_lab_run``), since 1 is
also what a caller that omits ``generation`` entirely (a pre-upgrade
in-flight activity) is treated as presenting, and equal tokens are accepted
by ``check_fencing_token``. And ``finalize_cycle_record_activity`` recovers
a missing ``run_id`` from the activity's own Temporal ``workflow_id``
(``_infer_run_id_from_activity_context``) rather than skipping fencing
outright — a pre-upgrade in-flight activity's *payload* predates ``run_id``,
but the workflow_id it's executing under does not.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from shared.concurrency.heartbeat import BackgroundHeartbeat
from shared.temporal.activity_utils import is_cancelled

if TYPE_CHECKING:
    # Type-checking only -- runtime resolution stays lazy (see module
    # docstring) since this import pulls in the full strategy-lab dependency
    # graph and this file's own ACTIVITIES list must stay importable without it.
    from investment_team.models import DesignAttemptCheckpoint
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext

logger = logging.getLogger(__name__)


class _DesignAttemptCancelled(BaseException):
    """Internal signal: cancellation observed at an ``emit`` checkpoint.

    Deliberately a ``BaseException`` (not ``Exception``) subclass -- the same
    reason ``asyncio.CancelledError`` moved to ``BaseException`` in Python
    3.8 -- so it survives every ``except Exception`` (and narrower) handler
    inside the design-attempt phase pipeline instead of being silently
    swallowed as an ordinary failure (e.g. the walk-forward evaluation
    fallback in ``orchestrator_verification.py``). Caught and converted to
    ``temporalio.exceptions.CancelledError`` only at
    ``run_design_attempt_activity``'s own outer boundary, immediately below.
    """


# Local, in-process retry delays (seconds) for finalize_cycle_record_activity's
# post-write fencing check's durable-generation read. Empty for every other
# fencing check (nothing committed yet there, so Temporal's own activity-level
# retry already safely covers a lookup failure by re-running the whole,
# side-effect-free-so-far activity). This one is different: by the time it
# runs, _finalize_strategy_lab_cycle_record has already durably committed, so
# a Temporal-level retry of the whole activity would re-execute that
# non-idempotent write a second time -- a bounded LOCAL retry of just the
# cheap read absorbs a momentary job-service blip without ever re-triggering
# the write, sidestepping that concern entirely.
_POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS: Tuple[float, ...] = (0.5, 1.0)

# Job-record field-name prefix for a design-attempt checkpoint, namespaced by
# DesignAttemptCheckpoint.cycle_scope. _persist_run_state's underlying
# update_job does a FIELD-level partial merge (not a deep merge), so giving
# each concurrently-running cycle its own field name -- rather than sharing
# one "design_attempt_checkpoint" field -- is what prevents two cycles in the
# same wave (StrategyLabBatchWorkflow's max_parallel) from clobbering each
# other's checkpoint.
_DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX = "design_attempt_checkpoint:"


def _map_exception_to_application_error(exc: Exception) -> ApplicationError:
    """Translate an exception raised inside an activity body into an ``ApplicationError``.

    Preconditions:
        ``exc`` was raised by a strategy-lab agent-class method invoked from
        within an activity.
    Postconditions:
        Returns (does not raise) an ``ApplicationError``. ``non_retryable`` is
        ``True`` when ``exc`` is a ``StrategyLabLLMError`` whose ``outcome``
        is ``"fatal"`` (mirrors ``classify_strands_exception``'s fatal
        classification — retrying the same call cannot help) or any
        non-LLM parse/validation failure raised directly by an agent method
        (also not resolved by a bare retry). ``non_retryable`` is ``False``
        only for a ``StrategyLabLLMError`` whose ``outcome`` is
        ``"exhausted"`` or ``"budget_exhausted"`` — the in-activity envelope
        already spent its own retry budget, so a bounded extra
        Temporal-level attempt only helps recover from a genuine worker
        crash mid-envelope, not re-run the whole backoff loop.
    """
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    if isinstance(exc, StrategyLabLLMError):
        non_retryable = exc.outcome == "fatal"
        return ApplicationError(
            str(exc), type=exc.outcome or "StrategyLabLLMError", non_retryable=non_retryable
        )
    return ApplicationError(
        f"{type(exc).__name__}: {exc}", type=type(exc).__name__, non_retryable=True
    )


def _infer_run_id_from_activity_context() -> Optional[str]:
    """Best-effort fallback: recover run_id from the current activity's Temporal
    workflow_id when a caller's payload predates the run_id field.

    ``StrategyLabBatchWorkflow`` always dispatches under the deterministic
    workflow id ``f"{WORKFLOW_ID_PREFIX}{run_id}"`` (``strategy_lab.temporal.
    start_workflow.start_strategy_lab_batch_workflow``), so run_id is
    recoverable from the activity execution context even for a
    ``strategy_lab_finalize_cycle_record`` task Temporal scheduled before the
    run_id parameter existed — its recorded input can't carry a key that
    didn't exist yet, but the workflow_id it's running under is unaffected by
    that and is available regardless.

    Preconditions:
        None.
    Postconditions:
        Returns the recovered run_id, or ``None`` when there is no current
        activity execution context (e.g. a direct, non-Temporal call — this
        codebase's own test suite calls activities as plain Python functions)
        or the workflow_id doesn't match the expected prefix. Never raises.
    """
    try:
        workflow_id = activity.info().workflow_id
    except Exception:
        return None
    from investment_team.strategy_lab.temporal import WORKFLOW_ID_PREFIX

    if workflow_id and workflow_id.startswith(WORKFLOW_ID_PREFIX):
        return workflow_id[len(WORKFLOW_ID_PREFIX) :]
    return None


def _infer_cycle_scope_from_activity_context() -> Optional[str]:
    """Best-effort: the current activity's own Temporal workflow_id, used as
    an opaque per-cycle correlation id for design-attempt checkpoint scoping.

    ``StrategyLabCycleWorkflow`` child workflows are started under the
    deterministic id ``f"{run_id}-c{cycle_index}"``
    (``temporal/workflows.py``'s wave-start loop); every activity dispatched
    from inside that child workflow -- including ``run_design_attempt_activity``
    -- runs under that same ``workflow_id``. Returning it whole (never
    parsed/sliced, unlike ``_infer_run_id_from_activity_context`` above) is
    enough to disambiguate two cycles racing ``run_design_attempt_activity``
    at ``design_attempt=0`` concurrently on the same ``run_id`` within one
    wave (``StrategyLabBatchWorkflow.run``'s ``max_parallel`` wave-start
    loop).

    Preconditions:
        None.
    Postconditions:
        Returns the current ``workflow_id``, or ``None`` when there is no
        current activity execution context (e.g. a direct, non-Temporal
        call -- this codebase's own test suite calls activities as plain
        Python functions). Never raises.
    """
    try:
        return activity.info().workflow_id
    except Exception:
        return None


def _check_generation_fencing(
    run_id: str,
    provided_generation: int,
    *,
    retry_on_lookup_failure: bool,
    lookup_retry_delays: Tuple[float, ...] = (),
) -> None:
    """Raise unless ``provided_generation`` is current or newer than ``run_id``'s
    durable generation. Shared by ``persist_run_state_activity`` and
    ``finalize_cycle_record_activity``, whose fencing checks are otherwise
    identical apart from the retryability of a lookup failure.

    Preconditions:
        ``run_id`` names a strategy-lab run; ``provided_generation`` is the
        fencing generation the calling activity was dispatched with.
        ``retry_on_lookup_failure`` is ``True`` when nothing has been written
        yet (safe to retry the whole activity on a transient durable-read
        failure) and ``False`` once a non-idempotent write may already have
        committed (a retry would re-execute it). ``lookup_retry_delays`` is a
        tuple of local, in-process retry delays (seconds) applied to a
        transient durable-read failure before giving up -- empty for a
        pre-write check (the whole activity is already safely Temporal-retryable
        on a single lookup failure, so a local retry adds nothing), non-empty
        for a post-write check (a bounded local retry of just the read
        absorbs a momentary job-service blip without ever re-triggering the
        non-idempotent write a Temporal-level activity retry would).
    Postconditions:
        Returns normally when ``provided_generation`` is current or newer.
        Raises ``ApplicationError(type="StaleFencingTokenError",
        non_retryable=True)`` when it's stale — not handled via
        ``_map_exception_to_application_error``, since that helper's
        documented precondition is an exception raised by a strategy-lab
        agent-class method, which ``check_fencing_token`` is not. The durable
        lookup is retried locally up to ``len(lookup_retry_delays)`` more
        times (sleeping the corresponding delay between attempts) before a
        persisting failure raises ``ApplicationError`` with
        ``non_retryable=(not retry_on_lookup_failure)``. Any other exception
        from ``check_fencing_token`` (a caller precondition violation, e.g. a
        non-int ``provided_generation`` — pure comparison, no I/O, so this is
        the only other failure mode) is also raised non-retryable, preserving
        the original exception's type name rather than conflating it with a
        stale token.
    """
    from investment_team.strategy_lab.run_state import get_run_generation_strict
    from shared.fencing import StaleFencingTokenError, check_fencing_token

    current_generation: Optional[int] = None
    lookup_exc: Optional[Exception] = None
    for attempt in range(len(lookup_retry_delays) + 1):
        try:
            current_generation = get_run_generation_strict(run_id)
            lookup_exc = None
            break
        except Exception as exc:  # noqa: BLE001
            lookup_exc = exc
            if attempt < len(lookup_retry_delays):
                time.sleep(lookup_retry_delays[attempt])
    if lookup_exc is not None:
        raise ApplicationError(
            f"{type(lookup_exc).__name__}: {lookup_exc}",
            type=type(lookup_exc).__name__,
            non_retryable=not retry_on_lookup_failure,
        ) from lookup_exc
    try:
        check_fencing_token(
            agent_id=run_id,
            resource="strategy_lab_run",
            provided_token=provided_generation,
            current_token=current_generation,
        )
    except StaleFencingTokenError as exc:
        raise ApplicationError(str(exc), type="StaleFencingTokenError", non_retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}", type=type(exc).__name__, non_retryable=True
        ) from exc


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_compute_regime_summary")
def compute_regime_summary_activity() -> Optional[Dict[str, Any]]:
    """Derive the current market-regime summary for the designer prompt.

    Preconditions:
        None.
    Postconditions:
        Returns the resulting ``RegimeSummary``'s JSON dump. The caller (the
        cycle workflow) is responsible for the
        ``STRATEGY_LAB_REGIME_SUMMARY_ENABLED`` on/off gate — a resolved,
        workflow-input flag, not an env read inside this activity —
        and for skipping this activity entirely when the feature is
        disabled, mirroring ``StrategyLabOrchestrator._compute_regime_summary``.
        ``compute_regime_summary`` is itself fail-open (never raises; a
        degraded summary is returned instead), so this activity only raises
        ``ApplicationError`` on a genuinely unexpected exception.
    """
    from datetime import datetime, timezone

    from investment_team.market_data_service import MarketDataService
    from investment_team.strategy_lab.market_regime import compute_regime_summary

    service = MarketDataService()
    try:
        summary = compute_regime_summary(
            service.fetch_ohlcv,
            computed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return summary.model_dump(mode="json")


@activity.defn(name="strategy_lab_resolve_workflow_config")
def resolve_workflow_config_activity() -> Dict[str, Any]:
    """Resolve every env var the cycle-workflow control flow needs, once.

    Preconditions:
        None.
    Postconditions:
        Returns ``{"design_review_rounds": int, "design_review_stall_rounds":
        int, "mechanical_repair_enabled": bool, "code_conformance_retries":
        int, "design_max_llm_calls": int, "regime_summary_enabled": bool,
        "max_design_reentries": int}``.
        A workflow may never read these env vars itself (``os.*`` is
        restricted at workflow runtime by the temporalio sandbox) — it calls
        this activity once and threads the resolved values through
        ``cycle_input`` instead of re-reading env vars in any loop body.
        ``max_design_reentries`` is a plain module constant (not env-derived);
        it is surfaced here so the workflow need never import
        ``orchestrator`` (and its heavy transitive graph) into the sandbox.
    """
    from investment_team.strategy_lab.orchestrator import (
        MAX_DESIGN_REENTRIES,
        _design_max_llm_calls,
        _design_review_rounds,
        _design_review_stall_rounds,
        _env_flag,
        _mechanical_repair_enabled,
    )
    from investment_team.strategy_lab.quality_gates.predicate_conformance import (
        _code_conformance_retries,
    )

    return {
        "design_review_rounds": _design_review_rounds(),
        "design_review_stall_rounds": _design_review_stall_rounds(),
        "mechanical_repair_enabled": _mechanical_repair_enabled(),
        "code_conformance_retries": _code_conformance_retries(),
        "design_max_llm_calls": _design_max_llm_calls(),
        "regime_summary_enabled": _env_flag("STRATEGY_LAB_REGIME_SUMMARY_ENABLED"),
        "max_design_reentries": MAX_DESIGN_REENTRIES,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_persist_run_state")
def persist_run_state_activity(
    run_id: str, state: dict, create: bool = False, generation: int = 1
) -> None:
    """Persist strategy-lab run/batch progress to the durable job store.

    Preconditions:
        ``run_id`` is a non-empty run identifier; ``state`` is a JSON-shaped
        dict of run-state fields; ``generation`` is the fencing generation
        the calling workflow incarnation was dispatched with (default ``1``
        for backward compatibility with a workflow-history replay predating
        this parameter).
    Postconditions:
        Checks ``run_id``'s fencing token first (see ``shared.fencing.
        check_fencing_token``): raises a non-retryable ``ApplicationError``
        instead of writing when ``generation`` is older than the run's
        current persisted generation — a later restart has already minted a
        newer one, so this write belongs to a superseded incarnation and
        must not land. Otherwise delegates to ``investment_team.strategy_lab.orchestrator_api.
        _persist_run_state`` verbatim, which now propagates any job-service
        failure rather than swallowing it -- letting this activity raise
        too, so its caller's Temporal retry policy (``_ACTIVITY_RETRY`` in
        ``strategy_lab/temporal/workflows.py``) can retry the durable write.
        If retries are exhausted, the workflow fails visibly instead of
        silently continuing with an unpersisted run state.

        Not a fully atomic check-then-write: the fencing read and the
        eventual write are two separate job-service calls, so a restart
        racing exactly between them is (rarely) still possible. This closes
        the realistic window — the prior workflow is already confirmed
        terminated before a restart mints its new generation — not a
        mathematically perfect one. The fencing read itself fails CLOSED:
        a transient durable-read failure raises (via ``get_run_generation_strict``)
        rather than silently defaulting to the most permissive generation,
        which could otherwise mask a genuinely higher current generation and
        let a stale write land — but that raised lookup failure is kept
        RETRYABLE (only an actual ``StaleFencingTokenError`` is marked
        non-retryable), since a transient job-service outage should let
        Temporal's retry policy wait for the store to recover rather than
        permanently failing the workflow over a momentary blip.
    """
    from investment_team.strategy_lab.orchestrator_api import _persist_run_state

    # Nothing has been written yet, so a lookup failure is safe to retry.
    _check_generation_fencing(run_id, generation, retry_on_lookup_failure=True)
    _persist_run_state(run_id, state, create=create)


def persist_design_attempt_checkpoint(checkpoint: DesignAttemptCheckpoint) -> None:
    """Durably persist one design attempt's Phase 1 output at the design/synthesis boundary.

    Deliberately **not** a Temporal activity: no ``@activity.defn`` decorator,
    and not registered in ``ACTIVITIES``/``__all__``. Per ``ADR-012``
    (``system_design/adr/ADR-012-strategy-lab-design-attempt-checkpoint-contract.md``,
    "Where the write happens"), the parent epic rules out decomposing the
    design attempt into per-phase Temporal activities, and activities cannot
    invoke other activities in any case. This is instead a plain synchronous
    durable-store call, invoked inline from inside
    ``run_design_attempt_activity``'s own execution via a checkpoint-write
    callback threaded through ``_run_design_attempt``. Deliberately does not
    call ``activity.info()`` itself (see
    ``_infer_cycle_scope_from_activity_context``) -- ``checkpoint.cycle_scope``
    is recovered once, centrally, by the caller, keeping this function pure
    and trivially unit-testable without mocking Temporal activity context.

    Preconditions:
        ``checkpoint.generation`` is the fencing generation the calling
        workflow incarnation was dispatched with; ``checkpoint.cycle_scope``
        is the caller's own recovered per-cycle correlation id.
    Postconditions:
        Checks ``checkpoint.run_id``'s fencing token first, exactly like
        ``persist_run_state_activity`` (see that function's docstring for the
        full non-atomicity/fail-closed rationale, which applies verbatim
        here): raises a non-retryable ``ApplicationError`` instead of writing
        when ``checkpoint.generation`` is older than the run's current
        persisted generation. Otherwise persists
        ``checkpoint.model_dump(mode="json")`` under the
        ``f"{_DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}{checkpoint.cycle_scope}"``
        field of ``checkpoint.run_id``'s job record via ``_persist_run_state``,
        whose partial-merge write leaves every other field on that record
        (including a different cycle's own checkpoint field) untouched.
    """
    from investment_team.strategy_lab.orchestrator_api import _persist_run_state

    # Nothing has been written yet, so a lookup failure is safe to retry --
    # same rationale as persist_run_state_activity's pre-write check.
    _check_generation_fencing(
        checkpoint.run_id, checkpoint.generation, retry_on_lookup_failure=True
    )
    _persist_run_state(
        checkpoint.run_id,
        {
            f"{_DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}{checkpoint.cycle_scope}": checkpoint.model_dump(
                mode="json"
            )
        },
    )


def load_design_attempt_checkpoint(
    run_id: str, cycle_scope: Optional[str], design_attempt: int
) -> Optional["DesignAttemptCheckpoint"]:
    """Look up a valid design-attempt checkpoint for ``(run_id, cycle_scope, design_attempt)``.

    Preconditions:
        ``run_id`` names a strategy-lab run (may not exist). ``cycle_scope``
        is the caller's own recovered per-cycle correlation id (see
        ``_infer_cycle_scope_from_activity_context``), or ``None`` when
        recovery failed (e.g. outside a real Temporal activity context).
    Postconditions:
        Returns ``None`` -- never raises -- for every "no usable checkpoint"
        outcome: ``cycle_scope`` is ``None``; no job record for ``run_id``;
        no field for this ``cycle_scope``; the persisted blob fails
        ``DesignAttemptCheckpoint`` validation; the checkpoint's own
        ``run_id``/``cycle_scope``/``design_attempt`` don't match the
        caller's (defense in depth -- the field-name scoping should already
        guarantee this); or the checkpoint's ``generation`` doesn't exactly
        match the run's current persisted generation (a checkpoint minted
        under an older -- or, defensively, a newer -- generation is stale;
        generations only ever increase, so exact match is the correct
        comparison for a boundary-time snapshot). Otherwise returns the
        validated ``DesignAttemptCheckpoint``.

        Deliberately fails OPEN on a current-generation lookup failure or a
        durable-read failure -- unlike ``get_run_generation_strict``'s own
        fail-CLOSED contract and unlike ``persist_design_attempt_checkpoint``'s
        write-side check. A checkpoint READ is a pure optimization on a path
        where nothing has been mutated yet (no Phase 1 LLM call has been
        made at this point in ``run_design_attempt_activity``): the worst
        case of proceeding as "no checkpoint found" is one unnecessary full
        re-run of Phase 1, never a correctness violation -- whereas raising
        here would burn one of only ``_ACTIVITY_RETRY.maximum_attempts=2``
        Temporal-level attempts on an ancillary read having nothing to do
        with the design work that budget exists to protect.

        Reads the durable job store directly (``run_state.
        load_run_from_job_service``), bypassing ``active_runs``, for the same
        cross-process reason ``get_run_generation_strict`` does: this runs
        inside a Temporal worker process, not the API-server process that
        owns ``active_runs``.
    """
    if not cycle_scope:
        return None
    from investment_team.models import DesignAttemptCheckpoint
    from investment_team.strategy_lab.run_state import (
        get_run_generation_strict,
        load_run_from_job_service,
    )

    try:
        state = load_run_from_job_service(run_id)
    except Exception:  # noqa: BLE001 -- fail open, see docstring
        return None
    if not state:
        return None
    raw = state.get(f"{_DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}{cycle_scope}")
    if not raw:
        return None
    try:
        checkpoint = DesignAttemptCheckpoint.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None
    if (
        checkpoint.run_id != run_id
        or checkpoint.cycle_scope != cycle_scope
        or checkpoint.design_attempt != design_attempt
    ):
        return None
    try:
        current_generation = get_run_generation_strict(run_id)
    except Exception:  # noqa: BLE001 -- fail open, see docstring
        return None
    if checkpoint.generation != current_generation:
        return None
    return checkpoint


_DESIGN_CONTEXT_WIRE_KEYS = ("rounds", "critiques", "stop_reason", "loop_telemetry")


def _design_context_to_wire(
    design_context: Optional["_DesignPersistContext"],
) -> Optional[Dict[str, Any]]:
    """JSON-shape a ``_DesignPersistContext`` for a wire outcome / checkpoint payload.

    Preconditions:
        None.
    Postconditions:
        Returns ``None`` when ``design_context`` is ``None``, else a dict
        with keys ``rounds``, ``critiques``, ``stop_reason``, and
        ``loop_telemetry``, with every ``SpecCritique`` in ``critiques``
        dumped to a JSON-shaped dict.
    """
    if design_context is None:
        return None
    return {
        "rounds": design_context.rounds,
        "critiques": [c.model_dump(mode="json") for c in (design_context.critiques or [])],
        "stop_reason": design_context.stop_reason,
        "loop_telemetry": design_context.loop_telemetry,
    }


def _design_context_from_wire(data: Optional[Dict[str, Any]]) -> Optional["_DesignPersistContext"]:
    """Inverse of :func:`_design_context_to_wire` -- reconstructs real ``SpecCritique`` objects.

    Preconditions:
        ``data`` is ``None`` or a dict. A nonempty dict must contain every
        key produced by :func:`_design_context_to_wire` with the matching
        types (``rounds`` int, ``critiques`` list, ``stop_reason`` str,
        ``loop_telemetry`` dict).
    Postconditions:
        Returns ``None`` when ``data`` is ``None``/empty. Otherwise returns a
        ``_DesignPersistContext`` with ``critiques`` rebuilt as real
        ``SpecCritique`` instances -- not left as plain dicts, which would
        raise ``AttributeError`` deep inside record assembly
        (``orchestrator_record_assembly.py`` calls ``.model_dump()`` on each
        ``design_context.critiques`` element). Raises ``ValueError`` /
        ``TypeError`` when a nonempty payload is missing keys or has the
        wrong shape, so checkpoint resume can fail open instead of
        fabricating default audit fields.
    """
    if not data:
        return None
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.agents.design_review import SpecCritique

    missing = [key for key in _DESIGN_CONTEXT_WIRE_KEYS if key not in data]
    if missing:
        raise ValueError(f"design_context missing wire fields: {missing}")
    rounds = data["rounds"]
    critiques = data["critiques"]
    stop_reason = data["stop_reason"]
    loop_telemetry = data["loop_telemetry"]
    if isinstance(rounds, bool) or not isinstance(rounds, int):
        raise TypeError(f"design_context.rounds must be int, got {type(rounds).__name__}")
    if not isinstance(critiques, list):
        raise TypeError(f"design_context.critiques must be list, got {type(critiques).__name__}")
    if not isinstance(stop_reason, str):
        raise TypeError(f"design_context.stop_reason must be str, got {type(stop_reason).__name__}")
    if not isinstance(loop_telemetry, dict):
        raise TypeError(
            f"design_context.loop_telemetry must be dict, got {type(loop_telemetry).__name__}"
        )
    return _DesignPersistContext(
        rounds=rounds,
        critiques=[SpecCritique.model_validate(c) for c in critiques],
        stop_reason=stop_reason,
        loop_telemetry=dict(loop_telemetry),
    )


@activity.defn(name="strategy_lab_snapshot_prior_records")
def snapshot_prior_records_activity(reverse: bool = False) -> List[Dict[str, Any]]:
    """Read the durable strategy-lab record store, sorted by creation time.

    Preconditions:
        None — safe to call against an empty store.
    Postconditions:
        Returns a list of ``StrategyLabRecord`` JSON dumps sorted by
        ``created_at`` (ascending by default, descending when
        ``reverse=True``), delegating to
        ``investment_team.strategy_lab.orchestrator_api._snapshot_prior_records``
        verbatim (façade over ``api.main`` until the helper body moves).
    """
    from investment_team.strategy_lab.orchestrator_api import _snapshot_prior_records

    records = _snapshot_prior_records(reverse=reverse)
    return [r.model_dump(mode="json") for r in records]


# ---------------------------------------------------------------------------
# Composite activities — wrap a whole orchestrator sub-pipeline verbatim
# rather than decomposing it further, reusing existing gate-wiring logic.
# Each constructs its own throwaway ``StrategyLabOrchestrator()`` purely to
# call the existing instance method unmodified.
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_build_short_circuit_record")
def build_short_circuit_record_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run ``StrategyLabOrchestrator._build_short_circuit_record``.

    Preconditions:
        ``params`` carries every keyword ``_build_short_circuit_record``
        accepts (JSON-shaped): ``spec``,
        ``config``, ``code``, ``original_spec``, ``original_code``,
        ``rationale``, ``all_gate_results``, ``refinement_attempts``,
        ``short_circuit_status``, ``short_circuit_reason``,
        ``design_context``, ``phase_back_count``, ``drift_collector``, and
        ``convergence_tracker_state``.
    Postconditions:
        Returns ``{"record": ..., "convergence_tracker_state": ...}``
        (updated by the ``count_asset_class=False`` tracker mutation this
        method performs). Raises ``ApplicationError`` on an unexpected
        exception.
    """
    from investment_team.models import (
        BacktestConfig,
        CodeRevision,
        GateEvent,
        SpecRevision,
        StrategySpec,
    )
    from investment_team.strategy_lab._orchestrator_helpers import (
        _DesignPersistContext,
        _DriftCollector,
    )
    from investment_team.strategy_lab.agents.design_review import SpecCritique
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.strategy_lab.temporal.dto import (
        convergence_tracker_from_wire,
        convergence_tracker_to_wire,
    )

    design_context_data = params.get("design_context") or {}
    design_context = _DesignPersistContext(
        rounds=design_context_data.get("rounds", 0),
        critiques=[
            SpecCritique.model_validate(c) for c in design_context_data.get("critiques", [])
        ],
        stop_reason=design_context_data.get("stop_reason", ""),
        loop_telemetry=design_context_data.get("loop_telemetry", {}),
    )
    drift_data = params.get("drift_collector") or {}
    drift_collector = _DriftCollector(
        spec_history=[SpecRevision(**d) for d in drift_data.get("spec_history", [])],
        code_history=[CodeRevision(**d) for d in drift_data.get("code_history", [])],
        gate_timeline=[GateEvent(**d) for d in drift_data.get("gate_timeline", [])],
    )

    orch = StrategyLabOrchestrator()
    orch.convergence_tracker = convergence_tracker_from_wire(params["convergence_tracker_state"])
    try:
        record = orch._build_short_circuit_record(
            spec=StrategySpec.parse_persisted(params["spec"]),
            config=BacktestConfig(**params["config"]),
            code=params["code"],
            original_spec=StrategySpec.parse_persisted(params["original_spec"]),
            original_code=params["original_code"],
            rationale=params["rationale"],
            all_gate_results=[
                QualityGateResult.model_validate(g) for g in params["all_gate_results"]
            ],
            refinement_attempts=params["refinement_attempts"],
            short_circuit_status=params["short_circuit_status"],
            short_circuit_reason=params["short_circuit_reason"],
            emit=lambda *_a, **_kw: None,
            design_context=design_context,
            phase_back_count=params.get("phase_back_count", 0),
            drift_collector=drift_collector,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "record": record.model_dump(mode="json"),
        "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
    }


_DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S = 20.0
"""How often the background heartbeat beats during a design attempt.

Decoupled from ``emit`` checkpoint cadence on purpose: some single LLM/
sub-calls between two ``emit()`` calls can run long, so relying on ``emit()``
cadence for heartbeat *delivery* risks a missed server-side liveness deadline
(``workflows.py``'s ``_DESIGN_ATTEMPT_HEARTBEAT_TIMEOUT``) triggering a full,
up-to-2-hour attempt retry. A short fixed interval keeps delivery steady
regardless of what the pipeline is doing at any given moment.
"""


def _design_attempt_cancellation_checkpoint(phase: str, data: Dict[str, Any]) -> None:
    """``emit``-shaped checkpoint: raise once cancellation is observed.

    Threaded as the ``emit`` callback into ``_run_design_attempt`` (and
    therefore invoked at every sub-phase step of the design/synthesis/
    refinement/alignment/verification/analysis pipeline), so this is the
    "between steps" cancellation check the owning activity relies on. Does
    not itself call ``activity.heartbeat()`` -- heartbeat *delivery* is owned
    by the background ``BackgroundHeartbeat`` wrapping the whole call (see
    ``_DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S``); this only checks state.

    Preconditions:
        None (safe to call outside a real activity context -- direct-call
        unit tests -- via :func:`shared.temporal.activity_utils.is_cancelled`).
    Postconditions:
        Returns normally when not cancelled. Raises
        :class:`_DesignAttemptCancelled` when ``activity.is_cancelled()`` is
        True.
    """
    if is_cancelled():
        raise _DesignAttemptCancelled()


@activity.defn(name="strategy_lab_run_design_attempt", no_thread_cancel_exception=True)
def run_design_attempt_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one full ``StrategyLabOrchestrator._run_design_attempt`` verbatim.

    This is the single per-attempt boundary the cycle workflow drives. It
    wraps the whole design → synthesis → refinement/alignment →
    verification/analysis → record-assembly sequence
    (``orchestrator._run_design_attempt``) unmodified, inside one activity.
    Running it here — outside the temporalio workflow sandbox — is what makes
    the quality gates, ``datetime.now``/``uuid.uuid4``, and the scattered
    ``os.environ`` reads in those phases legal: none of that is re-expressed as
    sandboxed workflow code. The ``SpecImplementabilityError`` design-re-entry
    control-flow signal is caught at this outermost frame and returned as a
    structured ``{"kind": "reentry", ...}`` outcome rather than crossing the
    activity boundary as an exception; the workflow branches on
    ``outcome["kind"]`` exactly where ``run_cycle`` branches on the ``except``.

    Cooperative cancellation: heartbeats on a background thread
    (``BackgroundHeartbeat``, every ``_DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S``
    seconds) while ``_run_design_attempt`` runs, and checks
    ``activity.is_cancelled()`` at every ``emit`` checkpoint the pipeline
    already threads through each phase
    (:func:`_design_attempt_cancellation_checkpoint`). ``no_thread_cancel_exception=True``
    disables the SDK's own asynchronous thread-injected cancellation so this
    checkpoint is the only cancellation-delivery path -- otherwise the SDK's
    injection can land at an arbitrary point in the call tree (including
    inside a broad ``except Exception`` in the phase pipeline) and race the
    checkpoint. On cancellation this raises ``temporalio.exceptions.
    CancelledError`` instead of returning an outcome dict, so the caller
    workflow sees the activity as cancelled rather than as any of the
    ``kind`` outcomes below.

    Preconditions:
        ``params`` carries the JSON-shaped attempt inputs:
          - ``prior_records``: list of ``StrategyLabRecord`` JSON dumps.
          - ``config``: ``BacktestConfig`` JSON dump.
          - ``signal_brief``: ``SignalIntelligenceBriefV1`` JSON dump or ``None``.
          - ``exclude_asset_classes``: list of str or ``None``.
          - ``directives``: list of convergence-directive strings.
          - ``design_attempt``: int re-entry index (0-based).
          - ``phase_back_count``: int prior phase-backs.
          - ``drift``: ``{"spec_history", "code_history", "gate_timeline"}``
            (each a list of ``SpecRevision``/``CodeRevision``/``GateEvent`` JSON
            dumps) — normally the empty per-attempt child collector, since
            ``_DriftCollector.snapshot()`` hands each attempt a fresh empty one.
          - ``gate_results``: list of ``QualityGateResult`` JSON dumps
            accumulated across prior attempts (extended in place by the run).
          - ``budget_calls``: int LLM calls charged in prior attempts; the
            per-cycle budget is pre-charged to this so its ceiling spans every
            re-entry, exactly as ``run_cycle``'s single ``with use_budget(...)``
            around the whole loop does.
          - ``regime_summary``: ``RegimeSummary`` JSON dump or ``None``.
          - ``convergence_tracker_state``: ``dto.convergence_tracker_to_wire``'s
            output for the batch-level tracker.
          - ``run_id`` (optional): the owning strategy-lab run's id. Absent
            or ``None`` disables design-attempt checkpointing entirely (both
            read and write no-op) -- this is the default for any caller that
            predates ``ADR-012``, including this file's own direct-call unit
            tests.
          - ``generation`` (int, default ``1``): the fencing generation the
            calling workflow incarnation was dispatched with, mirroring
            ``persist_run_state_activity``'s own default-generation
            backward-compat convention.
    Postconditions:
        Returns either
        ``{"kind": "record", "record": <StrategyLabRecord JSON dump>,
        "convergence_tracker_state": ..., "gate_results": [...],
        "budget_calls": int, "drift": {...}}`` on a terminal record, or
        ``{"kind": "reentry", "evidence": str, "last_spec": <StrategySpec JSON
        dump or None>, "last_code": str, "failure_phase": str|None,
        "design_context": {...}|None, "convergence_tracker_state": ...,
        "gate_results": [...], "budget_calls": int, "drift": {...}}`` when
        ``_run_design_attempt`` raised ``SpecImplementabilityError``, or
        ``{"kind": "skipped", "reason": "no_market_data",
        "convergence_tracker_state": ..., "gate_results": [...],
        "budget_calls": int, "drift": {...}}`` when this attempt recorded a
        failed ``"market_data"`` gate (no data available for the asset class —
        ``_fetch_market_data``/``_fetch_market_data_for_synthesis`` degrade to
        this gate rather than raising; a bare ``HTTPException(502)`` is also
        still honored as a secondary/defense-in-depth signal, matching thread
        mode's wave loop). Either case is cycle-terminal — no further
        design-attempt retry. Any other exception (including a non-502
        ``HTTPException``) maps to ``ApplicationError`` via
        :func:`_map_exception_to_application_error`.

        Checkpoint resume (``ADR-012``): when ``run_id`` is present, this
        activity's own Temporal ``workflow_id`` is recoverable
        (:func:`_infer_cycle_scope_from_activity_context`), and a valid
        checkpoint exists for ``(run_id, cycle_scope, design_attempt)``, Phase
        1 (design + review) is skipped and ``drift_collector``/
        ``cumulative_gate_results``/the LLM budget are seeded from the
        checkpoint's boundary-time state instead of from ``params``' -- the
        checkpoint strictly dominates, since it reflects everything ``params``
        carried plus this exact attempt's own Phase-1 work before it crashed.
        Regardless of whether this execution resumed or ran Phase 1 fresh, a
        new checkpoint is (best-effort) written at the design/synthesis
        boundary for a *future* crash to resume from.
    Invariants:
        The returned ``convergence_tracker_state``/``gate_results``/
        ``budget_calls`` reflect exactly this attempt's mutations layered on
        the incoming state; ``drift`` reflects only this attempt's own child
        collector — the workflow owns the parent copy-on-entry/merge across
        attempts.
    """
    from fastapi import HTTPException

    from investment_team.models import (
        BacktestConfig,
        CodeRevision,
        GateEvent,
        SpecRevision,
        StrategyLabRecord,
    )
    from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
    from investment_team.strategy_lab._orchestrator_helpers import _DriftCollector
    from investment_team.strategy_lab.agents._llm_budget import LLMCallBudget, use_budget
    from investment_team.strategy_lab.batch_cache_context import (
        get_or_create_batch_cache,
        use_batch_indicator_cache,
    )
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.indicators.streaming import _batch_cache_flag_enabled
    from investment_team.strategy_lab.market_regime import RegimeSummary
    from investment_team.strategy_lab.orchestrator import (
        StrategyLabOrchestrator,
        _design_max_llm_calls,
    )
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.strategy_lab.temporal.dto import (
        convergence_tracker_from_wire,
        convergence_tracker_to_wire,
    )

    prior_records = [StrategyLabRecord.parse_persisted(r) for r in params["prior_records"]]
    config = BacktestConfig(**params["config"])

    # Batch-scoped indicator cache: when the feature flag is on and the batch
    # workflow supplied a per-batch key, resolve the one shared
    # BatchIndicatorCache for that key in this worker process and bind it around
    # the whole attempt, so every IndicatorRegistry the executor builds during it
    # shares one cache instance. Flag off / no key -> a nullcontext, so nothing
    # is constructed and behavior is unchanged.
    batch_cache_key = params.get("batch_cache_key")
    if _batch_cache_flag_enabled() and batch_cache_key:
        _batch_cache_cm: contextlib.AbstractContextManager = use_batch_indicator_cache(
            get_or_create_batch_cache(batch_cache_key)
        )
    else:
        _batch_cache_cm = contextlib.nullcontext()
    signal_brief = (
        SignalIntelligenceBriefV1(**params["signal_brief"]) if params.get("signal_brief") else None
    )
    regime_summary = (
        RegimeSummary(**params["regime_summary"]) if params.get("regime_summary") else None
    )
    drift_data = params.get("drift") or {}
    drift_collector = _DriftCollector(
        spec_history=[SpecRevision(**d) for d in drift_data.get("spec_history", [])],
        code_history=[CodeRevision(**d) for d in drift_data.get("code_history", [])],
        gate_timeline=[GateEvent(**d) for d in drift_data.get("gate_timeline", [])],
    )
    # ``_run_design_attempt`` appends to this list in place (its
    # ``all_gate_results``); returning the mutated list threads accumulation
    # across re-entries, matching ``run_cycle``'s single ``cumulative_gate_results``.
    cumulative_gate_results = [
        QualityGateResult.model_validate(g) for g in params.get("gate_results", [])
    ]

    orch = StrategyLabOrchestrator()
    orch.convergence_tracker = convergence_tracker_from_wire(params["convergence_tracker_state"])

    # Pre-charge the per-cycle budget to what prior attempts already spent so
    # the ceiling is a true whole-cycle cap, not a per-attempt allowance.
    budget = LLMCallBudget(_design_max_llm_calls())
    budget.calls_made = min(int(params.get("budget_calls") or 0), budget.limit)

    # ── Checkpoint resume (ADR-012) ─────────────────────────────────────
    # Must run BEFORE gate_results_len_before is captured below: a valid
    # checkpoint REPLACES cumulative_gate_results with a list that already
    # includes this attempt's own Phase-1 gate entries, and the post-call
    # market_data-gate scan relies on that snapshot reflecting the state
    # AFTER this replacement to correctly isolate "gates this execution
    # itself added" from "gates the checkpoint already carried."
    run_id = params.get("run_id")
    generation = int(params.get("generation", 1))
    design_attempt_index = params.get("design_attempt", 0)
    cycle_scope = _infer_cycle_scope_from_activity_context()
    checkpoint_enabled = run_id is not None and cycle_scope is not None

    resume_spec = None
    resume_rationale = None
    resume_design_context = None
    if checkpoint_enabled:
        checkpoint = load_design_attempt_checkpoint(run_id, cycle_scope, design_attempt_index)
        if checkpoint is not None:
            # The checkpoint's boundary-time state strictly dominates the
            # params-seeded pre-attempt state: it already reflects everything
            # params carried PLUS Phase 1's own additions before this exact
            # attempt crashed. Replace, never merge/append.
            #
            # load_design_attempt_checkpoint's own fail-open contract only
            # covers the top-level DesignAttemptCheckpoint shape -- nested
            # fields like gate_results are typed loosely (List[Dict[str,
            # Any]]) so a malformed entry (e.g. missing gate_name) survives
            # that validation and only fails here, reconstructing the real
            # QualityGateResult/SpecCritique objects. design_context is the
            # same kind of hole: Dict[str, Any] accepts {}, and
            # _design_context_from_wire returns None without raising, which
            # would otherwise set resume_spec while leaving
            # resume_design_context None -- skipping Phase 1 with a blank
            # context. Without this guard that raises straight out of the
            # activity, and every Temporal retry reloads the same unusable
            # checkpoint and fails identically -- exactly the crash loop the
            # fail-open contract exists to avoid. Treat any reconstruction
            # failure the same as "no checkpoint found": fall through to a
            # normal Phase 1 re-run. Reconstruct into temporaries first and
            # only adopt them together -- never leave
            # drift_collector/cumulative_gate_results/budget in a
            # partially-checkpointed, partially-params-seeded mix if
            # reconstruction fails partway through.
            try:
                checkpoint_drift_collector = _DriftCollector(
                    spec_history=list(checkpoint.spec_history),
                    code_history=list(checkpoint.code_history),
                    gate_timeline=list(checkpoint.gate_timeline),
                )
                checkpoint_gate_results = [
                    QualityGateResult.model_validate(g) for g in checkpoint.gate_results
                ]
                checkpoint_design_context = _design_context_from_wire(checkpoint.design_context)
                if checkpoint_design_context is None:
                    raise ValueError(
                        "checkpoint design_context is empty; treating as invalid for resume"
                    )
            except Exception as exc:  # noqa: BLE001 -- fail open, see comment above
                logger.warning(
                    "design attempt checkpoint for run %s attempt %s failed to "
                    "reconstruct (treating as no checkpoint found): %s",
                    run_id,
                    design_attempt_index,
                    exc,
                    exc_info=True,
                )
            else:
                drift_collector = checkpoint_drift_collector
                cumulative_gate_results = checkpoint_gate_results
                budget.calls_made = min(checkpoint.budget_calls, budget.limit)
                resume_spec = checkpoint.spec
                resume_rationale = checkpoint.rationale
                resume_design_context = checkpoint_design_context

    def _write_checkpoint(_phase: str, data: Dict[str, Any]) -> None:
        """``checkpoint_hook`` passed into ``_run_design_attempt`` (``PhaseCallback`` shape).

        Preconditions:
            ``data`` contains ``"spec"``, ``"rationale"``, and ``"design_context"``
            -- guaranteed by this closure's sole caller, ``_run_design_attempt``'s
            single ``checkpoint_hook(...)`` invocation at the design/synthesis
            boundary (``orchestrator_design.py``), which always passes exactly
            this literal shape. Not re-validated here: this is a private,
            single-call-site closure, not a public boundary.
        Postconditions:
            No-op when ``checkpoint_enabled`` is ``False``. Otherwise persists
            (or best-effort-swallows a retryable write failure for, per the
            ``except`` handling below).
        """
        if not checkpoint_enabled:
            return
        from investment_team.models import DesignAttemptCheckpoint

        checkpoint_to_write = DesignAttemptCheckpoint(
            run_id=run_id,
            cycle_scope=cycle_scope,
            design_attempt=design_attempt_index,
            generation=generation,
            spec=data["spec"],
            rationale=data["rationale"],
            design_context=_design_context_to_wire(data["design_context"]) or {},
            spec_history=list(drift_collector.spec_history),
            code_history=list(drift_collector.code_history),
            gate_timeline=list(drift_collector.gate_timeline),
            gate_results=[g.model_dump(mode="json") for g in cumulative_gate_results],
            budget_calls=budget.calls_made,
        )
        try:
            persist_design_attempt_checkpoint(checkpoint_to_write)
        except ApplicationError as exc:
            if exc.non_retryable:
                # A stale-fencing (or genuine caller-precondition-violation)
                # failure means this whole activity execution belongs to a
                # superseded run incarnation -- let it kill the activity,
                # same as persist_run_state_activity's own stale-write
                # contract.
                raise
            # A transient job-service lookup blip on the WRITE side. By this
            # point Phase 1's real LLM calls already happened -- failing the
            # whole activity here would force a full Temporal-level retry
            # (burning one of only 2 total attempts) purely to recover a
            # checkpoint write, when losing this write only costs a future
            # crash the ability to skip Phase 1 on resume, not correctness.
            # Best-effort: log and continue this attempt.
            logger.warning(
                "design attempt checkpoint write failed for run %s attempt %s "
                "(retryable lookup failure): %s",
                run_id,
                design_attempt_index,
                exc,
                exc_info=True,
            )
        except Exception as exc:  # noqa: BLE001
            # A raw (non-ApplicationError) failure from the WRITE itself --
            # e.g. a job-service connection/HTTP error surfacing after
            # _persist_run_state's own retries are exhausted. Only the
            # fencing check ahead of the write raises ApplicationError; the
            # write call can still fail with an ordinary exception. Same
            # best-effort rationale as the retryable-fencing branch above:
            # checkpoint persistence is optional, so this must never
            # propagate up and discard the whole (already-completed) design
            # attempt over a transport blip.
            logger.warning(
                "design attempt checkpoint write failed for run %s attempt %s "
                "(non-fencing write failure): %s",
                run_id,
                design_attempt_index,
                exc,
                exc_info=True,
            )

    def _drift_to_wire(collector: _DriftCollector) -> Dict[str, Any]:
        return {
            "spec_history": [r.model_dump(mode="json") for r in collector.spec_history],
            "code_history": [r.model_dump(mode="json") for r in collector.code_history],
            "gate_timeline": [r.model_dump(mode="json") for r in collector.gate_timeline],
        }

    def _skipped_outcome() -> Dict[str, Any]:
        return {
            "kind": "skipped",
            "reason": "no_market_data",
            "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
            "gate_results": [g.model_dump(mode="json") for g in cumulative_gate_results],
            "budget_calls": budget.calls_made,
            "drift": _drift_to_wire(drift_collector),
        }

    # ``cumulative_gate_results`` is ``all_gate_results`` in
    # ``_run_design_attempt``'s frame — mutated in place, so a "market_data"
    # gate this attempt records lands here too. Snapshot the length first so
    # the post-call scan only sees THIS attempt's own additions, never a
    # market_data failure recorded by an earlier re-entered attempt (whose
    # own outcome already returned separately as "reentry").
    gate_results_len_before = len(cumulative_gate_results)

    def _beat() -> None:
        # Best-effort: outside a real activity context (e.g. a direct-call
        # unit test) this raises and BackgroundHeartbeat swallows it.
        activity.heartbeat()

    try:
        with (
            BackgroundHeartbeat(
                _beat,
                _DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S,
                copy_context=True,
                name="strategy-lab-design-attempt-hb",
            ),
            use_budget(budget),
            _batch_cache_cm,
        ):
            record = orch._run_design_attempt(
                prior_records=prior_records,
                config=config,
                signal_brief=signal_brief,
                emit=_design_attempt_cancellation_checkpoint,
                exclude_asset_classes=params.get("exclude_asset_classes"),
                directives=list(params.get("directives") or []),
                design_attempt=design_attempt_index,
                phase_back_count=params.get("phase_back_count", 0),
                drift_collector=drift_collector,
                cumulative_gate_results=cumulative_gate_results,
                regime_summary=regime_summary,
                resume_spec=resume_spec,
                resume_rationale=resume_rationale,
                resume_design_context=resume_design_context,
                checkpoint_hook=_write_checkpoint,
            )
    except _DesignAttemptCancelled:
        # BaseException, so it already bypassed every ``except Exception``
        # (and narrower) handler in the phase pipeline untouched -- convert
        # it to the real Temporal signal only here, at the activity boundary.
        raise CancelledError("Strategy Lab design attempt cancelled before completion") from None
    except SpecImplementabilityError as exc:
        design_context_wire = _design_context_to_wire(exc.design_context)
        return {
            "kind": "reentry",
            "evidence": exc.evidence,
            "last_spec": (
                exc.last_spec.model_dump(mode="json") if exc.last_spec is not None else None
            ),
            "last_code": exc.last_code or "",
            "failure_phase": exc.failure_phase,
            "design_context": design_context_wire,
            "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
            "gate_results": [g.model_dump(mode="json") for g in cumulative_gate_results],
            "budget_calls": budget.calls_made,
            "drift": _drift_to_wire(drift_collector),
        }
    except HTTPException as exc:
        # Defense-in-depth: no current code path in the design-attempt
        # pipeline raises this (market-data failures degrade to the
        # "market_data" gate checked below instead), but thread mode's wave
        # loop still honors a bare 502 the same way, so this stays a live
        # secondary signal rather than being removed. Any other
        # HTTPException status is still a deep failure.
        if exc.status_code == 502:
            return _skipped_outcome()
        raise _map_exception_to_application_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc

    # Primary "no market data" signal: ``_fetch_market_data``/
    # ``_fetch_market_data_for_synthesis`` never raise on a failed/empty
    # fetch — they record a critical "market_data" gate and the synthesis
    # loop breaks immediately, and ``_run_design_attempt`` still returns a
    # normal (failing) record rather than short-circuiting. Detect that gate
    # among this attempt's own additions and report it as a skip instead of
    # a real record, matching what thread mode's now-dead HTTPException(502)
    # branch was actually meant to catch.
    if any(
        g.gate_name == "market_data" and not g.passed
        for g in cumulative_gate_results[gate_results_len_before:]
    ):
        return _skipped_outcome()

    return {
        "kind": "record",
        "record": record.model_dump(mode="json"),
        "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
        "gate_results": [g.model_dump(mode="json") for g in cumulative_gate_results],
        "budget_calls": budget.calls_made,
        "drift": _drift_to_wire(drift_collector),
    }


# ---------------------------------------------------------------------------
# Batch-level activities — used by StrategyLabBatchWorkflow (the per-batch /
# per-wave orchestration around the per-cycle child workflows). Each wraps an
# existing thread-mode helper verbatim so the two paths share one implementation.
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_compute_signal_brief")
def compute_signal_brief_activity(benchmark_symbol: str) -> Dict[str, Any]:
    """Build the per-batch signal brief over all currently-persisted prior records.

    Preconditions:
        ``benchmark_symbol`` is the run's benchmark ticker.
    Postconditions:
        Returns ``{"signal_brief": <SignalIntelligenceBriefV1 JSON dump or None>,
        "signal_brief_storage": <dict or None>}`` — the JSON-shaped pair the batch
        workflow threads into each cycle's ``signal_brief`` input and into
        ``finalize_cycle_record_activity``. Delegates to
        ``investment_team.strategy_lab.orchestrator_api._compute_signal_brief_snapshot``
        (façade over ``api.main``; fail-open — never raises; returns a
        skipped/degraded marker instead), so this activity likewise only raises
        ``ApplicationError`` on a genuinely unexpected exception.
    """
    from investment_team.strategy_lab.orchestrator_api import _compute_signal_brief_snapshot

    try:
        brief, storage = _compute_signal_brief_snapshot(benchmark_symbol)
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "signal_brief": brief.model_dump(mode="json") if brief is not None else None,
        "signal_brief_storage": storage,
    }


@activity.defn(name="strategy_lab_is_run_cancelled")
def is_run_cancelled_activity(run_id: str) -> bool:
    """Return True if the run has stopped for any external reason (terminal job status).

    Despite the activity name (kept for wire-protocol/replay compatibility),
    this is a general "should the workflow stop" check, not a cancellation-
    specific one -- it also fires for an externally-recorded failure or
    interruption, not just a genuine user cancellation.

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns ``orchestrator_api._is_strategy_lab_run_externally_stopped``'s
        result verbatim — True for a ``cancelled``/``failed``/``interrupted``
        job status, False otherwise. That helper never raises, so this activity
        never raises either.
    """
    from investment_team.strategy_lab.orchestrator_api import (
        _is_strategy_lab_run_externally_stopped,
    )

    return _is_strategy_lab_run_externally_stopped(run_id)


@activity.defn(name="strategy_lab_external_terminal_status")
def external_terminal_status_activity(run_id: str) -> Optional[str]:
    """Return the run's persisted external stop status, or None if not stopped.

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns ``orchestrator_api._strategy_lab_external_terminal_status``'s
        result verbatim — the exact persisted status string (``cancelled``,
        ``failed``, or ``interrupted``) when the job was externally marked
        terminal, else ``None``. That helper never raises, so this activity never
        raises either. Callers persist the returned status directly so an
        external interrupt/failure is not mislabeled a user cancellation (thread
        mode's ``_strategy_lab_worker`` makes the same distinction).
    """
    from investment_team.strategy_lab.orchestrator_api import (
        _strategy_lab_external_terminal_status,
    )

    return _strategy_lab_external_terminal_status(run_id)


@activity.defn(name="strategy_lab_finalize_cycle_record")
def finalize_cycle_record_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run the post-``run_cycle`` finalization (signal brief + paper-trade + persist).

    The per-cycle child workflow (``StrategyLabCycleWorkflow``) returns only the
    raw ``run_cycle`` record; this activity reproduces the tail that thread-mode's
    ``_run_one_strategy_lab_cycle`` runs after it, by delegating to the shared
    ``orchestrator_api._finalize_strategy_lab_cycle_record`` helper (façade over
    ``api.main`` until the helper body moves).

    Preconditions:
        ``params`` carries ``record`` (a ``StrategyLabRecord`` JSON dump from the
        cycle workflow), and optionally ``run_id`` (the owning run),
        ``generation`` (the fencing generation the calling workflow incarnation
        was dispatched with, default ``1`` — the legacy generation, since that's
        also the generation a pre-upgrade caller that never set this field is
        treated as presenting), ``signal_brief_storage`` (dict or None),
        ``paper_trading_enabled`` (bool, default True), and
        ``paper_trading_lookback_days`` (int, default 365). When ``params``
        lacks ``run_id`` — a ``strategy_lab_finalize_cycle_record`` task
        Temporal already scheduled from a pre-upgrade workflow history, whose
        recorded input predates this key entirely, retried after a rolling
        deploy/worker restart — it is recovered from the current activity's
        Temporal ``workflow_id`` (``"strategy-lab-{run_id}"``, always available
        from the execution context regardless of what the scheduled payload
        contains), so such a payload is still fenced rather than silently
        skipped. Only when even that recovery fails (no activity context at
        all, e.g. a direct non-Temporal call) do both fencing checks below
        no-op.
    Postconditions:
        Checks the fencing token BOTH before and after
        ``_finalize_strategy_lab_cycle_record`` (market-data fetch +
        paper-trading execution + the durable record write — not a fast,
        adjacent operation like ``persist_run_state_activity``'s): raises a
        non-retryable ``ApplicationError`` when ``generation`` is stale at
        either check. The pre-check is a cheap early exit for an
        already-known-stale call; the post-check catches a restart that
        mints a newer generation WHILE this activity was running. A durable
        lookup failure (as opposed to an actual stale token) at the
        post-check is retried locally, bounded, before giving up — see
        ``_POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS`` — since
        ``_finalize_strategy_lab_cycle_record`` already durably committed by
        that point and retrying the WHOLE ACTIVITY (as Temporal would on a
        non-retryable-free failure) would re-execute its non-idempotent side
        effects (a fresh paper-trading session, orphaning the first); a
        persisting lookup failure after those local retries are exhausted is
        still raised non-retryable for that reason. Neither check is a true
        atomic conditional write against the record store — see the module
        docstring's honest accounting of what's actually closed here.
        Otherwise returns ``{"record": <finalized StrategyLabRecord JSON
        dump>}`` — the same record with ``paper_trading_*`` resolved and
        durably persisted. Raises ``ApplicationError`` on any other unexpected
        exception from ``_finalize_strategy_lab_cycle_record`` itself
        (paper-trading failures are already non-fatal inside the helper).
    """
    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.orchestrator_api import (
        _finalize_strategy_lab_cycle_record,
    )
    from investment_team.strategy_lab.run_state import DEFAULT_FENCING_GENERATION

    def _check_generation(
        run_id: str, *, retry_on_lookup_failure: bool, lookup_retry_delays: Tuple[float, ...] = ()
    ) -> None:
        _check_generation_fencing(
            run_id,
            params.get("generation", DEFAULT_FENCING_GENERATION),
            retry_on_lookup_failure=retry_on_lookup_failure,
            lookup_retry_delays=lookup_retry_delays,
        )

    run_id = params.get("run_id") or _infer_run_id_from_activity_context()
    if run_id is not None:
        # Cheap early exit for an already-known-stale call -- checked before
        # parsing so a stale call doesn't pay for Pydantic reconstruction of
        # a potentially large record. Safe to retry the whole activity if
        # THIS lookup itself transiently fails: nothing has been written yet.
        _check_generation(run_id, retry_on_lookup_failure=True)
    try:
        record = StrategyLabRecord.parse_persisted(params["record"])
        finalized = _finalize_strategy_lab_cycle_record(
            record,
            signal_brief_storage=params.get("signal_brief_storage"),
            paper_trading_enabled=params.get("paper_trading_enabled", True),
            paper_trading_lookback_days=params.get("paper_trading_lookback_days", 365),
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    if run_id is not None:
        # A restart may have minted a newer generation WHILE the finalize call
        # above was running (market data + paper trading can take a while) --
        # this can no longer prevent the write that already happened, but it
        # does stop the workflow from treating this cycle's result as trusted.
        # The durable-read lookup itself gets a few bounded local retries
        # (_POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS) so a momentary job-service
        # blip doesn't permanently fail an otherwise-successful run: retrying
        # the WHOLE ACTIVITY (as a non-retryable=False failure would let
        # Temporal do) would re-run _finalize_strategy_lab_cycle_record's
        # non-idempotent side effects a second time, but retrying just this
        # cheap read locally does not. Only a failure that persists through
        # those local retries -- or an actual stale token -- is non-retryable.
        _check_generation(
            run_id,
            retry_on_lookup_failure=False,
            lookup_retry_delays=_POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS,
        )
    return {"record": finalized.model_dump(mode="json")}


@activity.defn(name="strategy_lab_merge_wave_results")
def merge_wave_results_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fold a completed wave's cycle results into the batch-level convergence tracker.

    Reproduces the deterministic merge thread-mode does after each wave
    (``api/main.py``: sort settled cycles by cycle index, then per record
    ``primary_tracker.record(strategy, gate_results)`` for diversity/failure-mode
    state and ``primary_tracker.merge_from(cycle_tracker)`` for the trial-count
    delta). Runs in an activity — not workflow code — so the Pydantic
    reconstruction of ``StrategyLabRecord``/``QualityGateResult`` and the tracker
    math stay outside the temporalio sandbox.

    Mirrors thread mode's per-cycle isolation of the merge step (``api/main.py``):
    a single record's ``merge_from`` failure is captured, not fatal — the wave's
    other records still merge and the activity still succeeds.

    Preconditions:
        ``params`` = ``{"primary_tracker_state": <dto wire dict>, "wave_results":
        [{"cycle_index": int, "record": <StrategyLabRecord JSON dump>,
        "cycle_tracker_state": <dto wire dict>}, ...]}``.
    Postconditions:
        Returns ``{"primary_tracker_state": <updated dto wire dict>, "merge_errors":
        [{"cycle_index": int, "error": str, "exception_type": str, "reason":
        "tracker_merge_failed"}, ...]}`` — the primary tracker with every settled
        cycle in the wave recorded/merged in cycle-index order (reproducible
        across runs), and one ``merge_errors`` entry per record whose
        ``merge_from`` call raised (that record's ``.record(...)`` call still
        ran). A ``merge_errors`` entry's ``cycle_index`` is ``wr["cycle_index"] +
        1`` — a 1-based, human-friendly cycle number for error reporting, distinct
        from the 0-based ``cycle_index`` used in ``wave_results`` above. Raises
        ``ApplicationError`` on an exception outside the isolated ``merge_from``
        step (e.g. malformed input).
    """
    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult

    try:
        primary = ConvergenceTracker.from_wire_dict(params["primary_tracker_state"])
        merge_errors: List[Dict[str, Any]] = []
        for wr in sorted(params["wave_results"], key=lambda w: w["cycle_index"]):
            record = StrategyLabRecord.parse_persisted(wr["record"])
            gate_results = [
                QualityGateResult(**g) if isinstance(g, dict) else g
                for g in record.quality_gate_results
            ]
            primary.record(record.strategy, gate_results)
            try:
                primary.merge_from(ConvergenceTracker.from_wire_dict(wr["cycle_tracker_state"]))
            except Exception as exc:  # noqa: BLE001
                merge_errors.append(
                    {
                        "cycle_index": wr["cycle_index"] + 1,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                        "reason": "tracker_merge_failed",
                    }
                )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {"primary_tracker_state": primary.to_wire_dict(), "merge_errors": merge_errors}


ACTIVITIES = [
    compute_regime_summary_activity,
    persist_run_state_activity,
    snapshot_prior_records_activity,
    build_short_circuit_record_activity,
    run_design_attempt_activity,
    resolve_workflow_config_activity,
    compute_signal_brief_activity,
    is_run_cancelled_activity,
    external_terminal_status_activity,
    finalize_cycle_record_activity,
    merge_wave_results_activity,
]

__all__ = [
    "ACTIVITIES",
    "compute_signal_brief_activity",
    "finalize_cycle_record_activity",
    "is_run_cancelled_activity",
    "external_terminal_status_activity",
    "merge_wave_results_activity",
    "build_short_circuit_record_activity",
    "compute_regime_summary_activity",
    "persist_run_state_activity",
    "resolve_workflow_config_activity",
    "run_design_attempt_activity",
    "snapshot_prior_records_activity",
]
