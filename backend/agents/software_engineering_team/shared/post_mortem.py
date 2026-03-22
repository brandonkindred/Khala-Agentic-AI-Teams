"""
Post-mortem module for documenting failed recovery attempts.

When all recovery strategies (continuation and decomposition) fail,
this module writes a detailed post-mortem analysis to help diagnose
and fix the underlying issues.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

POST_MORTEM_DIR = "post_mortems"
POST_MORTEM_FILE = "POST_MORTEMS.md"


class PostMortemWriter:
    """Writes post-mortem analysis when all recovery strategies fail.

    Post-mortems are appended to a single file (POST_MORTEMS.md) in the
    post_mortems directory at the project root. Each entry includes:
    - Timestamp and agent name
    - Task description
    - What went wrong (continuation attempts, decomposition depth)
    - Partial response excerpts
    - Suggested fixes
    """

    def __init__(self, project_root: Optional[Path] = None):
        """Initialize the post-mortem writer.

        Args:
            project_root: Root directory for the post_mortems folder.
                         If None, uses current working directory.
        """
        if project_root is None:
            project_root = Path.cwd()
        self.project_root = Path(project_root)
        self.post_mortem_dir = self.project_root / POST_MORTEM_DIR
        self.post_mortem_file = self.post_mortem_dir / POST_MORTEM_FILE

    def write_failure(
        self,
        agent_name: str,
        task_description: str,
        original_prompt: str,
        partial_responses: List[str],
        continuation_attempts: int,
        decomposition_depth: int,
        error: Exception,
        *,
        max_continuation_cycles: int = 5,
        max_decomposition_depth: int = 20,
    ) -> Path:
        """Append a failure analysis to the post-mortem file.

        Args:
            agent_name: Name of the agent that failed.
            task_description: Brief description of what the agent was trying to do.
            original_prompt: The original prompt that led to failure (truncated).
            partial_responses: List of partial responses collected.
            continuation_attempts: Number of continuation cycles attempted.
            decomposition_depth: Maximum decomposition depth reached.
            error: The final error that caused failure.
            max_continuation_cycles: Maximum allowed continuation cycles.
            max_decomposition_depth: Maximum allowed decomposition depth.

        Returns:
            Path to the post-mortem file.
        """
        self.post_mortem_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = self._format_entry(
            timestamp=timestamp,
            agent_name=agent_name,
            task_description=task_description,
            original_prompt=original_prompt,
            partial_responses=partial_responses,
            continuation_attempts=continuation_attempts,
            decomposition_depth=decomposition_depth,
            error=error,
            max_continuation_cycles=max_continuation_cycles,
            max_decomposition_depth=max_decomposition_depth,
        )

        file_existed = self.post_mortem_file.exists()

        with open(self.post_mortem_file, "a", encoding="utf-8") as f:
            if not file_existed:
                f.write("# Agent Recovery Failures - Post-Mortem Log\n\n")
                f.write("This file documents failures where all recovery strategies ")
                f.write("(continuation and decomposition) were exhausted.\n\n")
                f.write("---\n\n")

            f.write(entry)

        logger.info(
            "Post-mortem written to %s for agent %s",
            self.post_mortem_file,
            agent_name,
        )

        return self.post_mortem_file

    def _format_entry(
        self,
        timestamp: str,
        agent_name: str,
        task_description: str,
        original_prompt: str,
        partial_responses: List[str],
        continuation_attempts: int,
        decomposition_depth: int,
        error: Exception,
        max_continuation_cycles: int,
        max_decomposition_depth: int,
    ) -> str:
        """Format a post-mortem entry.

        Args:
            All the same as write_failure.

        Returns:
            Formatted markdown string for the entry.
        """
        prompt_preview = self._truncate_text(original_prompt, max_len=500)
        error_str = str(error)[:500]

        partial_excerpts = self._format_partial_responses(partial_responses)
        suggestions = self._generate_suggestions(
            continuation_attempts=continuation_attempts,
            decomposition_depth=decomposition_depth,
            partial_responses=partial_responses,
            max_continuation_cycles=max_continuation_cycles,
            max_decomposition_depth=max_decomposition_depth,
        )

        entry = f"""## Failure: {timestamp} - {agent_name}

### Task Description

{task_description}

### What Went Wrong

- **Continuation attempts**: {continuation_attempts}/{max_continuation_cycles} cycles exhausted
- **Decomposition depth**: {decomposition_depth}/{max_decomposition_depth} levels reached
- **Final error**: `{error_str}`

