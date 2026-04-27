"""ProductOwnerAgent — ranks a product's backlog.

Flow:

1. Pull every story under ``product_id`` from the store.
2. Ask the LLM to produce *scoring inputs* (never the score itself; see
   :mod:`product_delivery.product_owner_agent.prompts`).
3. Compute WSJF or RICE scores deterministically via
   :mod:`product_delivery.scoring`.
4. Optionally persist the scores back onto each story row.
5. Return a ranked :class:`GroomResult`.

Tests inject a stub ``llm_client`` (any object with a ``complete_json``
method). In production we use ``llm_service.get_client("product_owner")``.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Callable, Protocol

from product_delivery.models import (
    GroomMethod,
    GroomResult,
    RankedBacklogItem,
    Story,
)
from product_delivery.product_owner_agent.prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
)
from product_delivery.scoring import RICEInputs, WSJFInputs, rice_score, wsjf_score
from product_delivery.store import ProductDeliveryStore

logger = logging.getLogger(__name__)

_MISSING_RATIONALE = "LLM did not score this story; needs manual review."
_MALFORMED_RATIONALE = "LLM emitted malformed scoring inputs; needs manual review."
# `cause` is filled in per-deferral so operators can see which cap fired
# (story count vs. prompt-byte budget) and tune the right env var. Codex
# flagged that conflating the two pointed at the wrong control.
_BACKLOG_TOO_LARGE_RATIONALE = "Story deferred to a later run: {cause}."


def _max_stories_per_groom() -> int:
    """Per-call cap on stories serialised into the grooming prompt.

    A single LLM call has a hard context-window ceiling; serialising
    every story in a multi-thousand-item production backlog would
    either be silently truncated by the provider or fail the call
    outright (which the route maps to 503, blocking *all* prioritisation
    until the backlog shrinks). We cap at ``PRODUCT_DELIVERY_GROOM_MAX_STORIES``
    (default 200) and surface the deferred stories in the result with
    ``score=0`` + a "deferred" rationale so they don't disappear from
    the planning view.
    """
    return _env_int("PRODUCT_DELIVERY_GROOM_MAX_STORIES", default=200, minimum=1)


def _max_prompt_bytes_per_groom() -> int:
    """Per-call byte budget for the grooming prompt's stories payload.

    Story-count alone isn't sufficient: a small backlog with very long
    ``user_story`` fields can still overrun the model context (Codex
    flagged this in PR #369). We trim from the *tail* of the candidate
    slice once the JSON-serialized payload exceeds this budget; the
    trimmed stories surface with the same "deferred" rationale as the
    story-count cap, so deferred work stays visible to the planner.

    Default 65_536 bytes (~16k tokens worst case) — well under the
    smallest model context budgets we use. Override with
    ``PRODUCT_DELIVERY_GROOM_MAX_PROMPT_BYTES``.
    """
    return _env_int("PRODUCT_DELIVERY_GROOM_MAX_PROMPT_BYTES", default=65_536, minimum=1_024)


def _env_int(name: str, *, default: int, minimum: int) -> int:
    """Parse an int env var with a default + lower bound.

    Centralises the "missing → default; non-int → warn + default;
    below minimum → clamp" pattern shared by the two grooming cap
    knobs above. Returns >= ``minimum`` always.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int; using default %d",
            name,
            raw,
            default,
        )
        return default
    return max(minimum, value)


class LLMScoringUnavailable(RuntimeError):
    """The LLM call inside grooming failed end-to-end (transport, parse,
    or model error). The route maps this to 503 so callers can retry —
    a 200 with all-fallback zero-scores would silently de-prioritise
    the entire backlog during a transient outage.
    """


def _to_float(value: Any, fallback: float) -> float:
    """Coerce an LLM-supplied value to a finite ``float`` with a typed fallback.

    Handles four lossy LLM behaviours:

    * explicit ``null`` → returns the fallback (``float(None)`` would
      otherwise raise ``TypeError`` and skip the whole story);
    * JSON booleans (``true``/``false``) → propagates ``TypeError``
      so the caller surfaces the story as malformed;
      ``float(True)`` would otherwise silently produce ``1.0`` and
      let a confused LLM rank everything identically;
    * non-finite ``"NaN"`` / ``"Infinity"`` → returns the fallback
      (those would later break Starlette's JSON encoder if persisted);
    * malformed strings → propagates ``ValueError``/``TypeError`` for
      the caller's exception handler to log + skip.
    """
    if value is None:
        return float(fallback)
    if isinstance(value, bool):
        # `isinstance(True, int)` is True, so `float(True)` returns 1.0
        # without raising. Reject explicitly so the malformed fallback
        # in `_compute_ranked` fires instead of silently scoring.
        raise TypeError("LLM scoring input must be a number, not a boolean")
    coerced = float(value)
    if not math.isfinite(coerced):
        return float(fallback)
    return coerced


