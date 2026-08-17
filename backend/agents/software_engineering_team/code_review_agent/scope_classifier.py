"""Batched, parallel LLM in/out-of-scope classification for review findings.

A code review over a pull request surfaces two very different kinds of finding:
defects the change under review actually introduced (in-scope — worth a PR
comment) and pre-existing defects in unrelated, unchanged code the reviewer
merely noticed in passing (out-of-scope — better routed to a proposal for a
human than posted as a comment on this PR).

This module is the dedicated LLM pass that draws that line. :func:`classify_scope`
takes the finding list, groups the findings by their cited file, caps each group
at ``CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP`` findings per batch, and fans the
batches out across the shared map-parallelism budget
(``shared.concurrency.parallel_map``). Each batch is one lightweight
``complete_json`` call — no reasoning agent and no file-read tools; the heavier
tool-grounded verifier lives in :mod:`scope_filter`.

The pass is **fail-safe by design**: a missing client or any per-batch failure
(LLM error, malformed reply) degrades the affected findings to the ``"unknown"``
verdict (``in_scope=None``) so a caller can fall back to the free heuristic.
:func:`classify_scope` never raises and always returns a verdict positionally
aligned 1:1 with its input.

Relationship to :mod:`scope_filter` and status:
    This is a lightweight, standalone building block — it is **not yet wired
    into the coordinator's tail-pass pipeline**, so it currently emits no
    verdicts into a live review and cannot conflict with any other pass. The
    existing :func:`scope_filter.apply_scope_verification` remains the wired-in
    scope pass: it is heavier (a tool-grounded reasoning agent using the
    added/modified/deleted line maps) and tags findings ``pre_existing``. This
    module instead does one bounded ``complete_json`` call per file batch and
    returns a structured :class:`ScopeClassification`. Reconciling the two into
    a single wired-in source of scope truth is deliberately left to the
    follow-up work that integrates this pass; until then the caller owns which
    verdict it consumes.

Model resolution mirrors the sibling verification passes
(:func:`false_positive_filter.filter_false_positives`,
:func:`scope_filter.apply_scope_verification`): the ``llm`` client is supplied
by the caller, which is responsible for resolving the ``code_review_verify``
model (e.g. via :func:`model_resolution.resolve_code_review_verify_model`) so
this pass uses the same lighter verify model, pin, and failover behavior as its
siblings rather than self-resolving a heavier ``code_review`` client.

Invariants:
    - The returned list has exactly ``len(issues)`` elements, element ``i`` being
      the verdict for ``issues[i]``.
    - Every element is a :class:`ScopeClassification`; a finding with no parsed
      verdict (degraded batch, missing index) is :data:`UNKNOWN`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from llm_service.interface import LLMClient
from shared.concurrency import parallel_map
from software_engineering_team.shared.context_sizing import parse_env_int

from ._llm_client_utils import is_unscripted_dummy
from ._prompt_utils import _cap_context_field, _render_finding_block, _truncate_with_marker
from .models import CodeReviewInput, CodeReviewIssue

logger = logging.getLogger(__name__)

# Default cap on how many findings for one cited file go into a single LLM
# batch; larger groups are split into several batches. Mirrors
# ``CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP`` in false_positive_filter, but a
# smaller default keeps each scope call focused. Env-overridable, floor 1.
DEFAULT_SCOPE_MAX_FINDINGS_PER_GROUP = 20

# Cap on the inlined cited-file excerpt so a large file cannot blow the prompt.
_FILE_EXCERPT_CHARS = 6_000
_FILE_EXCERPT_TRUNCATION_MARKER = "\n… (truncated) …"

# Tokens a model may use for ``in_scope`` beyond a real JSON bool.
_IN_SCOPE_TOKENS = frozenset({"in_scope", "in-scope", "inscope", "yes", "true", "y"})
_OUT_OF_SCOPE_TOKENS = frozenset({"out_of_scope", "out-of-scope", "outofscope", "no", "false", "n"})

SCOPE_CLASSIFY_SYSTEM_PROMPT = (
    "You are a meticulous code-review triage assistant. For each finding you are "
    "given, decide whether it is IN SCOPE or OUT OF SCOPE for the pull request "
    "under review.\n\n"
    "- IN SCOPE: the finding is a defect the change under review introduced or is "
    "directly responsible for — a bug in code this PR added or modified, or a "
    "required change the PR should have made but omitted.\n"
    "- OUT OF SCOPE: the finding is a pre-existing defect in unrelated, unchanged "
    "code that merely happens to be near the change — it was already there before "
    "this PR and the PR is not responsible for it.\n\n"
    "Judge only from the evidence provided. When you genuinely cannot tell, say so "
    "rather than guessing."
)

SCOPE_CLASSIFY_FORMATTING_INSTRUCTIONS = (
    "Reply with a single JSON object and nothing else, in exactly this shape:\n"
    '{"verdicts": [{"index": <int>, "in_scope": <true|false|"unknown">, '
    '"reason": "<one short sentence>"}]}\n'
    "Include one entry per finding, using the finding's index. Set in_scope to "
    "true for IN SCOPE and false for OUT OF SCOPE. If you truly cannot decide a "
    'finding, omit it (or set its in_scope to "unknown").'
)


@dataclass(frozen=True)
class ScopeClassification:
    """One in/out-of-scope verdict for a single finding.

    Invariants:
        - ``in_scope`` is ``True`` (the finding is in-scope for the PR),
          ``False`` (out-of-scope / pre-existing), or ``None`` ("unknown" — the
          classifier could not decide or the batch degraded, so a caller should
          fall back to its heuristic).
        - ``reason`` is a short human-readable justification; it may be blank
          (e.g. for an ``unknown`` verdict from a degraded batch).
    """

    in_scope: Optional[bool]
    reason: str = ""


# The verdict every undecidable / degraded finding collapses to. Immutable and
# safe to share because ``ScopeClassification`` is a frozen dataclass.
UNKNOWN = ScopeClassification(in_scope=None, reason="")


def _max_findings_per_group() -> int:
    """Configured per-file batch cap for scope classification.

    Postconditions:
        - Returns ``CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP`` (default
          :data:`DEFAULT_SCOPE_MAX_FINDINGS_PER_GROUP`) floored at 1. Never
          raises for a bad environment value (garbage → default).
    """
    return parse_env_int(
        "CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP",
        DEFAULT_SCOPE_MAX_FINDINGS_PER_GROUP,
        1,
    )


def _scope_parallelism() -> int:
    """Fan-out width for scope batches — the shared map-parallelism budget.

    Consumes the package's public ``chunking.map_parallelism`` accessor
    (``CODE_REVIEW_MAP_PARALLELISM`` clamped by the process-global LLM gate)
    rather than adding a third concurrency knob, so this pass depends on a stable
    public API, not another module's private ``_map_parallelism`` symbol.
    Imported lazily to keep module import light.

    Postconditions:
        - Returns an ``int >= 1``. Never raises.
    """
    from .chunking import map_parallelism

    return map_parallelism()


def _coerce_in_scope(value: Any) -> Optional[bool]:
    """Map a model-supplied ``in_scope`` value to the tri-state verdict.

    Preconditions: ``value`` may be any JSON-decoded value.
    Postconditions:
        - A real ``bool`` maps to itself. A recognized in-scope / out-of-scope
          token (case-insensitive, whitespace-tolerant) maps to ``True`` /
          ``False``. Anything else (``"unknown"``, ``None``, a number, an
          unrecognized string) maps to ``None``. Pure; never raises.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _IN_SCOPE_TOKENS:
            return True
        if token in _OUT_OF_SCOPE_TOKENS:
            return False
    return None


