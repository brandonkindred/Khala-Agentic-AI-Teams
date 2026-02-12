"""Backend Expert agent: Python/Java implementation."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from shared.llm import LLMClient

from .models import BackendInput, BackendOutput
from .prompts import BACKEND_PROMPT

logger = logging.getLogger(__name__)

# Validation constants
MAX_PATH_SEGMENT_LENGTH = 40
BAD_NAME_PATTERN = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+-[a-z]+-[a-z]+")  # 6+ hyphenated words = likely sentence


def _validate_file_paths(files: Dict[str, str]) -> tuple[Dict[str, str], list[str]]:
    """
    Validate and sanitize file paths from LLM output.

    Returns (validated_files, warnings).
    Rejects files with overly long path segments or names that look like task descriptions.
    """
    validated = {}
    warnings = []
    for path, content in files.items():
        segments = path.split("/")
        bad_segment = False
        for seg in segments:
            name_part = seg.split(".")[0]
            if len(name_part) > MAX_PATH_SEGMENT_LENGTH:
                warnings.append(f"Path segment too long: '{seg}' in '{path}'")
                bad_segment = True
                break
            if BAD_NAME_PATTERN.match(name_part):
                warnings.append(f"Path segment looks like a sentence: '{seg}' in '{path}'")
                bad_segment = True
                break
        if bad_segment:
            continue
        if not content or not content.strip():
            warnings.append(f"Empty file content for '{path}' - skipping")
            continue
        validated[path] = content
    return validated, warnings


class BackendExpertAgent:
    """
    Backend expert that implements solutions in Python or Java.
    Validates output to ensure proper file structure and non-empty deliverables.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: BackendInput) -> BackendOutput:
        """Implement backend functionality."""
        logger.info(
            "Backend: received task - description=%s | requirements=%s | user_story=%s | language=%s | "
            "has_architecture=%s | has_existing_code=%s | has_api_spec=%s | has_spec=%s | "
            "qa_issues=%s | security_issues=%s | code_review_issues=%s",
            input_data.task_description[:120],
            input_data.requirements[:120] if input_data.requirements else "",
            input_data.user_story[:80] if input_data.user_story else "",
            input_data.language,
            input_data.architecture is not None,
            bool(input_data.existing_code),
            bool(input_data.api_spec),
            bool(input_data.spec_content),
            len(input_data.qa_issues) if input_data.qa_issues else 0,
            len(input_data.security_issues) if input_data.security_issues else 0,
            len(input_data.code_review_issues) if input_data.code_review_issues else 0,
        )
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
            f"**Language:** {input_data.language}",
        ]
        if input_data.user_story:
            context_parts.extend(["", f"**User Story:** {input_data.user_story}"])
        if input_data.spec_content:
            context_parts.extend([
                "",
                "**Project Specification (full spec for the application being built):**",
                "---",
                input_data.spec_content,
                "---",
            ])
        if input_data.architecture:
            context_parts.extend([
                "",
                "**Architecture:**",
                input_data.architecture.overview,
                *[f"- {c.name} ({c.type}): {c.technology}" for c in input_data.architecture.components if c.technology],
            ])
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_spec:
            context_parts.extend(["", "**API spec:**", input_data.api_spec])
        if input_data.qa_issues:
            qa_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                for i in input_data.qa_issues
            )
            context_parts.extend(["", "**QA issues to fix (implement these):**", qa_text])
        if input_data.security_issues:
            sec_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('category')}: {i.get('description')} (location: {i.get('location')})\n  Recommendation: {i.get('recommendation')}"
                for i in input_data.security_issues
            )
            context_parts.extend(["", "**Security issues to fix (implement these):**", sec_text])
        if input_data.code_review_issues:
            cr_text = "\n".join(
                f"- [{i.get('severity')}] {i.get('category', 'general')}: {i.get('description')} "
                f"(file: {i.get('file_path', 'unknown')})\n  Suggestion: {i.get('suggestion', '')}"
                for i in input_data.code_review_issues
            )
            context_parts.extend(["", "**Code review issues to resolve:**", cr_text])

        prompt = BACKEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        code = data.get("code", "")
        if code and "\\n" in code:
            code = code.replace("\\n", "\n")
        tests = data.get("tests", "")
        if tests and "\\n" in tests:
            tests = tests.replace("\\n", "\n")

        # Process files dict - unescape newlines in file contents
        raw_files = data.get("files", {})
        if raw_files and isinstance(raw_files, dict):
            for fpath, fcontent in list(raw_files.items()):
                if isinstance(fcontent, str) and "\\n" in fcontent:
                    raw_files[fpath] = fcontent.replace("\\n", "\n")

        # Validate file paths
        validated_files, validation_warnings = _validate_file_paths(raw_files)
        for warn in validation_warnings:
            logger.warning("Backend output validation: %s", warn)

        # If all files were rejected but we have code, that's a problem
        if not validated_files and not data.get("needs_clarification", False):
            if raw_files:
                logger.error(
                    "Backend: ALL %d files were rejected by validation. Raw filenames: %s",
                    len(raw_files),
                    list(raw_files.keys()),
                )
            elif code:
                logger.warning("Backend: returned 'code' but no 'files' dict. Code will be written as fallback.")
            else:
                logger.error("Backend: produced no files and no code. Task may have failed.")

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "Backend: done, code=%s chars, files=%s (validated from %s), tests=%s chars, "
            "summary=%s chars, needs_clarification=%s",
            len(code), len(validated_files), len(raw_files), len(tests),
            len(summary), needs_clarification,
        )
        return BackendOutput(
            code=code,
            language=data.get("language", input_data.language),
            summary=summary,
            files=validated_files,
            tests=tests,
            suggested_commit_message=data.get("suggested_commit_message", ""),
            needs_clarification=needs_clarification,
            clarification_requests=clarification_requests,
        )