def _score_for(
    method: GroomMethod, inputs: dict[str, Any], story: Story
) -> tuple[float, float | None, float | None]:
    """Compute (score, wsjf_value, rice_value) for one story.

    Centralises the WSJF/RICE branching so the loop in ``_compute_ranked``
    doesn't have to. The two unused per-method values stay ``None`` so
    ``RankedBacklogItem`` and ``persist_rows`` keep their typed shape.
    """
    if method == "wsjf":
        wsjf = wsjf_score(
            WSJFInputs(
                user_business_value=_to_float(inputs.get("user_business_value"), 0.0),
                time_criticality=_to_float(inputs.get("time_criticality"), 0.0),
                risk_reduction_or_opportunity_enablement=_to_float(
                    inputs.get("risk_reduction_or_opportunity_enablement"), 0.0
                ),
                job_size=_to_float(inputs.get("job_size"), story.estimate_points or 1.0),
            )
        )
        return wsjf, wsjf, None
    rice = rice_score(
        RICEInputs(
            reach=_to_float(inputs.get("reach"), 0.0),
            impact=_to_float(inputs.get("impact"), 0.0),
            confidence=_to_float(inputs.get("confidence"), 0.0),
            effort=_to_float(inputs.get("effort"), (story.estimate_points or 4.0) / 4.0),
        )
    )
    return rice, None, rice


def _story_payload(story: Story) -> dict[str, Any]:
    """The serialised shape sent to the LLM for one story.

    Centralised so ``_call_llm`` and ``_full_prompt_size`` agree on
    what's in the prompt — otherwise the byte budget would underestimate.
    """
    return {
        "id": story.id,
        "title": story.title,
        "user_story": story.user_story,
        "estimate_points": story.estimate_points,
        "status": story.status,
    }


def _full_prompt_size(method: GroomMethod, stories: list[Story]) -> int:
    """Byte size of the *full* prompt `_call_llm` will actually send.

    Codex flagged that the previous size estimate measured only the
    JSON list of stories, ignoring ``SYSTEM_PROMPT`` and the
    instruction text in ``build_user_prompt``. Near the byte budget
    that meant a payload could pass the trimmer and still overrun
    model context inside ``_call_llm``, turning what should be a
    deferred-story result into a 503 grooming failure.

    This now sums:
      * the system prompt bytes (``SYSTEM_PROMPT``),
      * the full user prompt as ``build_user_prompt(method, …)``
        renders it — including the instruction wrapper and the
        ``json.dumps(..., indent=2)`` payload — so the budget reflects
        what actually crosses the wire to the model.
    """
    stories_payload = json.dumps([_story_payload(s) for s in stories], indent=2)
    user_prompt = build_user_prompt(method, stories_payload)
    return len(SYSTEM_PROMPT.encode("utf-8")) + len(user_prompt.encode("utf-8"))


def _select_grooming_candidates(
    method: GroomMethod, stories: list[Story], *, cap: int, byte_budget: int
) -> tuple[list[Story], list[Story], list[Story]]:
    """Pick up to ``cap`` stories that fit in ``byte_budget``.

    Returns ``(scored, count_deferred, byte_deferred)``. Codex flagged
    a starvation case in the previous two-stage selection: when
    ``cap=1`` and the oldest candidate is always over the byte budget,
    the trim returned ``scored=[]`` every run, the oldest story's
    ``updated_at`` never advanced, and newer stories that would fit
    were permanently deferred for count-cap reasons even though there
    was prompt room for them. The fix folds the two stages into one
    pass so byte-deferred stories don't consume cap slots:

      * sort by ``updated_at`` ascending (oldest-touched first);
      * for each story, if it'd push the prompt past ``byte_budget``,
        defer it as **byte_deferred** — it does *not* count against
        ``cap``;
      * else, if we already have ``cap`` kept, defer the rest as
        **count_deferred** — still surfaced with the count-cap
        rationale so an operator knows which control to tune;
      * else, keep it.

    Stable sort within ``updated_at`` ties keeps selection
    deterministic across runs.
    """
    if not stories:
        return [], [], []
    by_freshness = sorted(stories, key=lambda s: s.updated_at)
    scored: list[Story] = []
    count_deferred: list[Story] = []
    byte_deferred: list[Story] = []
    for story in by_freshness:
        if len(scored) >= cap:
            count_deferred.append(story)
            continue
        candidate = scored + [story]
        if _full_prompt_size(method, candidate) > byte_budget:
            byte_deferred.append(story)
            continue
        scored.append(story)
    return scored, count_deferred, byte_deferred


