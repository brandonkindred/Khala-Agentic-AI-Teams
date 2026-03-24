"""Code Review Coordinator: splits large code into chunks and merges review results."""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

from llm_service import LLMClient, compact_text
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_chunk_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_spec_excerpt_chars,
)

from .chunk_reviewer import ChunkReviewAgent
from .models import ChunkReviewInput, CodeReviewInput, CodeReviewIssue, CodeReviewOutput

logger = logging.getLogger(__name__)

# Pattern: ### path/to/file ### at start of a block (content may contain \n\n)
_FILE_HEADER_PATTERN = re.compile(r"###\s+(.+?)\s+###\s*\n", re.DOTALL)


def parse_code_into_file_blocks(code: str) -> List[Tuple[str, str]]:
    """
    Parse concatenated code into (path, content) blocks using ### path ### pattern.
    Returns list of (file_path, content) tuples.
    """
    blocks: List[Tuple[str, str]] = []
    matches = list(_FILE_HEADER_PATTERN.finditer(code))
    if not matches:
        if code.strip():
            blocks.append(("", code.strip()))
        return blocks
    for i, m in enumerate(matches):
        path = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        content = code[start:end].rstrip()
        blocks.append((path, content))
    return blocks


def build_chunks(blocks: List[Tuple[str, str]], max_chars: int) -> List[Tuple[List[str], str]]:
    """
    Group file blocks into chunks so each chunk is ≤ max_chars.
    Returns list of (list_of_paths, combined_content).
    """
    chunks: List[Tuple[List[str], str]] = []
    current_paths: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    for path, content in blocks:
        block_text = f"### {path} ###\n{content}" if path else content
        block_len = len(block_text)
        if current_len + block_len > max_chars and current_parts:
            combined = "\n\n".join(current_parts)
            chunks.append((list(current_paths), combined))
            current_paths = []
            current_parts = []
            current_len = 0
        current_paths.append(path or "(unknown)")
        current_parts.append(block_text)
        current_len += block_len

    if current_parts:
        combined = "\n\n".join(current_parts)
        chunks.append((list(current_paths), combined))
    return chunks


def run_coordinator(llm: LLMClient, input_data: CodeReviewInput) -> CodeReviewOutput:
    """
    Split code into chunks, review each chunk, and merge results deterministically.
    """
    code = input_data.code or ""
    max_spec = compute_code_review_spec_excerpt_chars(llm)
    max_arch = compute_code_review_arch_overview_chars(llm)
    max_existing = compute_code_review_existing_codebase_chars(llm)
    spec_content = compact_text(input_data.spec_content or "", max_spec, llm, "specification")
    arch_overview = ""
    if input_data.architecture:
        arch_overview = compact_text(
            input_data.architecture.overview or "", max_arch, llm, "architecture overview"
        )
    existing_codebase = compact_text(
        input_data.existing_codebase or "", max_existing, llm, "existing codebase"
    )

    max_chars_per_chunk = compute_code_review_chunk_chars(llm)
    blocks = parse_code_into_file_blocks(code)
    chunks = build_chunks(blocks, max_chars=max_chars_per_chunk)

    logger.info(
        "CodeReviewCoordinator: %s blocks -> %s chunks",
        len(blocks),
        len(chunks),
    )

    chunk_reviewer = ChunkReviewAgent(llm)
    all_issues: List[CodeReviewIssue] = []
    all_approved = True
    summaries: List[str] = []

    for paths, chunk_content in chunks:
        paths_label = ", ".join(p for p in paths if p)
        chunk_input = ChunkReviewInput(
            code_chunk=chunk_content,
            file_path_or_label=paths_label,
            task_description=input_data.task_description or "",
            task_requirements=input_data.task_requirements or "",
            acceptance_criteria=input_data.acceptance_criteria or [],
            spec_excerpt=spec_content,
            architecture_overview=arch_overview,
            existing_codebase_excerpt=existing_codebase or None,
        )
        chunk_output = chunk_reviewer.run(chunk_input)
        all_approved = all_approved and chunk_output.approved
        summaries.append(chunk_output.summary)
        for i in chunk_output.issues:
            if isinstance(i, dict):
                all_issues.append(
                    CodeReviewIssue(
                        severity=i.get("severity", "major"),
                        category=i.get("category", "general"),
                        file_path=i.get("file_path", paths_label),
                        description=i.get("description", ""),
                        suggestion=i.get("suggestion", ""),
                    )
                )

    # Dedupe issues by (file_path, description)
    seen: set[Tuple[str, str]] = set()
    deduped: List[CodeReviewIssue] = []
    for issue in all_issues:
        key = (issue.file_path, issue.description)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)

    # approved = False if any critical/major
    critical_or_major = [i for i in deduped if i.severity in ("critical", "major")]
    approved = all_approved and len(critical_or_major) == 0

    # Safety net: same as main agent
    if not approved and not critical_or_major and deduped:
        logger.info("CodeReviewCoordinator: overriding to approved=True (only minor/nit issues)")
        approved = True

    merged_summary = "\n\n".join(s for s in summaries if s.strip())

    return CodeReviewOutput(
        approved=approved,
        issues=deduped,
        summary=merged_summary,
        spec_compliance_notes="",
        suggested_commit_message="",
    )
