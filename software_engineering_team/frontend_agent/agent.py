"""Frontend Expert agent: Angular implementation."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from shared.llm import LLMClient

from .models import FrontendInput, FrontendOutput
from .prompts import FRONTEND_PROMPT

logger = logging.getLogger(__name__)

# Validation constants
MAX_PATH_SEGMENT_LENGTH = 30
ANGULAR_PATH_PATTERN = re.compile(r"^src/")
BAD_NAME_PATTERN = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+")  # 4+ hyphenated words = likely sentence
VERB_PREFIX_PATTERN = re.compile(
    r"^(implement|create|build|setup|configure|add|make|define|develop|write|design|establish)-"
)
FILLER_WORD_PATTERN = re.compile(r"-(the|that|with|using|which|for|and|a|an)-")

# Well-known directory names that are always allowed
_ALLOWED_DIRS = frozenset({
    "src", "app", "lib", "components", "services", "models", "guards", "pipes",
    "shared", "pages", "features", "assets", "styles", "environments", "modules",
    "interceptors", "directives", "utils", "helpers", "test", "spec", "dist", "node_modules",
})


def _validate_file_paths(files: Dict[str, str]) -> tuple[Dict[str, str], list[str]]:
    """
    Validate and sanitize file paths from LLM output.

    Returns (validated_files, warnings).
    Rejects files with:
    - Path segments > MAX_PATH_SEGMENT_LENGTH
    - Names that look like sentences (4+ hyphenated words)
    - Names starting with verbs (implement-, create-, build-, etc.)
    - Names containing filler words (-the-, -with-, -using-, etc.)
    - Empty content
    """
    validated = {}
    warnings = []
    for path, content in files.items():
        segments = path.split("/")
        bad_segment = False
        for seg in segments:
            name_part = seg.split(".")[0]  # strip extension
            if not name_part:
                continue
            # Skip well-known directory names
            if name_part.lower() in _ALLOWED_DIRS:
                continue
            if len(name_part) > MAX_PATH_SEGMENT_LENGTH:
                warnings.append(f"Path segment too long (likely task description as name): '{seg}' in '{path}'")
                bad_segment = True
                break
            if BAD_NAME_PATTERN.match(name_part):
                warnings.append(f"Path segment looks like a sentence (4+ hyphenated words): '{seg}' in '{path}'")
                bad_segment = True
                break
            if VERB_PREFIX_PATTERN.match(name_part):
                warnings.append(f"Path segment starts with a verb (task description as name): '{seg}' in '{path}'")
                bad_segment = True
                break
            if FILLER_WORD_PATTERN.search(name_part):
                warnings.append(f"Path segment contains filler words (task description as name): '{seg}' in '{path}'")
                bad_segment = True
                break
        if bad_segment:
            continue

        # Ensure content is non-empty
        if not content or not content.strip():
            warnings.append(f"Empty file content for '{path}' - skipping")
            continue

        validated[path] = content

    return validated, warnings


class FrontendExpertAgent:
    """
    Frontend expert that implements solutions using Angular.
    Validates output to ensure proper naming conventions and project structure.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: FrontendInput) -> FrontendOutput:
        """Implement frontend functionality in Angular."""
        logger.info(
            "Frontend: received task - description=%s | requirements=%s | user_story=%s | "
            "has_architecture=%s | has_existing_code=%s | has_api_endpoints=%s | has_spec=%s | "
            "qa_issues=%s | security_issues=%s | code_review_issues=%s",
            input_data.task_description[:120],
            input_data.requirements[:120] if input_data.requirements else "",
            input_data.user_story[:80] if input_data.user_story else "",
            input_data.architecture is not None,
            bool(input_data.existing_code),
            bool(input_data.api_endpoints),
            bool(input_data.spec_content),
            len(input_data.qa_issues) if input_data.qa_issues else 0,
            len(input_data.security_issues) if input_data.security_issues else 0,
            len(input_data.code_review_issues) if input_data.code_review_issues else 0,
        )
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
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
                *[f"- {c.name} ({c.type})" for c in input_data.architecture.components if c.type == "frontend"],
            ])
        if input_data.existing_code:
            context_parts.extend(["", "**Existing code:**", input_data.existing_code])
        if input_data.api_endpoints:
            context_parts.extend(["", "**API endpoints:**", input_data.api_endpoints])
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

        prompt = FRONTEND_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        code = data.get("code", "")
        if code and "\\n" in code:
            code = code.replace("\\n", "\n")

        # Process files dict - unescape newlines in file contents
        raw_files = data.get("files", {})
        if raw_files and isinstance(raw_files, dict):
            for fpath, fcontent in list(raw_files.items()):
                if isinstance(fcontent, str) and "\\n" in fcontent:
                    raw_files[fpath] = fcontent.replace("\\n", "\n")

        # Validate file paths - reject bad names/empty files
        validated_files, validation_warnings = _validate_file_paths(raw_files)
        for warn in validation_warnings:
            logger.warning("Frontend output validation: %s", warn)

        # If all files were rejected but we have code, that's a problem - log it
        if not validated_files and not data.get("needs_clarification", False):
            if raw_files:
                logger.error(
                    "Frontend: ALL %d files were rejected by validation. Raw filenames: %s",
                    len(raw_files),
                    list(raw_files.keys()),
                )
            elif code:
                logger.warning("Frontend: returned 'code' but no 'files' dict. Code will be written as fallback.")
            else:
                logger.error("Frontend: produced no files and no code. Task may have failed.")

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "Frontend: done, code=%s chars, files=%s (validated from %s), summary=%s chars, needs_clarification=%s",
            len(code), len(validated_files), len(raw_files), len(summary), needs_clarification,
        )
        return FrontendOutput(
            code=code,
            summary=summary,
            files=validated_files,
            components=data.get("components", []),
            suggested_commit_message=data.get("suggested_commit_message", ""),
            needs_clarification=needs_clarification,
            clarification_requests=clarification_requests,
        )