def _parse_classifications(data: object, count: int) -> Dict[int, ScopeClassification]:
    """Map a classifier reply to ``{index: ScopeClassification}`` for valid indices.

    Preconditions: ``count >= 0`` is the number of findings in the batch.
    Postconditions:
        - Reads the ``verdicts`` value from ``data`` via ``.get`` (missing →
          treated as absent); each in-range, non-duplicate entry with an
          integer ``index`` in ``[0, count)`` yields a verdict. The first entry
          for an index wins; later duplicates are dropped. A ``bool`` index is
          rejected (``bool`` is an ``int`` subclass). Malformed replies yield
          ``{}``. Pure; never raises.
    """
    if not isinstance(data, dict):
        return {}
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return {}
    verdicts: Dict[int, ScopeClassification] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_index = item.get("index")
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            continue
        if not (0 <= raw_index < count) or raw_index in verdicts:
            continue
        in_scope = _coerce_in_scope(item.get("in_scope"))
        reason = str(item.get("reason", "") or "").strip()
        verdicts[raw_index] = ScopeClassification(in_scope=in_scope, reason=reason)
    return verdicts


def _file_excerpt(file_path: str, files: Optional[Mapping[str, str]]) -> str:
    """Return the cited file's content, capped, or empty when unavailable.

    Postconditions:
        - Returns ``files.get(file_path)`` truncated to
          :data:`_FILE_EXCERPT_CHARS` (with a marker) when present and
          non-empty; otherwise ``""`` (missing ``files`` or key → ``""``). Never
          raises.
    """
    if not files:
        return ""
    content = files.get(file_path) or ""
    if not content:
        return ""
    return _truncate_with_marker(content, _FILE_EXCERPT_CHARS, _FILE_EXCERPT_TRUNCATION_MARKER)


