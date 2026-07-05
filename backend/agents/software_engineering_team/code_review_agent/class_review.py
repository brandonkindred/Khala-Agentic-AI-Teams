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
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

from llm_service import LLMClient
from shared_env import env_flag_enabled
from software_engineering_team.shared.context_sizing import (
    compute_code_review_class_cohesion_max_classes,
)

from .chunk_reviewer import ChunkReviewAgent
from .chunking import _clean_str, _map_parallelism
from .code_units import ClassUnit, extract_classes
from .models import ChunkReviewInput, CodeReviewInput, CodeReviewIssue, coerce_line
from .profiles import ReviewProfile

logger = logging.getLogger(__name__)

# Default-on toggle mirroring the false-positive filter's env flag: an explicit
# ``CODE_REVIEW_CLASS_COHESION=false``/``0``/``no`` disables the pass; any other
# value (or unset) leaves it enabled. See docs/ENV_VARS.md.
_COHESION_ENV = "CODE_REVIEW_CLASS_COHESION"

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

    agent = ChunkReviewAgent(llm)

    def _review_one(item: Tuple[str, ClassUnit]) -> List[CodeReviewIssue]:
        path, cu = item
        try:
            chunk_input = ChunkReviewInput(
                code_chunk=_render_class_prompt(path, cu),
                file_path_or_label=path,
                language=input_data.language or "",
                task_description=input_data.task_description or "",
                task_requirements=input_data.task_requirements or "",
                acceptance_criteria=input_data.acceptance_criteria or [],
                user_decisions=input_data.user_decisions or None,
                profile=ReviewProfile.CLASS_COHESION,
            )
            out = agent.run(chunk_input)
            return _issues_from_class_output(path, cu, out.issues)
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