### Original Prompt (truncated)

```
{prompt_preview}
```

### Partial Responses

{partial_excerpts}

### Suggested Fixes

{suggestions}

---

"""
        return entry

    def _truncate_text(self, text: str, max_len: int = 500) -> str:
        """Truncate text to max_len with ellipsis.

        Args:
            text: Text to truncate.
            max_len: Maximum length.

        Returns:
            Truncated text.
        """
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    def _format_partial_responses(
        self,
        partial_responses: List[str],
        max_per_response: int = 200,
        max_responses: int = 3,
    ) -> str:
        """Format partial responses for display.

        Args:
            partial_responses: List of partial responses.
            max_per_response: Max chars per response excerpt.
            max_responses: Max number of responses to show.

        Returns:
            Formatted markdown string.
        """
        if not partial_responses:
            return "*No partial responses captured*"

        lines = []
        total = len(partial_responses)
        shown = min(total, max_responses)

        for i in range(shown):
            response = partial_responses[i]
            excerpt = self._truncate_text(response, max_per_response)
            lines.append(f"**Response {i + 1}/{total}** ({len(response)} chars):\n```\n{excerpt}\n```\n")

        if total > shown:
            lines.append(f"*... and {total - shown} more responses not shown*\n")

        return "\n".join(lines)

    def _generate_suggestions(
        self,
        continuation_attempts: int,
        decomposition_depth: int,
        partial_responses: List[str],
        max_continuation_cycles: int,
        max_decomposition_depth: int,
    ) -> str:
        """Generate suggestions based on failure patterns.

        Args:
            continuation_attempts: Number of continuation cycles attempted.
            decomposition_depth: Decomposition depth reached.
            partial_responses: Partial responses collected.
            max_continuation_cycles: Max allowed continuation cycles.
            max_decomposition_depth: Max allowed decomposition depth.

        Returns:
            Markdown formatted suggestions.
        """
        suggestions = []

        if continuation_attempts >= max_continuation_cycles:
            suggestions.append(
                "- **Increase continuation cycles**: The response may need more than "
                f"{max_continuation_cycles} cycles. Consider increasing `MAX_CONTINUATION_CYCLES`."
            )

        if decomposition_depth >= max_decomposition_depth:
            suggestions.append(
                "- **Simplify the task**: The task could not be decomposed into small "
                "enough pieces. Consider breaking it into explicit subtasks before sending."
            )

        if partial_responses:
            total_chars = sum(len(r) for r in partial_responses)
            if total_chars > 50000:
                suggestions.append(
                    "- **Reduce output size**: The accumulated response is very large "
                    f"({total_chars} chars). Consider requesting more concise output."
                )

        suggestions.append(
            "- **Review token limits**: Check `SW_LLM_MAX_TOKENS` and model context size."
        )
        suggestions.append(
            "- **Reduce prompt complexity**: Simplify the original prompt or provide "
            "more focused instructions."
        )
        suggestions.append(
            "- **Check for infinite loops**: Ensure the LLM isn't generating repetitive "
            "content that never terminates naturally."
        )

        return "\n".join(suggestions)


def write_post_mortem(
    agent_name: str,
    task_description: str,
    original_prompt: str,
    partial_responses: List[str],
    continuation_attempts: int,
    decomposition_depth: int,
    error: Exception,
    *,
    project_root: Optional[Path] = None,
    max_continuation_cycles: int = 5,
    max_decomposition_depth: int = 20,
) -> Path:
    """Convenience function for writing a post-mortem.

    Args:
        agent_name: Name of the agent that failed.
        task_description: Brief description of the task.
        original_prompt: The original prompt (will be truncated).
        partial_responses: List of partial responses collected.
        continuation_attempts: Number of continuation cycles attempted.
        decomposition_depth: Decomposition depth reached.
        error: The final error.
        project_root: Root directory for post_mortems folder.
        max_continuation_cycles: Max allowed continuation cycles.
        max_decomposition_depth: Max allowed decomposition depth.

    Returns:
        Path to the post-mortem file.
    """
    writer = PostMortemWriter(project_root=project_root)
    return writer.write_failure(
        agent_name=agent_name,
        task_description=task_description,
        original_prompt=original_prompt,
        partial_responses=partial_responses,
        continuation_attempts=continuation_attempts,
        decomposition_depth=decomposition_depth,
        error=error,
        max_continuation_cycles=max_continuation_cycles,
        max_decomposition_depth=max_decomposition_depth,
    )