def _build_classify_prompt(
    file_path: str,
    issues: Sequence[CodeReviewIssue],
    input_data: Optional[CodeReviewInput],
) -> str:
    """Build the user prompt classifying one batch of findings for one file.

    Preconditions: ``issues`` is a non-empty batch of findings all cited against
        ``file_path``.
    Postconditions:
        - Names ``file_path``, inlines the PR task description / requirements /
          acceptance criteria (capped) and the cited file excerpt when present,
          then indexes the findings via ``_render_finding_block``. Ends with the
          strict-JSON formatting instructions. Never raises.
    """
    parts: List[str] = []
    if input_data is not None:
        desc = _cap_context_field(input_data.task_description or "")
        req = _cap_context_field(input_data.task_requirements or "")
        if desc:
            parts.extend(["**Pull request / task description:**", desc, ""])
        if req:
            parts.extend(["**Task requirements / PR body:**", req, ""])
        ac = [str(x).strip() for x in (input_data.acceptance_criteria or []) if str(x).strip()]
        if ac:
            parts.append("**Acceptance criteria:**")
            parts.extend(f"- {_cap_context_field(item)}" for item in ac)
            parts.append("")
    excerpt = _file_excerpt(file_path, input_data.files if input_data is not None else None)
    if excerpt:
        parts.extend([f"**Current source of `{file_path}`:**", excerpt, ""])
    parts.extend(
        [
            f"**Findings below are all cited against `{file_path}`.**",
            "Classify each as in-scope or out-of-scope for this pull request.",
            "",
        ]
    )
    for i, issue in enumerate(issues):
        parts.extend(_render_finding_block(i, issue))
        parts.append("")
    parts.append(SCOPE_CLASSIFY_FORMATTING_INSTRUCTIONS)
    return "\n".join(parts)


def _batches(issues: Sequence[CodeReviewIssue], max_findings_per_group: int) -> List[List[int]]:
    """Group finding indices by cited file, then chunk each group by the cap.

    Preconditions: ``max_findings_per_group >= 1``.
    Postconditions:
        - Returns a list of index batches. Every index in ``[0, len(issues))``
          appears in exactly one batch; each batch is non-empty and holds at
          most ``max_findings_per_group`` indices, all citing the same file
          (blank ``file_path`` groups under ``"(unknown)"``). File groups keep
          first-seen order; indices keep ascending order within a file. Pure;
          never raises.
    """
    assert max_findings_per_group >= 1, "max_findings_per_group must be >= 1"
    by_file: Dict[str, List[int]] = {}
    for idx, issue in enumerate(issues):
        path = (getattr(issue, "file_path", "") or "").strip() or "(unknown)"
        by_file.setdefault(path, []).append(idx)
    batches: List[List[int]] = []
    for indices in by_file.values():
        for start in range(0, len(indices), max_findings_per_group):
            batches.append(indices[start : start + max_findings_per_group])
    return batches


