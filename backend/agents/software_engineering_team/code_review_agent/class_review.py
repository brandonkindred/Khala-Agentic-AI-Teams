"""Class-level cohesion pass for the code-review engine.

The per-function map review reads code one function/method at a time. It can
say whether each method is correct, but it cannot see whether a class's methods
*together* serve the class's stated purpose. This pass closes that gap: for each
class in the submission it runs one bounded LLM review that evaluates the class's
purpose (name + docstring) against a body-free summary of its methods
(signatures + docstrings), flagging single-responsibility violations, misfit
methods, purpose/behavior mismatches, and missing responsibilities.

Design:
    - One LLM call per class (not per method): classes are extracted with
      ``code_units.extract_classes`` and reviewed through the shared
      ``ChunkReviewAgent`` under ``ReviewProfile.CLASS_COHESION``.
    - Advisory by default: cohesion findings are capped at ``medium`` severity so
      the pass surfaces design concerns without blocking a merge on a judgment
      call. A genuine correctness/security defect is still caught by the
      per-function review, which is not capped.
    - Bounded and opt-out: gated by ``CODE_REVIEW_CLASS_COHESION`` (default on)
      and capped at ``CODE_REVIEW_CLASS_COHESION_MAX_CLASSES`` classes, so the
      fan-out can never balloon on a large submission.
    - Fail-safe: a per-class failure yields no findings for that class (best
      effort); the pass never raises into the coordinator.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from llm_service import LLMClient
from shared_env import env_flag_enabled
from software_engineering_team.shared.context_sizing import (
    compute_code_review_class_cohesion_max_classes,
    parse_env_int,
)

from .chunk_reviewer import ChunkReviewAgent
from .chunking import _clean_str, _map_parallelism
from .code_units import ClassUnit, extract_classes
from .mapping import _review_model_fingerprint, _stable_json_digest
from .models import ChunkReviewInput, CodeReviewInput, CodeReviewIssue, coerce_line
from .profiles import ReviewProfile

logger = logging.getLogger(__name__)

# Default-on toggle mirroring the false-positive filter's env flag: an explicit
# ``CODE_REVIEW_CLASS_COHESION=false``/``0``/``no`` disables the pass; any other
# value (or unset) leaves it enabled. See docs/ENV_VARS.md.
_COHESION_ENV = "CODE_REVIEW_CLASS_COHESION"

# Process-global outcome cache for the cohesion pass. The map phase caches its
# per-chunk outcomes, but cohesion runs after it and would otherwise re-issue one
# LLM call per class on every re-review (retries, the SE planning-cache
# short-circuit, etc.). This bounded LRU keyed on the class's exact rendered
# prompt + shared task/spec context + resolved model reuses a class's findings
# whenever nothing that affects them changed. Best-effort: a miss simply
# recomputes, and only successful outcomes are stored (a transient per-class
# failure is retried next time). ``0`` disables it.
DEFAULT_COHESION_CACHE_SIZE = 512  # CODE_REVIEW_COHESION_CACHE_SIZE, floor 0

_COHESION_CACHE: "OrderedDict[str, List[CodeReviewIssue]]" = OrderedDict()
_COHESION_CACHE_LOCK = threading.Lock()


def _cohesion_cache_size() -> int:
    """Resolve the cohesion cache capacity (``CODE_REVIEW_COHESION_CACHE_SIZE``).

    Postconditions:
        - Returns the parsed int, floored at 0; ``0`` disables the cache.
    """
    return parse_env_int("CODE_REVIEW_COHESION_CACHE_SIZE", DEFAULT_COHESION_CACHE_SIZE, 0)


def clear_cohesion_cache() -> None:
    """Drop every cached cohesion outcome (test/force-cold helper).

    Postconditions:
        - The process-global cohesion cache is empty.
    """
    with _COHESION_CACHE_LOCK:
        _COHESION_CACHE.clear()


def _cohesion_key(path: str, prompt: str, input_data: CodeReviewInput, model_fp: str) -> str:
    """Stable cache key for one class's cohesion review.

    Preconditions:
        - ``prompt`` is the class's rendered cohesion prompt; ``model_fp`` is the
          resolved review-model fingerprint; every value folded into the key is
          JSON-native (enforced by ``_stable_json_digest``).

    Postconditions:
        - Returns a digest over the exact rendered class prompt plus every input
          that changes the review (path, task/requirements/criteria/language/
          user-decisions, the fixed CLASS_COHESION profile, resolved model), so a
          hit reproduces the same findings and any relevant change is a miss.
    """
    return _stable_json_digest(
        {
            "prompt": prompt,
            "path": path,
            "task_description": input_data.task_description or "",
            "task_requirements": input_data.task_requirements or "",
            "acceptance_criteria": list(input_data.acceptance_criteria or []),
            "language": input_data.language or "",
            "user_decisions": list(input_data.user_decisions or []),
            "profile": ReviewProfile.CLASS_COHESION.value,
            "model": model_fp,
        }
    )


def _cohesion_cache_get(key: str) -> Optional[List[CodeReviewIssue]]:
    """Return a deep copy of the cached findings for ``key``, or None on a miss.

    Preconditions:
        - ``key`` is a ``_cohesion_key`` digest.

    Postconditions:
        - On a hit, returns a fresh deep copy of the cached findings (so the
          caller can mutate them without corrupting the cache) and marks the
          entry most-recently-used. Returns None on a miss. Thread-safe (holds
          the cache lock).
    """
    with _COHESION_CACHE_LOCK:
        cached = _COHESION_CACHE.get(key)
        if cached is None:
            return None
        _COHESION_CACHE.move_to_end(key)
        return [i.model_copy(deep=True) for i in cached]


def _cohesion_cache_put(key: str, issues: List[CodeReviewIssue], size: int) -> None:
    """Store a deep copy of ``issues`` under ``key``, evicting LRU past ``size``.

    Preconditions:
        - ``key`` is a ``_cohesion_key`` digest; ``issues`` is that class's
          normalized findings; ``size`` is the resolved cache capacity.

    Postconditions:
        - A no-op when ``size <= 0``. Otherwise stores a deep copy of ``issues``
          (so a later caller mutating the returned list cannot corrupt the cache,
          and the caller's own list stays independent), marks it
          most-recently-used, and evicts least-recently-used entries until the
          cache holds at most ``size``. Thread-safe (holds the cache lock).
    """
    if size <= 0:
        return
    with _COHESION_CACHE_LOCK:
        _COHESION_CACHE[key] = [i.model_copy(deep=True) for i in issues]
        _COHESION_CACHE.move_to_end(key)
        while len(_COHESION_CACHE) > size:
            _COHESION_CACHE.popitem(last=False)


# Cohesion findings are advisory: severities above this cap are lowered to it so
# a judgment-call design concern never blocks a merge. Ordered least→most severe.
_SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")
_MAX_COHESION_SEVERITY = "medium"

# Bounds on what is inlined per class, so one huge class cannot dominate a prompt.
_MAX_METHODS_INLINED = 60
_MAX_DOCSTRING_CHARS = 600
_MAX_METHOD_DOC_CHARS = 300


def _cap_severity(severity: str) -> str:
    """Lower an LLM-reported severity to the advisory cohesion ceiling.

    Postconditions:
        - Returns ``severity`` unchanged when it is at or below
          ``_MAX_COHESION_SEVERITY`` in ``_SEVERITY_ORDER``; otherwise returns
          ``_MAX_COHESION_SEVERITY``. An unrecognized value is treated as the
          ceiling (so a bad value never blocks the gate).
    """
    cap_idx = _SEVERITY_ORDER.index(_MAX_COHESION_SEVERITY)
    try:
        idx = _SEVERITY_ORDER.index(severity)
    except ValueError:
        return _MAX_COHESION_SEVERITY
    return severity if idx <= cap_idx else _MAX_COHESION_SEVERITY


def _first_paragraph(text: str, limit: int) -> str:
    """First paragraph of ``text`` (docstring), stripped and length-capped.

    Postconditions:
        - Returns the text up to the first blank line, stripped, truncated to
          ``limit`` chars; '' for blank input.
    """
    if not text or not text.strip():
        return ""
    para = text.strip().split("\n\n", 1)[0].strip()
    return para[:limit]


def _render_class_prompt(path: str, cu: ClassUnit) -> str:
    """Render the cohesion review body for one class.

    Postconditions:
        - Returns a bounded text block naming the class, its file/line range, its
          stated purpose (docstring or "(none provided)"), and a body-free list
          of its methods (signature + first docstring paragraph), method count
          truncated to ``_MAX_METHODS_INLINED``.
    """
    lines = [
        f"Class `{cu.name}` (from {path}, lines {cu.start_line}-{cu.end_line})",
        "",
        "Stated purpose (class docstring):",
        _first_paragraph(cu.docstring, _MAX_DOCSTRING_CHARS) or "(none provided)",
        "",
        f"Methods ({len(cu.methods)}):",
    ]
    for m in cu.methods[:_MAX_METHODS_INLINED]:
        entry = f"- {m.signature}"
        doc = _first_paragraph(m.docstring, _MAX_METHOD_DOC_CHARS)
        if doc:
            entry += f"\n    purpose: {doc}"
        lines.append(entry)
    if len(cu.methods) > _MAX_METHODS_INLINED:
        lines.append(f"- ... and {len(cu.methods) - _MAX_METHODS_INLINED} more method(s)")
    if not cu.methods:
        lines.append("- (this class defines no methods)")
    return "\n".join(lines)


def _collect_classes(
    blocks: List[Tuple[str, str]], max_classes: int
) -> List[Tuple[str, ClassUnit]]:
    """Extract up to ``max_classes`` (path, class) pairs across all blocks.

    Postconditions:
        - Returns pairs in block-then-source order, truncated to ``max_classes``
          (which is >= 0; ``0`` yields ``[]``). Non-Python and unparseable blocks
          contribute nothing (``extract_classes`` returns []).
    """
    collected: List[Tuple[str, ClassUnit]] = []
    for path, content in blocks:
        for cu in extract_classes(path, content):
            collected.append((path, cu))
            if len(collected) >= max_classes:
                return collected
    return collected


def _issues_from_class_output(
    path: str, cu: ClassUnit, raw_issues: List[dict]
) -> List[CodeReviewIssue]:
    """Normalize one class review's raw issue dicts into ``CodeReviewIssue``s.

    Postconditions:
        - Every issue is anchored to ``path``; ``line`` is the LLM-cited line when
          it falls inside the class's range, else the class's start line (so a
          cohesion finding always anchors somewhere in the class).
        - ``severity`` is capped to the advisory ceiling; ``category`` defaults to
          "structure". Malformed items and blank descriptions are dropped; never
          raises.
    """
    issues: List[CodeReviewIssue] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        description = _clean_str(item.get("description"), "")
        if not description:
            continue
        cited = coerce_line(item.get("line"))
        line = (
            cited
            if (cited is not None and cu.start_line <= cited <= cu.end_line)
            else cu.start_line
        )
        issues.append(
            CodeReviewIssue(
                severity=_cap_severity(_clean_str(item.get("severity"), "medium").lower()),
                category=_clean_str(item.get("category"), "structure"),
                file_path=path,
                line=line,
                description=description,
                suggestion=_clean_str(item.get("suggestion"), ""),
            )
        )
    return issues


def review_class_cohesion(
    llm: LLMClient,
    blocks: List[Tuple[str, str]],
    input_data: CodeReviewInput,
) -> List[CodeReviewIssue]:
    """Review each class in the submission for cohesion; return advisory findings.

    Preconditions:
        - ``blocks`` are the submission's ``(path, content)`` blocks (as built by
          the coordinator's ``_blocks_from_input``); ``input_data`` carries the
          task/spec context to give the reviewer.

    Postconditions:
        - Returns a flat list of ``CodeReviewIssue``s (advisory: severity capped
          at ``medium``), one class's findings after another in extraction order.
        - Returns ``[]`` when the pass is disabled (``CODE_REVIEW_CLASS_COHESION``
          false), when the class cap is ``0``, when no Python class is found, or
          when every per-class review fails. Never raises — a per-class failure
          contributes no findings for that class and is logged.
    """
    if not env_flag_enabled(_COHESION_ENV):
        return []
    max_classes = compute_code_review_class_cohesion_max_classes()
    if max_classes <= 0:
        return []
    classes = _collect_classes(blocks, max_classes)
    if not classes:
        return []

    # One ChunkReviewAgent instance is shared across the ThreadPoolExecutor
    # workers below. This is safe per its class docstring (chunk_reviewer.py):
    # it holds only the injected ``llm`` handle and builds a fresh strands
    # ``Agent`` on every ``run`` call, so concurrent ``run`` calls share no
    # mutable state — the same sharing pattern the coordinator's map phase uses.
    agent = ChunkReviewAgent(llm)
    cache_size = _cohesion_cache_size()
    model_fp = _review_model_fingerprint(llm)

    def _review_one(item: Tuple[str, ClassUnit]) -> List[CodeReviewIssue]:
        # The ENTIRE per-class path is best-effort: prompt rendering, key/cache
        # ops, the LLM call, and issue normalization all run under one guard so a
        # failure anywhere drops only this class's advisory findings and never
        # propagates out of review_class_cohesion (the coordinator relies on the
        # pass never raising).
        path, cu = item
        try:
            prompt = _render_class_prompt(path, cu)
            key = _cohesion_key(path, prompt, input_data, model_fp) if cache_size > 0 else None
            if key is not None:
                hit = _cohesion_cache_get(key)
                if hit is not None:
                    return hit
            chunk_input = ChunkReviewInput(
                code_chunk=prompt,
                file_path_or_label=path,
                language=input_data.language or "",
                task_description=input_data.task_description or "",
                task_requirements=input_data.task_requirements or "",
                acceptance_criteria=input_data.acceptance_criteria or [],
                # Same empty/falsy -> [] fallback the cache key uses (_cohesion_key
                # above), so both treat "no user decisions" identically.
                user_decisions=list(input_data.user_decisions or []),
                profile=ReviewProfile.CLASS_COHESION,
            )
            out = agent.run(chunk_input)
            issues = _issues_from_class_output(path, cu, out.issues)
            # Only successful outcomes are cached, so a transient failure retries.
            if key is not None:
                _cohesion_cache_put(key, issues, cache_size)
            return issues
        except Exception as exc:  # noqa: BLE001 - best-effort: a class failure drops only its findings
            logger.warning(
                "ClassCohesion: review failed for class %s in %s (%s: %s); skipping it",
                cu.name,
                path,
                type(exc).__name__,
                exc,
            )
            return []

    workers = min(_map_parallelism(), len(classes))
    if workers <= 1:
        per_class = [_review_one(item) for item in classes]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            per_class = list(executor.map(_review_one, classes))

    findings = [issue for group in per_class for issue in group]
    if findings:
        logger.info("ClassCohesion: %s finding(s) across %s class(es)", len(findings), len(classes))
    return findings