def _fallback_item(story: Story, method: GroomMethod, rationale: str) -> RankedBacklogItem:
    """Synthetic ``score=0`` row for a story we couldn't score this run.

    Surfaced in the ranked list so downstream planning sees the gap;
    *not* persisted so existing scores on the row stay intact. Used for
    three branches: the LLM omitted the story, the LLM emitted
    malformed inputs for it, and the backlog was too large to fit in
    one prompt so the story was deferred.
    """
    return RankedBacklogItem(
        kind="story",
        id=story.id,
        title=story.title,
        score=0.0,
        wsjf_score=0.0 if method == "wsjf" else None,
        rice_score=0.0 if method == "rice" else None,
        rationale=rationale,
    )


def _index_payload(payload: Any) -> dict[str, dict[str, Any]]:
    """Build ``{story_id: raw_item}`` from the LLM response.

    Raises :class:`LLMScoringUnavailable` (→ 503 at the route) when the
    envelope itself is malformed — a non-dict response or a response
    without a list-typed ``items`` key indicates a provider/schema
    regression and is *not* equivalent to "the LLM scored zero
    stories". Returning a 200 with every story zero-scored would mask
    that regression as normal output and silently de-prioritise the
    whole backlog, so we surface it as a retryable failure instead.

    Per-item issues (non-dict entries, missing/non-string ids) are
    still tolerated: those are filtered out and the rest of the items
    proceed through the missing-fallback path. Duplicate ids keep the
    first occurrence so persisted scores stay deterministic instead
    of depending on which copy lands last.
    """
    if not isinstance(payload, dict):
        raise LLMScoringUnavailable(
            f"LLM returned a non-object envelope ({type(payload).__name__}); "
            "expected an object with an 'items' list."
        )
    items = payload.get("items")
    if not isinstance(items, list):
        raise LLMScoringUnavailable(
            "LLM envelope missing or non-list 'items' field; "
            "treating as a transient provider/schema failure."
        )
    indexed: dict[str, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        sid = raw.get("id")
        if not isinstance(sid, str):
            continue
        if sid in indexed:
            logger.warning(
                "ProductOwnerAgent: duplicate story id %s in LLM payload; ignoring repeat",
                sid,
            )
            continue
        indexed[sid] = raw
    return indexed


class _LLMLike(Protocol):
    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = ...,
        system_prompt: str | None = ...,
        **kwargs: Any,
    ) -> dict[str, Any]: ...


