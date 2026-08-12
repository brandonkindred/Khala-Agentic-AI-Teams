"""Strands Agent that reviews a proposed ``StrategySpec`` and emits a critique.

The reviewer sees only the spec and the deterministic readiness findings
already produced by :class:`SpecReadinessGate`; it does not see code,
and its output is a critique — never a revised spec, never code. The
orchestrator runs this agent in a bounded design-review loop, asking the
designer to ``revise`` until the reviewer's ``ready`` flag is true or
the round budget is exhausted.

Models (``CritiqueIssue`` / ``SpecCritique``) live alongside the agent
so consumers import a single module (mirrors ``alignment.py``).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field, ValidationError, model_validator
from strands import Agent

from llm_service.interface import LLMSemanticExhaustionError

from ...models import StrategySpec
from .._orchestrator_helpers import _has_short_period_stall
from ..exceptions import StrategyLabLLMError
from ..quality_gates.models import QualityGateResult
from . import _structured_output as so
from ._llm_budget import DesignBudgetExhausted, charge_active_budget
from ._llm_envelope import run_structured_agent
from ._parse_helpers import coerce_strict_bool as _shared_coerce_strict_bool
from ._parse_helpers import extract_json_object
from ._prompt_context import spec_prompt_fields
from ._response_schemas import CRITIQUE_SCHEMA
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


# Shared stop-order semantics reference (stop-market / stop-limit / trailing
# stop). Appended so the reviewer's risk-control check does not mislabel a
# trailing stop's above-entry ratchet — the intended gain-locking behavior —
# as a defect.
@functools.lru_cache(maxsize=None)
def _get_stop_order_semantics() -> str:
    """Load and cache shared stop-order semantics markdown.

    Preconditions: ``_PROMPT_DIR / "_stop_order_semantics.md"`` exists and is
    readable UTF-8 text when first invoked.
    Postconditions: returns a non-empty ``str``; subsequent calls return the
    same cached value without re-reading the file.
    Invariants: module import does not invoke this helper.
    """
    text = (_PROMPT_DIR / "_stop_order_semantics.md").read_text(encoding="utf-8")
    if not text:
        raise ValueError("_stop_order_semantics.md must be non-empty")
    return text


# Shared sizing/drawdown risk-framing reference (deployed size IS the
# per-trade loss cap; no max-drawdown constraint exists). Appended so the
# reviewer's sizing-related interpretation rules live in one canonical place
# instead of drifting inline copies.
@functools.lru_cache(maxsize=None)
def _get_sizing_risk_framing() -> str:
    """Load and cache shared sizing/drawdown risk-framing markdown.

    Preconditions: ``_PROMPT_DIR / "_sizing_risk_framing.md"`` exists and is
    readable UTF-8 text when first invoked.
    Postconditions: returns a non-empty ``str``; subsequent calls return the
    same cached value without re-reading the file.
    Invariants: module import does not invoke this helper.
    """
    text = (_PROMPT_DIR / "_sizing_risk_framing.md").read_text(encoding="utf-8")
    if not text:
        raise ValueError("_sizing_risk_framing.md must be non-empty")
    return text


@functools.lru_cache(maxsize=None)
def _get_system_prompt() -> str:
    """Build and cache the design-review system prompt (body + shared reference blocks).

    Preconditions: ``design_review_system.md``, stop-order semantics, and
    sizing/risk framing files exist when first invoked.
    Postconditions: returned string contains the design-review system body
    followed by the stop-order semantics text and the sizing/risk framing
    text, each separated by a blank line; subsequent calls return the same
    cached composed prompt without re-reading any file.
    Invariants: module import does not invoke this helper.
    """
    body = (_PROMPT_DIR / "design_review_system.md").read_text(encoding="utf-8")
    if not body:
        raise ValueError("design_review_system.md must be non-empty")
    return body + "\n\n" + _get_stop_order_semantics() + "\n\n" + _get_sizing_risk_framing()

# The JSON Schema the LLM response must conform to, rendered once for
# injection into the prompt (mirrors ``refinement._REFINEMENT_SCHEMA_JSON``).
_CRITIQUE_SCHEMA_JSON = json.dumps(CRITIQUE_SCHEMA, indent=2)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


_CRITIQUE_FIELDS: tuple[str, ...] = (
    "entry_rules",
    "exit_rules",
    "sizing",
    "target_symbols",
    "risk_limits",
    "timeframe",
    "hypothesis",
    "signal_definition",
    "expectancy_forecast",
)


# The LLM reviewer must not block on objections the deterministic
# ``SpecReadinessGate`` already owns, nor on the (removed) max-drawdown
# constraint. The orchestrator only invokes the reviewer once that gate has
# passed, so:
#   * a ``sizing`` objection re-litigates a cleared check (Rule 5 realisability
#     + Rule 9 deployed-vs-cap coherence) — including the recurring
#     deployed-size-vs-stop "0.25% per trade" misread; and
#   * an objection about the retired max-drawdown *limit* is moot — max drawdown
#     is not a constraint (a strategy may lose up to 100% by design).
# Both are demoted to ``info`` in :func:`_coerce_critique`.
#
# This is deliberately NOT a blanket ``risk_limits`` carve-out: the gate does
# NOT check every risk-limit failure mode (e.g. ``max_gross_leverage=0`` passes
# the gate yet makes ``RiskFilter.can_enter`` reject every order — a guaranteed
# zero-trade strategy), so a genuine non-drawdown ``risk_limits`` critique
# (leverage, open positions, concentration) MUST keep blocking readiness.

# Matches a reference to the *retired max-drawdown limit/constraint* — NOT a
# generic mention of "drawdown". A real exit-completeness defect such as
# "no stop-loss or drawdown-protection exit" must keep blocking, so only the
# limit phrasing ("max drawdown", "maximum drawdown", "max_drawdown_pct",
# "drawdown limit/cap/ceiling/threshold/constraint") is treated as demotable.
_MAX_DRAWDOWN_LIMIT_RE = re.compile(
    r"max(?:imum)?[\s_-]*drawdown(?:_pct)?"
    r"|drawdown[\s_-]+(?:limit|cap|constraint|ceiling|threshold)",
    re.IGNORECASE,
)

# Sizing kinds whose realisability/coherence the deterministic gate FULLY owns
# (Rule 5 realisability + Rule 9 deployed-vs-cap). ``volatility_target`` is
# deliberately absent: ``SpecReadinessGate._check_sizing_realisable`` abstains on
# it and only emits a *warning* (e.g. an implausible ``target_annual_vol``),
# which the orchestrator treats as ready — so for vol-target the LLM reviewer's
# sizing objection is the ONLY substantive check and must keep blocking.
_GATE_OWNED_SIZING_KINDS: frozenset[str] = frozenset({"fixed_fraction", "fixed_notional"})


def _sizing_owned_by_gate(sizing_kind: object) -> bool:
    """True when the deterministic gate fully validates this sizing kind.

    Pre: ``sizing_kind`` is the spec's ``sizing.kind`` (str) or None.
    Post: True iff ``sizing_kind`` is a gate-owned static kind; False for
    ``volatility_target`` (gate abstains) and for unknown/missing kinds (fail
    safe — keep a sizing objection blocking when we cannot confirm ownership).
    """
    return sizing_kind in _GATE_OWNED_SIZING_KINDS


def _is_demotable_issue(issue: "CritiqueIssue", *, sizing_owned: bool) -> bool:
    """True when ``issue`` re-litigates a deterministic-gate-owned check or the
    removed max-drawdown constraint, so the reviewer may not block on it.

    Pre: ``issue`` is a :class:`CritiqueIssue`; ``sizing_owned`` says whether the
    spec's sizing kind is one the deterministic gate fully validates.
    Post: returns True iff (the issue is a ``sizing`` objection AND
    ``sizing_owned``) OR it references the retired max-drawdown *limit* (per
    ``_MAX_DRAWDOWN_LIMIT_RE``). A ``sizing`` objection on a non-owned kind
    (``volatility_target``), a substantive objection that merely mentions the
    word "drawdown" (e.g. a missing drawdown-protection *exit*), and non-drawdown
    ``risk_limits`` objections are all left blocking.
    """
    if issue.field == "sizing":
        return sizing_owned
    return bool(_MAX_DRAWDOWN_LIMIT_RE.search(issue.description))


def _normalize_issue_text(text: str) -> str:
    """Normalise critique prose so trivial rewordings map to one identity.

    Pre: ``text`` is any string.
    Post: returns a lowercased, punctuation-stripped, whitespace-collapsed
    rendering. Two descriptions that differ only in case, punctuation, or
    spacing produce identical output, so :func:`compute_issue_id` assigns
    them the same id.
    """
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compute_issue_id(field_name: str, description: str) -> str:
    """Deterministically derive a stable id for one critique issue.

    Pre: ``field_name`` and ``description`` are strings.
    Post: returns ``"{field}:{hash10}"`` where the hash is the first 10
    hex chars of the SHA-256 of ``"{field}|{normalized(description)}"``.
    The id is derived in code (never trusted from the LLM, which cannot
    reliably carry ids across rounds), so the *same* defect maps to the
    *same* id round after round — the basis for the regression guard.
    Mirrors the canonical-hash idiom in
    :meth:`ConvergenceTracker._strategy_signature`.
    """
    norm = _normalize_issue_text(description)
    digest = hashlib.sha256(f"{field_name}|{norm}".encode("utf-8")).hexdigest()[:10]
    return f"{field_name}:{digest}"


class CritiqueIssue(BaseModel):
    """A single point on which the reviewer thinks the spec is not ready.

    ``field`` names the spec field the issue applies to (or the closest
    proxy when the issue is cross-cutting). ``severity`` follows the same
    ladder as :class:`QualityGateResult` so downstream consumers can mix
    deterministic findings and reviewer critiques into one timeline.
    ``issue_id`` is a deterministic identity (see :func:`compute_issue_id`)
    so a defect resolved in one round and reintroduced in a later one is
    recognised as the *same* issue by :class:`CritiqueLedger`.
    """

    field: str = Field(
        description=(
            "Spec field the issue applies to: one of "
            "'entry_rules' | 'exit_rules' | 'sizing' | 'target_symbols' | "
            "'risk_limits' | 'timeframe' | 'hypothesis' | 'signal_definition' | "
            "'expectancy_forecast'."
        )
    )
    severity: Literal["info", "warning", "critical"] = "warning"
    description: str
    suggested_fix: str = ""
    issue_id: str = Field(
        default="",
        description=(
            "Deterministic id derived from (field, normalized description). "
            "Auto-filled when blank so every construction site — LLM-parsed, "
            "synthetic, and legacy-deserialized — carries a stable identity."
        ),
    )

    @model_validator(mode="after")
    def _ensure_issue_id(self) -> "CritiqueIssue":
        """Invariant: every ``CritiqueIssue`` carries a non-empty ``issue_id``.

        Post: ``issue_id`` is populated from ``(field, description)`` when it
        was left blank. An explicitly supplied id is preserved verbatim so
        persisted/legacy rows round-trip unchanged.
        """
        if not self.issue_id:
            self.issue_id = compute_issue_id(self.field, self.description)
        return self


class SpecCritique(BaseModel):
    """Verdict from one design-review round.

    ``ready`` is the only field the orchestrator branches on. When
    ``ready=True`` the loop exits and the spec advances to code synthesis;
    when ``ready=False`` the designer's ``revise`` method must address
    every issue here before the next review round.
    """

    ready: bool
    rationale: str = ""
    issues: List[CritiqueIssue] = Field(default_factory=list)
    readiness_findings: List[str] = Field(
        default_factory=list,
        description=(
            "Snapshot of deterministic SpecReadinessGate findings the "
            "reviewer was shown, persisted so the audit trail shows the "
            "full set of inputs to the verdict."
        ),
    )
    round: int = 0

    @property
    def open_issue_ids(self) -> Set[str]:
        """The set of *blocking* issue ids this round leaves open.

        Post: returns the ``issue_id`` of every issue at ``warning`` or
        ``critical`` severity. ``info`` issues are advisory and never count
        as "open", so they cannot keep the loop from converging. This set is
        the unit the :class:`CritiqueLedger` tracks across rounds.
        """
        return {i.issue_id for i in self.issues if i.severity in ("warning", "critical")}


class DesignReviewError(Exception):
    """Raised when the LLM call or response parsing fails inside
    :class:`DesignReviewAgent`. The orchestrator falls closed on this
    (``ready=False`` with a synthetic critical issue) so a reviewer
    transport hiccup cannot silently advance a half-validated spec."""


@dataclass(frozen=True)
class LedgerDelta:
    """Per-round bookkeeping produced by :meth:`CritiqueLedger.record_round`.

    All four sets partition the *change* in the blocking open-issue set:
      * ``resolved`` — ids that were open last round and are gone now (progress).
      * ``persisted`` — ids open both last round and this round.
      * ``new`` — ids appearing for the first time on this lineage.
      * ``regressed`` — ids that were previously *resolved* and have
        reappeared (the reviser reintroduced a fixed defect, or the reviewer
        re-raised a dropped one). This is the signal the loop escalates on.
    """

    round: int
    resolved: Set[str] = field(default_factory=set)
    persisted: Set[str] = field(default_factory=set)
    new: Set[str] = field(default_factory=set)
    regressed: Set[str] = field(default_factory=set)


class CritiqueLedger:
    """Monotonic open-issue tracker for one design ↔ review loop.

    The reviewer emits free-text critiques; each issue carries a stable
    :attr:`CritiqueIssue.issue_id`. The ledger watches the *blocking*
    open-issue set (warnings + criticals) round over round so the loop can
    (a) escalate regressions — a resolved id that reappears — and (b) detect
    a stall — the open set unchanged for N consecutive rounds.

    Invariants:
      * ``current_open`` is exactly the open-issue set of the most recently
        recorded round (empty before the first ``record_round``).
      * ``ever_resolved`` only grows; an id enters it the round it leaves the
        open set and never departs (so its later reappearance is a regression).
    """

    def __init__(self) -> None:
        self._current_open: Set[str] = set()
        self._ever_resolved: Set[str] = set()
        self._open_history: List[frozenset] = []
        self._total_regressed: int = 0
        self._rounds: int = 0

    def record_round(self, critique: "SpecCritique") -> LedgerDelta:
        """Fold one round's critique into the ledger and return the delta.

        Pre: ``critique`` is the :class:`SpecCritique` for the round being
        recorded; rounds are recorded in loop order.
        Post: ``current_open`` equals ``critique.open_issue_ids``;
        ``ever_resolved`` has absorbed any ids that just left the open set;
        the returned :class:`LedgerDelta` describes the transition. An id
        counts as ``regressed`` only when it both reappears *and* was in
        ``ever_resolved`` before this round.
        """
        new_open = critique.open_issue_ids
        prev_open = self._current_open

        resolved = prev_open - new_open
        persisted = prev_open & new_open
        appeared = new_open - prev_open
        regressed = appeared & self._ever_resolved
        genuinely_new = appeared - self._ever_resolved

        # Update state: newly-resolved ids are remembered forever so a later
        # reappearance is recognised as a regression rather than a new issue.
        self._ever_resolved |= resolved
        self._current_open = set(new_open)
        self._open_history.append(frozenset(new_open))
        self._total_regressed += len(regressed)
        delta = LedgerDelta(
            round=self._rounds,
            resolved=resolved,
            persisted=persisted,
            new=genuinely_new,
            regressed=regressed,
        )
        self._rounds += 1
        return delta

    def is_stalled(self, n: int) -> bool:
        """True when the open set shows no real progress for ``n`` rounds.

        Recognizes both a window of identical, non-empty open-issue sets and
        short-period oscillating signatures (e.g. an A/B/A/B... 2-cycle
        between two distinct open-issue sets) — see
        :func:`_orchestrator_helpers._has_short_period_stall`.

        Pre: ``n`` is the consecutive-round threshold (sub-1 values are
        floored to 1).
        Post: returns True only when at least ``n`` rounds have been recorded
        and the last ``n`` open-issue sets are exactly periodic with some
        period ``p <= n // 2`` (or the window is a single round), and the
        current open set is non-empty. An empty open set is never a stall
        (the loop is converging, not oscillating).
        """
        n = max(n, 1)
        if not self._current_open:
            return False
        if len(self._open_history) < n:
            return False
        return _has_short_period_stall(self._open_history[-n:])

    @property
    def current_open(self) -> Set[str]:
        """A copy of the most recent round's blocking open-issue set."""
        return set(self._current_open)

    @property
    def ever_resolved(self) -> Set[str]:
        """A copy of every id that has, at some point, left the open set."""
        return set(self._ever_resolved)

    @property
    def total_regressed(self) -> int:
        """Cumulative count of regression events across all recorded rounds."""
        return self._total_regressed


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------