def classify_scope(
    issues: Sequence[CodeReviewIssue],
    *,
    llm: Optional[LLMClient] = None,
    input_data: Optional[CodeReviewInput] = None,
    max_findings_per_group: Optional[int] = None,
    max_workers: Optional[int] = None,
) -> List[ScopeClassification]:
    """Classify each finding in/out-of-scope, batched by file and fanned out.

    ``llm`` is supplied by the caller — the verification pipeline resolves the
    ``code_review_verify`` model and passes it in, mirroring
    ``false_positive_filter``/``scope_filter``. This pass does not self-resolve a
    client; ``llm=None`` degrades to all-:data:`UNKNOWN` rather than reaching for
    a heavier ``code_review`` client of its own.

    ``input_data`` (optional) supplies the PR task text and cited-file content
    inlined into each batch's prompt; ``None`` omits that context.

    ``max_findings_per_group`` overrides the per-file batch cap
    (``CODE_REVIEW_SCOPE_MAX_FINDINGS_PER_GROUP``); ``None`` uses the
    environment-configured default (:func:`_max_findings_per_group`). A value
    below 1 is floored to 1 (see the flooring postcondition below).

    ``max_workers`` overrides the parallel fan-out width; ``None`` uses the
    shared ``CODE_REVIEW_MAP_PARALLELISM`` budget via :func:`_scope_parallelism`
    (itself bounded by the number of batches). A value below 1 is floored to 1.

    Preconditions:
        - ``issues`` is a sequence of ``CodeReviewIssue``-like findings (each
          exposes ``file_path``/``line``/``description``/``suggestion``).

    Postconditions:
        - Returns a list positionally aligned 1:1 with ``issues`` — element ``i``
          is the verdict for ``issues[i]``. Empty ``issues`` returns ``[]``.
        - Every element is a :class:`ScopeClassification`. A finding whose batch
          failed, whose index the model omitted, or which ran with no client
          (``llm=None``) or under the production dummy is :data:`UNKNOWN`
          (``in_scope=None``), so a caller can fall back to its heuristic.
        - A ``max_findings_per_group`` or ``max_workers`` argument below 1 is
          floored to 1 (matching the env path's ``parse_env_int(..., 1)`` clamp),
          so an out-of-range tuning value is clamped, never raised.
        - **Never raises**: client resolution, LLM, and parse failures all
          degrade to ``UNKNOWN`` rather than propagating; out-of-range tuning
          arguments are floored. The guarantee is unconditional.

    Side effects:
        - One ``complete_json`` LLM call per batch (bounded by the
          map-parallelism budget), unless short-circuited above.
    """
    n = len(issues)
    if n == 0:
        return []

    # The caller owns model resolution (the code_review_verify model, like the
    # sibling verification passes); a missing client degrades to all-unknown
    # rather than self-resolving a heavier code_review client here.
    if llm is None or is_unscripted_dummy(llm):
        return [UNKNOWN] * n

    cap = (
        max_findings_per_group if max_findings_per_group is not None else _max_findings_per_group()
    )
    # Floor a caller-supplied cap the same way the env path clamps
    # (parse_env_int(..., 1)), so classify_scope's "never raises" guarantee holds
    # unconditionally rather than tripping _batches' assert on a value < 1.
    cap = max(1, cap)
    batches = _batches(issues, cap)

    def _classify_one_batch(batch: List[int]) -> Dict[int, ScopeClassification]:
        """Classify one batch; degrade the whole batch to unknown on any failure.

        Postconditions: returns ``{orig_index: ScopeClassification}`` for parsed
            verdicts. Never raises — any exception yields ``{}`` (all findings in
            the batch stay :data:`UNKNOWN`).
        """
        file_path = (getattr(issues[batch[0]], "file_path", "") or "").strip() or "(unknown)"
        batch_issues = [issues[i] for i in batch]
        try:
            prompt = _build_classify_prompt(file_path, batch_issues, input_data)
            data = llm.complete_json(
                prompt,
                objective="classify code-review finding scope",
                system_prompt=SCOPE_CLASSIFY_SYSTEM_PROMPT,
                temperature=0.0,
            )
            parsed = _parse_classifications(data, len(batch_issues))
            return {batch[local]: verdict for local, verdict in parsed.items()}
        except Exception as exc:  # noqa: BLE001 — per-batch fail-safe, never raises
            logger.warning(
                "ScopeClassifier: classification failed for %s (%s: %s); batch stays unknown",
                file_path,
                type(exc).__name__,
                exc,
            )
            return {}

    # Floor at 1 on both paths so a caller-supplied max_workers < 1 is clamped
    # rather than raising out of parallel_map (which requires max_workers >= 1).
    workers = max(
        1, max_workers if max_workers is not None else min(_scope_parallelism(), len(batches))
    )
    batch_results = parallel_map(
        batches,
        _classify_one_batch,
        max_workers=workers,
        skip_none=False,
    )

    verdicts: Dict[int, ScopeClassification] = {}
    for result in batch_results:
        if result:
            verdicts.update(result)
    return [verdicts.get(i, UNKNOWN) for i in range(n)]