class ProductOwnerAgent:
    """Stateless: depends on a store + an LLM client (or factory).

    Accepting a *factory* (not a pre-built client) lets the agent
    short-circuit on an empty backlog without ever touching the LLM
    stack — important because the route can't safely "pre-check"
    emptiness without racing concurrent writes. We bootstrap the
    client lazily on the first call to ``_call_llm``.

    For ergonomic tests, callers may pass a pre-built ``llm_client``
    instead; it gets wrapped in a constant factory so the lazy path
    behaves the same way. Exactly one of the two arguments must be set.
    """

    def __init__(
        self,
        store: ProductDeliveryStore,
        llm_client: _LLMLike | None = None,
        *,
        llm_factory: Callable[[], _LLMLike] | None = None,
    ) -> None:
        if (llm_client is None) == (llm_factory is None):
            raise TypeError("ProductOwnerAgent: pass exactly one of llm_client or llm_factory")
        self._store = store
        if llm_factory is not None:
            self._llm_factory = llm_factory
            self._llm: _LLMLike | None = None
        else:
            # Pre-built client — store it directly so `_call_llm` skips
            # the bootstrap branch.
            self._llm_factory = lambda: llm_client  # type: ignore[return-value,assignment]
            self._llm = llm_client

    def groom(
        self,
        *,
        product_id: str,
        method: GroomMethod = "wsjf",
        persist: bool = True,
    ) -> GroomResult:
        # Single read of the backlog — no second `list_stories_for_product`
        # call elsewhere. The route doesn't pre-check emptiness either,
        # so there's no race window between an empty-check and the actual
        # iteration.
        stories = self._store.list_stories_for_product(product_id)
        if not stories:
            return GroomResult(
                product_id=product_id,
                method=method,
                ranked=[],
                rationale="No stories under this product yet.",
            )

        # Cap stories sent to the LLM to bound prompt size — production
        # backlogs can exceed the model context window. The deferred
        # tail is still surfaced in the result (score=0 + deferred
        # rationale) so callers see them; they just aren't scored this
        # run.
        #
        # Combined cap + byte-budget selection:
        # Codex flagged a starvation case where the two-stage version
        # (count cap first, then byte trim) could leave `scored_stories`
        # empty forever under cap=1 + an oldest oversize story —
        # `updated_at` never advanced, the oversize story stayed at the
        # front of the freshness queue, and newer stories that would
        # actually fit were permanently deferred. `_select_grooming_candidates`
        # iterates the freshness-sorted list once and only consumes a
        # cap slot when a story is *actually kept*, so a byte-deferral
        # rolls forward to the next candidate instead of blocking the run.
        cap = _max_stories_per_groom()
        prompt_bytes_cap = _max_prompt_bytes_per_groom()
        scored_stories, count_deferred, byte_deferred = _select_grooming_candidates(
            method, stories, cap=cap, byte_budget=prompt_bytes_cap
        )
        # Track the deferral cause separately so the rationale points
        # operators at the right control to tune (story-count cap vs.
        # prompt-byte budget) — Codex flagged that conflating the two
        # mis-diagnoses the failure in production.
        deferred_total = len(count_deferred) + len(byte_deferred)
        if deferred_total:
            logger.warning(
                "ProductOwnerAgent: backlog has %d stories; grooming %d, deferring "
                "%d (%d count-cap, %d byte-budget)",
                len(stories),
                len(scored_stories),
                deferred_total,
                len(count_deferred),
                len(byte_deferred),
            )

        if scored_stories:
            scoring_payload = self._call_llm(method, scored_stories)
            ranked, persist_rows = self._compute_ranked(method, scored_stories, scoring_payload)
        else:
            # Every candidate story was deferred (e.g. the only story
            # in the backlog has a `user_story` field longer than the
            # byte budget). Skip the LLM call entirely — sending an
            # empty list would either produce useless 0-score output
            # or 503 the call. Surface every story as deferred so the
            # planner sees the gap.
            ranked = []
            persist_rows = []
        # Distinct rationale per cause so operators can tell at a
        # glance which cap fired — the message reads cleanly when
        # both fire (rare) and stays accurate when only one does.
        for deferred in count_deferred:
            ranked.append(
                _fallback_item(
                    deferred,
                    method,
                    _BACKLOG_TOO_LARGE_RATIONALE.format(cause=f"story-count cap of {cap} exceeded"),
                )
            )
        for deferred in byte_deferred:
            ranked.append(
                _fallback_item(
                    deferred,
                    method,
                    _BACKLOG_TOO_LARGE_RATIONALE.format(
                        cause=f"prompt-byte budget of {prompt_bytes_cap} exceeded"
                    ),
                )
            )

        # Report the actual *persisted* count (not just `len(persist_rows)`):
        # if rows were deleted between ranking and persistence, fewer
        # scores landed than we attempted, and downstream automation
        # parsing the rationale should see that.
        persisted = 0
        if persist and persist_rows:
            persisted = self._store.bulk_update_story_scores(persist_rows)
        elif not persist:
            # Caller explicitly opted out — report attempted scoring.
            persisted = len(persist_rows)

        ranked.sort(key=lambda r: r.score, reverse=True)
        total = len(stories)
        if persisted == total:
            rationale = f"Scored {persisted} stories using {method.upper()}."
        elif deferred_total:
            # Both causes get name-checked in the summary so an
            # operator reading the result knows which control to tune.
            rationale = (
                f"Scored {persisted}/{total} stories using {method.upper()}; "
                f"{deferred_total} deferred ({len(count_deferred)} story-count "
                f"cap of {cap}, {len(byte_deferred)} prompt-byte budget "
                f"of {prompt_bytes_cap}); "
                f"{total - persisted - deferred_total} surfaced with "
                f"score=0 for manual review."
            )
        else:
            rationale = (
                f"Scored {persisted}/{total} stories using {method.upper()}; "
                f"{total - persisted} surfaced with score=0 for manual review."
            )
        return GroomResult(
            product_id=product_id,
            method=method,
            ranked=ranked,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_llm(self, method: GroomMethod, stories: list[Story]) -> dict[str, Any]:
        # Use the shared `_story_payload` helper so the byte-budget
        # estimate in `_full_prompt_size` matches what's actually
        # serialised here. Otherwise an indent-2 difference would
        # silently let the prompt overrun the budget.
        stories_payload = json.dumps(
            [_story_payload(s) for s in stories],
            indent=2,
        )
        prompt = build_user_prompt(method, stories_payload)
        try:
            if self._llm is None:
                # Lazy bootstrap: factory failures (missing credentials,
                # broken provider plugin, etc.) get the same 503 mapping
                # as LLM-call failures. Empty-backlog grooming never
                # reaches here, so it's safe to defer.
                self._llm = self._llm_factory()
            return self._llm.complete_json(
                prompt,
                temperature=0.0,
                system_prompt=SYSTEM_PROMPT,
            )
        except Exception as exc:
            # End-to-end LLM failure (network, model, JSON parse). Convert
            # to a typed domain error so the route can return 503 and
            # callers retry, instead of returning 200 with every story
            # zero-scored. Per-story missing/malformed payloads are still
            # handled gracefully downstream in `_compute_ranked`.
            logger.exception("ProductOwnerAgent: LLM call failed; surfacing as 503")
            raise LLMScoringUnavailable(f"LLM scoring call failed: {exc}") from exc

    def _compute_ranked(
        self,
        method: GroomMethod,
        stories: list[Story],
        payload: dict[str, Any],
    ) -> tuple[list[RankedBacklogItem], list[tuple[str, float | None, float | None]]]:
        """Build the ranked list + the persist payload.

        Iterates ``stories`` (each id seen once, no dedup needed) and
        looks up the LLM's scoring inputs by id. Stories the LLM
        omitted get a synthetic ``score=0`` row with a manual-review
        rationale so downstream planning never silently loses work;
        synthetic rows are *not* persisted (the existing scores on the
        row stay intact).
        """
        payload_by_id = _index_payload(payload)
        ranked: list[RankedBacklogItem] = []
        persist_rows: list[tuple[str, float | None, float | None]] = []

        for story in stories:
            raw = payload_by_id.get(story.id)
            if raw is None:
                logger.warning(
                    "ProductOwnerAgent: story %s missing from LLM output; including with score=0",
                    story.id,
                )
                ranked.append(_fallback_item(story, method, _MISSING_RATIONALE))
                continue

            inputs = raw.get("inputs")
            if not isinstance(inputs, dict) or not inputs:
                # `inputs` missing, non-object (e.g. `null`, list, string),
                # OR an empty dict. An empty dict would otherwise score
                # to a synthetic 0.0 and overwrite any real persisted
                # score on the row. Route through the malformed
                # fallback so the synthetic zero stays visible-only.
                logger.warning(
                    "ProductOwnerAgent: empty/non-dict inputs for story %s; including with score=0",
                    story.id,
                )
                ranked.append(_fallback_item(story, method, _MALFORMED_RATIONALE))
                continue
            try:
                score, wsjf_value, rice_value = _score_for(method, inputs, story)
            except (TypeError, ValueError, OverflowError):
                # Malformed inputs (un-coerceable strings, booleans,
                # integers too large for the float64 range, etc.).
                # Surface the story with score=0 + a malformed-inputs
                # rationale rather than dropping it silently — downstream
                # planning would otherwise lose this work item entirely.
                logger.warning(
                    "ProductOwnerAgent: malformed inputs for story %s; including with score=0",
                    story.id,
                )
                ranked.append(_fallback_item(story, method, _MALFORMED_RATIONALE))
                continue

            ranked.append(
                RankedBacklogItem(
                    kind="story",
                    id=story.id,
                    title=story.title,
                    score=score,
                    wsjf_score=wsjf_value,
                    rice_score=rice_value,
                    rationale=str(raw.get("rationale") or ""),
                )
            )
            persist_rows.append((story.id, wsjf_value, rice_value))

        return ranked, persist_rows