_REVIEW_USER_TEMPLATE = """\
Review the strategy specification below and decide whether it is implementable.

## Candidate Strategy Specification
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal definition: {signal_definition}
Timeframe: {timeframe}
Entry rules:
{entry_rules}
Exit rules:
{exit_rules}
Sizing: {sizing_rules}
Target symbols: {target_symbols}
Risk limits: {risk_limits}
Speculative: {speculative}

## Deterministic Readiness Findings ({n_readiness} run)
{readiness_block}

## Prior Critiques on this lineage ({n_prior_critiques})
{prior_critiques_block}

## Instructions

You are the only LLM in this loop. The deterministic gate already
catches mechanical errors — do not duplicate them. Focus on substantive
defects: thesis coherence, signal alignment, exit/risk-control
completeness, universe ↔ thesis fit, and hand-wavy or measurable-edge-
absent specifications. Sizing and `risk_limits` arithmetic are owned by
the deterministic gate — do NOT block on them (any sizing / risk_limits
issue you raise is advisory only), and there is NO max-drawdown
constraint: a strategy may lose up to 100% by design, so never flag
drawdown reachability.

Your response MUST conform to this JSON Schema:

```json
{response_schema_json}
```

Return ONLY a JSON object — no markdown — with this shape (a
representative example — the schema above is authoritative):

{{
  "ready": false,
  "rationale": "1-3 sentences",
  "issues": [
    {{
      "field": "entry_rules | exit_rules | sizing | target_symbols | risk_limits | timeframe | hypothesis | signal_definition | expectancy_forecast",
      "severity": "info | warning | critical",
      "description": "what's wrong",
      "suggested_fix": "concrete revision the designer should apply"
    }}
  ]
}}

- ``ready=true`` ONLY when no deterministic finding is critical AND you cannot identify a substantive defect.
- ``ready=false`` requires at least one entry in ``issues``.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DesignReviewAgent:
    """Review a proposed ``StrategySpec`` and return a :class:`SpecCritique`.

    Contract:
      Pre — ``spec`` is a constructed ``StrategySpec``;
            ``readiness_results`` is the deterministic gate output the
            orchestrator already ran on the same spec.
      Post — returns a :class:`SpecCritique`. The agent never returns
             code or a revised spec.
      Invariant — on LLM transport failure or unparseable JSON, the
             agent falls closed with ``ready=False`` and a single
             critical ``review_parse_error`` issue. This matches the
             fail-closed treatment in :class:`TradeAlignmentAgent`.
    """

    def run(
        self,
        spec: StrategySpec,
        readiness_results: Optional[List[QualityGateResult]] = None,
        prior_critiques: Optional[List[SpecCritique]] = None,
    ) -> SpecCritique:
        """Run one design-review round.

        Returns a :class:`SpecCritique`. Never raises for a reviewer/transport
        hiccup — those produce a fail-closed critique so the orchestrator's
        loop never stalls. The one exception is ``DesignBudgetExhausted``
        from ``charge_active_budget()``: a per-cycle budget trip is a
        cycle-level stop and is allowed to propagate (it is charged *before*
        the fail-closed ``try`` below).
        """
        readiness_list = readiness_results or []
        readiness_block, readiness_findings = _format_readiness(readiness_list)
        prior_block = format_prior_critiques(prior_critiques)

        user_prompt = _REVIEW_USER_TEMPLATE.format(
            **spec_prompt_fields(spec),
            timeframe=spec.timeframe,
            target_symbols=list(spec.target_symbols),
            speculative=spec.speculative,
            n_readiness=len(readiness_list),
            readiness_block=readiness_block,
            n_prior_critiques=len(prior_critiques or []),
            prior_critiques_block=prior_block,
            response_schema_json=_CRITIQUE_SCHEMA_JSON,
        )

        def _invoke_legacy() -> Dict[str, Any]:
            agent = Agent(
                model=get_strands_model("strategy_design_review"),
                system_prompt=_get_system_prompt(),
                tools=[],
            )
            return run_structured_agent(
                agent,
                user_prompt,
                agent_key="strategy_design_review",
                phase="design_review",
                parse=extract_json_object,
                charge=False,
                logger=logger,
            )

        # Charge outside the fail-closed ``try`` so DesignBudgetExhausted
        # propagates to ``_run_design_loop`` instead of being converted into
        # a fail-closed critique that would let the loop continue past budget.
        # Structured path: ``invoke_structured_with_schema(charge=True)``
        # charges once per provider call inside its retried closure
        # (reasoning + formatting). Legacy path: charge once here for the
        # single provider call.
        structured_available = so.structured_output_available()
        if not structured_available:
            charge_active_budget()

        try:
            if structured_available:
                try:
                    parsed = so.invoke_structured_with_schema(
                        "strategy_design_review",
                        _get_system_prompt(),
                        user_prompt,
                        phase="design_review_structured",
                        schema=CRITIQUE_SCHEMA,
                        charge=True,
                        objective="strategy design review (structured)",
                        logger=logger,
                        reasoning_system_prompt=so.build_reasoning_system_prompt(
                            _get_system_prompt()
                        ),
                    )
                except StrategyLabLLMError as exc:
                    cause = exc.cause
                    if not (isinstance(cause, LLMSemanticExhaustionError) and cause.schema_forced):
                        raise
                    logger.warning(
                        "structured design-review decode starved (schema_forced); "
                        "degrading to the legacy single-shot call."
                    )
                    # The schema_forced degrade path makes an additional real
                    # provider call after the structured attempt already charged
                    # for whichever sub-calls ran — charge for the legacy
                    # fallback here before invoking it.
                    charge_active_budget()
                    parsed = _invoke_legacy()
                else:
                    logger.info(
                        "strategy_lab structured_output outcome=succeeded "
                        "agent=strategy_design_review phase=design_review_structured",
                    )
            else:
                parsed = _invoke_legacy()
        except DesignBudgetExhausted:
            # Must propagate uncaught — same rationale as the pre-try charges
            # above (a per-cycle budget trip is a cycle-level stop, not a
            # reviewer/transport hiccup to fail closed on).
            raise
        except Exception as exc:  # noqa: BLE001 — fail-closed on any LLM/parse fault
            logger.warning("DesignReviewAgent failed to produce parseable JSON: %s", exc)
            return _fail_closed_critique(exc, readiness_findings)

        critique = _coerce_critique(
            parsed,
            readiness_findings,
            sizing_owned=_sizing_owned_by_gate(getattr(spec.sizing, "kind", None)),
        )
        return critique


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_readiness(results: List[QualityGateResult]) -> tuple[str, List[str]]:
    """Render readiness findings as a deterministic block, return findings list.

    Pre: ``results`` is a list of ``QualityGateResult``.
    Post: returns ``(block_text, findings_list)`` where ``findings_list``
    is the same shape persisted on the resulting :class:`SpecCritique`'s
    ``readiness_findings`` field.
    """
    if not results:
        return "(no findings)", []
    lines: List[str] = []
    findings: List[str] = []
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        line = f"  [{status}|{r.severity}] {r.details}"
        lines.append(line)
        findings.append(f"{r.severity}: {r.details}")
    return "\n".join(lines), findings


_CRITIQUE_PREVIEW_CHARS = 160


def format_prior_critiques(prior: Optional[List[SpecCritique]]) -> str:
    """Render past critiques so the reviewer / designer does not re-raise or regress resolved issues.

    Pre: ``prior`` is ``None``, ``[]``, or a list of ``SpecCritique``.
    Post: returns ``"None yet."`` when empty; otherwise one block per
    critique — a header line (round, ready flag, issue count, truncated
    rationale) followed by one indented line per issue carrying its
    severity, field, truncated description, and truncated ``suggested_fix``
    (when present). Surfacing the per-issue detail — not just the rationale —
    lets a later revision see *what* an earlier round fixed so it does not
    silently regress it when the rationale was terse. Shared between
    :class:`DesignReviewAgent` (showing past rounds to the reviewer) and
    :meth:`DesignAgent.revise` / :meth:`DesignAgent._with_self_review`
    (showing them to the designer) so all three see the same lineage view.
    """
    if not prior:
        return "None yet."
    lines: List[str] = []
    for c in prior:
        lines.append(
            f"  Round {c.round}: ready={c.ready} ({len(c.issues)} issues) — "
            f"{c.rationale[:_CRITIQUE_PREVIEW_CHARS]}"
        )
        for issue in c.issues:
            detail = (
                f"      - [{issue.severity}] {issue.field}: "
                f"{issue.description[:_CRITIQUE_PREVIEW_CHARS]}"
            )
            if issue.suggested_fix:
                detail += f" (fix: {issue.suggested_fix[:_CRITIQUE_PREVIEW_CHARS]})"
            lines.append(detail)
    return "\n".join(lines)


_SEVERITY_ORDER: Dict[str, int] = {"info": 0, "warning": 1, "critical": 2}


def _coerce_critique(
    parsed: Dict[str, Any],
    readiness_findings: List[str],
    *,
    demote_min_severity: Literal["warning", "critical"] = "warning",
    sizing_owned: bool = True,
) -> SpecCritique:
    """Convert a parsed LLM JSON dict into a :class:`SpecCritique`.

    Tolerant of mild schema drift so a single off-spec issue doesn't
    discard the rest of the review. Issues with unknown ``field`` values
    are remapped onto a permissive default (`hypothesis`) so the
    designer still sees the critique text.

    Fail-closed coercions (both required so the design loop cannot
    advance to code synthesis on a contradicted verdict):

    * ``ready`` is parsed strictly — only real ``bool`` or the literal
      strings ``"true"`` / ``"false"`` (case-insensitive) are honoured.
      Anything else (including ``"yes"``, ``""``, an int) defaults to
      ``False``.
    * ``ready=True`` alongside an issue at or above ``demote_min_severity``
      is treated as reviewer self-contradiction and demoted to
      ``ready=False``. The loop then keeps iterating rather than promoting
      an unreviewed spec on the strength of a flag that contradicts its
      own findings.

    ``demote_min_severity`` selects which severities count as blocking for
    that demotion. The default ``"warning"`` keeps the external
    ``DesignReviewAgent`` semantics (any non-info issue demotes). The
    internal self-review path passes ``"critical"`` so advisory warnings
    on an otherwise-ready verdict are accepted verbatim instead of burning
    a self-revision round.

    Deterministic-gate carve-out: issues for which :func:`_is_demotable_issue`
    is true are demoted to ``info`` before the blocking computation — i.e.
    ``sizing`` objections *when* ``sizing_owned`` (the spec's sizing kind is one
    the gate fully validates), and any objection referencing the retired
    max-drawdown *limit* (max drawdown is not a constraint). ``sizing_owned`` is
    ``False`` for ``volatility_target`` (the gate abstains and only warns, so the
    reviewer's plausibility objection is the only substantive check) — such a
    sizing objection keeps blocking. Non-drawdown ``risk_limits`` objections
    (e.g. ``max_gross_leverage=0``) are likewise never demoted. A not-ready
    verdict whose ONLY blocking objections were demotable ones is promoted to
    ``ready=True`` (with an audit-trail ``info`` note) rather than left to churn
    the loop on a veto the reviewer may not cast.

    Preconditions: ``parsed`` is a dict from a parsed LLM JSON payload;
    ``demote_min_severity`` is one of ``"warning"`` / ``"critical"``.
    Postconditions: returns a :class:`SpecCritique`. Demotable issues (see
    :func:`_is_demotable_issue`) never carry a blocking severity (demoted to
    ``info``). ``ready`` is ``False`` whenever a non-demotable blocking issue is
    present; when ``ready`` is ``False`` the ``open_issue_ids`` set is non-empty
    (a blocking issue is synthesised if the reviewer named none, or only ``info``
    notes, with no demoted blocking objection to promote on).
    """
    ready = _coerce_strict_bool(parsed.get("ready"))
    rationale = str(parsed.get("rationale", "")).strip()
    raw_issues = parsed.get("issues")
    if not isinstance(raw_issues, list):
        raw_issues = []

    issues: List[CritiqueIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field", "hypothesis"))
        if field not in _CRITIQUE_FIELDS:
            field = "hypothesis"
        severity_raw = str(raw.get("severity", "warning"))
        severity: Literal["info", "warning", "critical"] = (
            severity_raw if severity_raw in ("info", "warning", "critical") else "warning"
        )
        try:
            issues.append(
                CritiqueIssue(
                    field=field,
                    severity=severity,
                    description=str(raw.get("description", "")),
                    suggested_fix=str(raw.get("suggested_fix", "")),
                )
            )
        except (TypeError, ValueError, ValidationError):
            # Best-effort: one bad item shouldn't bin the rest.
            continue

    # Demote gate-owned sizing objections and max-drawdown-limit objections to
    # ``info``: the deterministic gate owns sizing for gate-owned kinds and max
    # drawdown is not a constraint, so the reviewer must not veto on them (see
    # ``_is_demotable_issue``). A ``volatility_target`` sizing objection (gate
    # abstains) and non-drawdown ``risk_limits`` critiques (e.g.
    # ``max_gross_leverage=0``) are left untouched so genuine mechanically-
    # unusable settings still block. ``demoted_blocking`` records
    # how many *blocking* (warning/critical) issues were demoted this way, so a
    # not-ready verdict whose ONLY blocking objections were these can advance
    # rather than churn the loop on a veto the reviewer is not allowed to cast.
    demoted_blocking = 0
    for issue in issues:
        if _is_demotable_issue(issue, sizing_owned=sizing_owned) and issue.severity in (
            "warning",
            "critical",
        ):
            issue.severity = "info"
            demoted_blocking += 1

    # Contract: ready=True alongside an issue at or above the demotion
    # threshold is incoherent. Demote the verdict and append a synthetic
    # issue so the lineage records *why* we overrode the reviewer, not
    # just that we did.
    threshold = _SEVERITY_ORDER[demote_min_severity]
    blocking_issues = [i for i in issues if _SEVERITY_ORDER[i.severity] >= threshold]
    if ready and blocking_issues:
        ready = False
        issues.append(
            CritiqueIssue(
                field="hypothesis",
                severity="critical",
                description=(
                    "Reviewer returned ready=true alongside "
                    f"{len(blocking_issues)} blocking issue(s) at or above "
                    f"'{demote_min_severity}'; demoting to ready=false so the "
                    "contradicted verdict cannot promote an unreviewed spec."
                ),
                suggested_fix=(
                    f"Resolve the listed issues; the reviewer must only set "
                    f"ready=true when no issue at or above '{demote_min_severity}' "
                    "remains."
                ),
            )
        )

    # Contract: a ``ready=False`` verdict must carry at least one *blocking*
    # (warning/critical) issue. The reviewer keeps the loop revising whenever
    # ``ready`` is false, but ``SpecCritique.open_issue_ids`` — the unit the
    # CritiqueLedger and stall detector track — counts only blocking issues.
    # A not-ready critique with no issues at all, or with only ``info`` issues,
    # would otherwise present an empty open set: the loop would churn to the
    # hard round cap with telemetry claiming zero open issues and stall
    # detection unable to fire. Synthesise a blocking placeholder (preserving
    # any info notes) so the not-ready signal is always backed by a tracked
    # open issue. ``info`` issues remain advisory and never block on their own.
    has_blocking = any(_SEVERITY_ORDER[i.severity] >= _SEVERITY_ORDER["warning"] for i in issues)
    if not ready and not has_blocking:
        if demoted_blocking:
            # The reviewer's only blocking objections were sizing or drawdown
            # concerns — the deterministic gate owns sizing and max drawdown is
            # not a constraint, so the reviewer may not block on them. With
            # nothing blockable left to act on, advance the spec rather than
            # churn the design loop to the round cap on a veto we have already
            # neutralised. Record the override as an info note so the lineage
            # shows why a not-ready verdict was promoted.
            ready = True
            issues.append(
                CritiqueIssue(
                    field="sizing",
                    severity="info",
                    description=(
                        "Reviewer's only blocking objection(s) were sizing or "
                        "drawdown concerns, which the deterministic readiness "
                        "gate owns; demoted to advisory and verdict promoted to "
                        "ready (max drawdown is not a constraint; deployed size, "
                        "not fraction×stop, is the per-trade risk)."
                    ),
                    suggested_fix="",
                )
            )
        else:
            issues.append(
                CritiqueIssue(
                    field="hypothesis",
                    severity="warning",
                    description=(
                        rationale or "Reviewer reported not-ready without naming an issue."
                    ),
                    suggested_fix="Tighten the hypothesis or rule definitions.",
                )
            )

    return SpecCritique(
        ready=ready,
        rationale=rationale,
        issues=issues,
        readiness_findings=list(readiness_findings),
    )


def _coerce_strict_bool(raw: Any) -> bool:
    """Strict-mode boolean coercion for reviewer ``ready`` flags.

    Pre: ``raw`` is any value extracted from a parsed LLM JSON payload.
    Post: returns a real ``bool``. Recognised:
        * real ``True`` / ``False``
        * case-insensitive ``"true"`` / ``"false"``
    Everything else (``None``, ``""``, ``"yes"``, ``1``, etc.) defaults
    to ``False`` — fail closed so a stray non-string never advances the
    design loop past a reviewer that did not actually say "ready".
    """
    return _shared_coerce_strict_bool(raw)


def _fail_closed_critique(exc: Exception, readiness_findings: List[str]) -> SpecCritique:
    """Build the fail-closed critique used when the reviewer LLM fails."""
    return SpecCritique(
        ready=False,
        rationale=(
            f"DesignReviewAgent fell closed: {type(exc).__name__}: {exc}. "
            "Treat as not-ready until the next round produces a parseable verdict."
        ),
        issues=[
            CritiqueIssue(
                field="hypothesis",
                severity="critical",
                description=(f"review_parse_error: {type(exc).__name__}: {exc}"),
                suggested_fix=(
                    "Re-emit the spec; if the failure recurs, the design loop will abort the cycle."
                ),
            )
        ],
        readiness_findings=list(readiness_findings),
    )


__all__ = [
    "CritiqueIssue",
    "CritiqueLedger",
    "DesignReviewAgent",
    "DesignReviewError",
    "LedgerDelta",
    "SpecCritique",
    "compute_issue_id",
    "format_prior_critiques",
]
