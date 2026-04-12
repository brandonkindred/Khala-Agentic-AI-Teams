"""Documentation agent: maintains README.md and project documentation."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from llm_service import get_strands_model
from software_engineering_team.shared.git_utils import (
    DEVELOPMENT_BRANCH,
    checkout_branch,
    create_feature_branch,
    delete_branch,
    merge_branch,
    write_files_and_commit,
)
from software_engineering_team.shared.repo_utils import DOCUMENTATION_EXTENSIONS, read_repo_code

from .models import DocumentationInput, DocumentationOutput, DocumentationStatus
from .prompts import (
    DOCUMENTATION_CONTRIBUTORS_FINAL_REVIEW_SUFFIX,
    DOCUMENTATION_CONTRIBUTORS_PROMPT,
    DOCUMENTATION_FINAL_REVIEW_SUFFIX,
    DOCUMENTATION_README_PROMPT,
)

logger = logging.getLogger(__name__)

# Maximum time (seconds) for the full documentation workflow before forced cleanup
MAX_WORKFLOW_SECONDS = 300

# Maximum characters of codebase content to send to the LLM
MAX_CODEBASE_CHARS = 40000


def _read_repo_code(repo_path: Path, extensions: List[str] | None = None) -> str:
    """Read code files from repo, concatenated. Delegates to shared.repo_utils."""
    if extensions is None:
        extensions = DOCUMENTATION_EXTENSIONS
    return read_repo_code(repo_path, extensions)


class DocumentationAgent:
    """
    Documentation agent that maintains README.md and CONTRIBUTORS.md for the project.
    Triggered by the Tech Lead after each task completes.

    Preconditions:
        - llm_client must be a valid, non-None LLMClient instance

    Postconditions:
        - Agent is ready to generate documentation via run() or run_full_workflow()

    Invariants:
        - Documentation updates never block the main pipeline (fail-safe)
        - Git branches are always cleaned up, even on failure
    """

    def __init__(self, llm_client=None) -> None:
        """
        Initialize the Documentation agent.

        Postconditions:
            - self._model is set to a Strands model
        """
        if llm_client is not None:
            self._model = llm_client
        else:
            self._model = get_strands_model("documentation")

    def run(self, input_data: DocumentationInput) -> DocumentationOutput:
        """
        Generate updated documentation based on the current codebase state.
        This method only generates content -- it does NOT perform git operations.

        Preconditions:
            - input_data contains valid repo_path and task_id
            - input_data.codebase_content is a non-empty string

        Postconditions:
            - Returns DocumentationOutput with updated readme and/or contributors content
            - readme_changed and contributors_changed flags accurately reflect whether updates occurred

        Raises:
            Exception: If LLM calls fail (handled internally, returns partial output)
        """
        logger.info(
            "Documentation: generating docs for task %s (agent=%s, is_final_review=%s)",
            input_data.task_id,
            input_data.agent_type,
            getattr(input_data, "is_final_review", False),
        )

        # Truncate codebase if too large
        codebase = input_data.codebase_content or ""
        if len(codebase) > MAX_CODEBASE_CHARS:
            codebase = codebase[:MAX_CODEBASE_CHARS] + (
                f"\n\n... [truncated, {len(codebase) - MAX_CODEBASE_CHARS} more chars]"
            )

        # --- Step 1: Update README.md (root + frontend/backend/devops) ---
        readme_content = ""
        readme_changed = False
        readme_frontend_content = ""
        readme_frontend_changed = False
        readme_backend_content = ""
        readme_backend_changed = False
        readme_devops_content = ""
        readme_devops_changed = False
        readme_summary = ""
        commit_message = "docs(readme): update project documentation"

        try:
            readme_context = [
                f"**Task just completed:** {input_data.task_id} ({input_data.agent_type})",
                f"**Task summary:** {input_data.task_summary}",
            ]

            if input_data.existing_readme:
                readme_context.extend(
                    [
                        "",
                        "**Current root README.md:**",
                        "---",
                        input_data.existing_readme,
                        "---",
                    ]
                )
            else:
                readme_context.append("\n**Current root README.md:** (none -- create from scratch)")

            if input_data.has_frontend_folder:
                if input_data.existing_readme_frontend:
                    readme_context.extend(
                        [
                            "",
                            "**Current frontend/README.md:**",
                            "---",
                            input_data.existing_readme_frontend,
                            "---",
                        ]
                    )
                else:
                    readme_context.append(
                        "\n**Current frontend/README.md:** (none -- create if appropriate)"
                    )
            if input_data.has_backend_folder:
                if input_data.existing_readme_backend:
                    readme_context.extend(
                        [
                            "",
                            "**Current backend/README.md:**",
                            "---",
                            input_data.existing_readme_backend,
                            "---",
                        ]
                    )
                else:
                    readme_context.append(
                        "\n**Current backend/README.md:** (none -- create if appropriate)"
                    )
            if input_data.has_devops_folder:
                if input_data.existing_readme_devops:
                    readme_context.extend(
                        [
                            "",
                            "**Current devops/README.md:**",
                            "---",
                            input_data.existing_readme_devops,
                            "---",
                        ]
                    )
                else:
                    readme_context.append(
                        "\n**Current devops/README.md:** (none -- create if appropriate)"
                    )
            readme_context.append(
                f"\n**Folders present in repo:** frontend={input_data.has_frontend_folder}, "
                f"backend={input_data.has_backend_folder}, devops={input_data.has_devops_folder}"
            )

            if input_data.spec_content:
                readme_context.extend(
                    [
                        "",
                        "**Project specification:**",
                        "---",
                        input_data.spec_content,
                        "---",
                    ]
                )

            if input_data.architecture:
                readme_context.extend(
                    [
                        "",
                        "**Architecture overview:**",
                        input_data.architecture.overview,
                    ]
                )

            readme_context.extend(
                [
                    "",
                    "**Full codebase:**",
                    "---",
                    codebase,
                    "---",
                ]
            )

            readme_prompt = DOCUMENTATION_README_PROMPT
            if getattr(input_data, "is_final_review", False):
                readme_prompt = readme_prompt + DOCUMENTATION_FINAL_REVIEW_SUFFIX
            prompt = "\n".join(readme_context)
            agent = Agent(model=self._model, system_prompt=readme_prompt)
            result = agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)

            readme_content = data.get("readme_content", "")
            readme_changed = bool(data.get("readme_changed", False))
            readme_frontend_content = data.get("frontend_readme", "")
            readme_frontend_changed = bool(data.get("frontend_readme_changed", False))
            readme_backend_content = data.get("backend_readme", "")
            readme_backend_changed = bool(data.get("backend_readme_changed", False))
            readme_devops_content = data.get("devops_readme", "")
            readme_devops_changed = bool(data.get("devops_readme_changed", False))
            readme_summary = data.get("summary", "")
            commit_message = data.get(
                "suggested_commit_message",
                "docs(readme): update project documentation",
            )

            # Safety: if content was returned but flag says unchanged, check for real diff
            if readme_content and not readme_changed:
                if readme_content.strip() != (input_data.existing_readme or "").strip():
                    readme_changed = True

            # Safety: if no README existed and content was generated, force creation
            if readme_content and not input_data.existing_readme:
                if not readme_changed:
                    logger.info("Documentation: README.md did not exist, forcing creation")
                readme_changed = True

            if readme_changed and not readme_content and not input_data.existing_readme:
                logger.warning(
                    "Documentation: LLM returned readme_changed=true but empty readme_content; "
                    "README will not be written"
                )

        except Exception as e:
            logger.warning("Documentation: README generation failed: %s", e)
            readme_summary = f"README update skipped due to error: {e}"

        # --- Step 2: Check CONTRIBUTORS.md ---
        contributors_content = ""
        contributors_changed = False
        contributors_summary = ""

        try:
            contrib_context = [
                f"**Task just completed:** {input_data.task_id}",
                f"**Agent type:** {input_data.agent_type}",
                f"**Task summary:** {input_data.task_summary}",
            ]
            if getattr(input_data, "completed_task_ids", None):
                contrib_context.append(
                    f"**All completed tasks (for CONTRIBUTORS):** {', '.join(input_data.completed_task_ids)}"
                )

            if input_data.existing_contributors:
                contrib_context.extend(
                    [
                        "",
                        "**Current CONTRIBUTORS.md:**",
                        "---",
                        input_data.existing_contributors,
                        "---",
                    ]
                )
            else:
                contrib_context.append(
                    "\n**Current CONTRIBUTORS.md:** (none -- create if appropriate)"
                )

            if input_data.spec_content:
                contrib_context.extend(
                    [
                        "",
                        "**Project specification:**",
                        input_data.spec_content,
                    ]
                )

            contrib_prompt = DOCUMENTATION_CONTRIBUTORS_PROMPT
            if getattr(input_data, "is_final_review", False):
                contrib_prompt = contrib_prompt + DOCUMENTATION_CONTRIBUTORS_FINAL_REVIEW_SUFFIX
            prompt = "\n".join(contrib_context)
            agent = Agent(model=self._model, system_prompt=contrib_prompt)
            result = agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)

            contributors_content = data.get("contributors_content", "")
            contributors_changed = bool(data.get("contributors_changed", False))
            contributors_summary = data.get("summary", "")

        except Exception as e:
            logger.warning("Documentation: CONTRIBUTORS check failed: %s", e)
            contributors_summary = f"CONTRIBUTORS check skipped due to error: {e}"

        # Build combined summary
        summaries = []
        if readme_summary:
            summaries.append(f"README: {readme_summary}")
        if contributors_summary:
            summaries.append(f"CONTRIBUTORS: {contributors_summary}")
        combined_summary = "; ".join(summaries) if summaries else "No documentation changes."

        logger.info(
            "Documentation: done, readme_changed=%s, readme_frontend_changed=%s, "
            "readme_backend_changed=%s, readme_devops_changed=%s, contributors_changed=%s",
            readme_changed,
            readme_frontend_changed,
            readme_backend_changed,
            readme_devops_changed,
            contributors_changed,
        )

        return DocumentationOutput(
            readme_content=readme_content,
            readme_frontend_content=readme_frontend_content,
            readme_backend_content=readme_backend_content,
            readme_devops_content=readme_devops_content,
            contributors_content=contributors_content,
            readme_changed=readme_changed,
            readme_frontend_changed=readme_frontend_changed,
            readme_backend_changed=readme_backend_changed,
            readme_devops_changed=readme_devops_changed,
            contributors_changed=contributors_changed,
            summary=combined_summary,
            suggested_commit_message=commit_message,
        )

    def run_full_workflow(
        self,
        repo_path: str | Path,
        task_id: str,
        task_summary: str,
        agent_type: str,
        spec_content: str,
        architecture: Any,
        codebase_content: str,
        on_status: Optional[Callable[[DocumentationStatus, str], None]] = None,
        is_final_review: bool = False,
        completed_task_ids: Optional[List[str]] = None,
    ) -> DocumentationOutput:
        """
        End-to-end documentation workflow: branch, generate, commit, merge, cleanup.

        Preconditions:
            - repo_path points to a valid git repository
            - task_id is a non-empty string
            - The repository is currently on the development branch

        Postconditions:
            - If successful: docs are updated and merged to development, branch is deleted
            - If failed: branch is cleaned up, development branch is checked out
            - Returns DocumentationOutput describing what was done

        Invariants:
            - The repository is always left on the development branch
            - No documentation failure blocks the main pipeline
        """
        path = Path(repo_path).resolve()
        branch_name = f"docs/{task_id}"  # preliminary; updated after branch creation
        workflow_start = time.monotonic()

        def _update(status: DocumentationStatus, detail: str = "") -> None:
            if on_status:
                on_status(status, detail)
            logger.info("DocAgent [%s]: %s %s", task_id, status.value, detail)

        def _elapsed() -> float:
            return time.monotonic() - workflow_start

        def _check_timeout() -> bool:
            if _elapsed() > MAX_WORKFLOW_SECONDS:
                logger.warning(
                    "DocAgent [%s]: workflow timeout (%.1fs > %ss), aborting",
                    task_id,
                    _elapsed(),
                    MAX_WORKFLOW_SECONDS,
                )
                return True
            return False

        _update(DocumentationStatus.STARTING)

        try:
            # Step 1: Create docs branch
            ok, msg = create_feature_branch(path, DEVELOPMENT_BRANCH, f"docs/{task_id}")
            if not ok:
                logger.warning("DocAgent [%s]: branch creation failed: %s", task_id, msg)
                _update(DocumentationStatus.FAILED, f"Branch creation failed: {msg}")
                return DocumentationOutput(
                    summary=f"Documentation update skipped: branch creation failed ({msg})",
                )
            # msg contains the actual branch name (e.g. "feature/docs/backend-validation")
            branch_name = msg

            if _check_timeout():
                _update(DocumentationStatus.FAILED, "timeout")
                self._cleanup_branch(path, branch_name)
                return DocumentationOutput(summary="Documentation update skipped: timeout")

            # Step 2: Read existing docs (root + frontend/backend/devops)
            _update(DocumentationStatus.REVIEWING_CODEBASE)
            existing_readme = self._read_file(path / "README.md")
            existing_contributors = self._read_file(path / "CONTRIBUTORS.md")
            frontend_dir = path / "frontend"
            backend_dir = path / "backend"
            devops_dir = path / "devops"
            existing_readme_frontend = (
                self._read_file(frontend_dir / "README.md") if frontend_dir.is_dir() else ""
            )
            existing_readme_backend = (
                self._read_file(backend_dir / "README.md") if backend_dir.is_dir() else ""
            )
            existing_readme_devops = (
                self._read_file(devops_dir / "README.md") if devops_dir.is_dir() else ""
            )
            readme_missing = not existing_readme

            # Step 3: Generate documentation
            _update(DocumentationStatus.UPDATING_README)
            input_data = DocumentationInput(
                repo_path=str(path),
                task_id=task_id,
                task_summary=task_summary,
                agent_type=agent_type,
                spec_content=spec_content,
                architecture=architecture,
                codebase_content=codebase_content,
                existing_readme=existing_readme,
                existing_readme_frontend=existing_readme_frontend,
                existing_readme_backend=existing_readme_backend,
                existing_readme_devops=existing_readme_devops,
                existing_contributors=existing_contributors,
                has_frontend_folder=frontend_dir.is_dir(),
                has_backend_folder=backend_dir.is_dir(),
                has_devops_folder=devops_dir.is_dir(),
                is_final_review=is_final_review,
                completed_task_ids=completed_task_ids,
            )

            result = self.run(input_data)

            # Force creation if README did not exist but content was generated
            if readme_missing and result.readme_content and not result.readme_changed:
                logger.info("DocAgent [%s]: README.md did not exist, forcing creation", task_id)
                result.readme_changed = True

            if _check_timeout():
                _update(DocumentationStatus.FAILED, "timeout")
                self._cleanup_branch(path, branch_name)
                return DocumentationOutput(summary="Documentation update skipped: timeout")

            # Step 4: Write and commit if changes were made
            files_to_write: Dict[str, str] = {}
            if result.readme_changed and result.readme_content:
                files_to_write["README.md"] = result.readme_content
            if (
                path.joinpath("frontend").is_dir()
                and result.readme_frontend_changed
                and result.readme_frontend_content
            ):
                files_to_write["frontend/README.md"] = result.readme_frontend_content
            if (
                path.joinpath("backend").is_dir()
                and result.readme_backend_changed
                and result.readme_backend_content
            ):
                files_to_write["backend/README.md"] = result.readme_backend_content
            if (
                path.joinpath("devops").is_dir()
                and result.readme_devops_changed
                and result.readme_devops_content
            ):
                files_to_write["devops/README.md"] = result.readme_devops_content
            if result.contributors_changed and result.contributors_content:
                files_to_write["CONTRIBUTORS.md"] = result.contributors_content

            if not files_to_write:
                logger.info(
                    "DocAgent [%s]: no documentation changes needed (readme_changed=%s, contributors_changed=%s, "
                    "readme_content_len=%d, contributors_content_len=%d)",
                    task_id,
                    result.readme_changed,
                    result.contributors_changed,
                    len(result.readme_content or ""),
                    len(result.contributors_content or ""),
                )
                _update(DocumentationStatus.COMPLETE, "no changes")
                self._cleanup_branch(path, branch_name)
                return result

            logger.info(
                "DocAgent [%s]: writing %d file(s): %s",
                task_id,
                len(files_to_write),
                list(files_to_write.keys()),
            )
            _update(DocumentationStatus.COMMITTING)
            ok, msg = write_files_and_commit(
                path,
                files_to_write,
                result.suggested_commit_message,
            )
            if not ok:
                logger.warning("DocAgent [%s]: commit failed: %s", task_id, msg)
                _update(DocumentationStatus.FAILED, f"Commit failed: {msg}")
                self._cleanup_branch(path, branch_name)
                return DocumentationOutput(
                    summary=f"Documentation commit failed: {msg}",
                )

            if _check_timeout():
                _update(DocumentationStatus.FAILED, "timeout")
                self._cleanup_branch(path, branch_name)
                return DocumentationOutput(summary="Documentation update skipped: timeout")

            # Step 5: Merge to development
            _update(DocumentationStatus.MERGING)
            merge_ok, merge_msg = merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
            if not merge_ok:
                logger.warning("DocAgent [%s]: merge failed: %s", task_id, merge_msg)
                _update(DocumentationStatus.FAILED, f"Merge failed: {merge_msg}")
                self._cleanup_branch(path, branch_name)
                return DocumentationOutput(
                    summary=f"Documentation merge failed: {merge_msg}",
                    readme_changed=result.readme_changed,
                    contributors_changed=result.contributors_changed,
                )

            # Step 6: Delete docs branch
            delete_branch(path, branch_name)
            checkout_branch(path, DEVELOPMENT_BRANCH)

            elapsed = _elapsed()
            _update(DocumentationStatus.COMPLETE, f"in {elapsed:.1f}s")
            logger.info(
                "DocAgent [%s]: documentation updated and merged in %.1fs (files: %s)",
                task_id,
                elapsed,
                list(files_to_write.keys()),
            )
            return result

        except Exception as e:
            logger.exception("DocAgent [%s]: workflow failed with exception", task_id)
            _update(DocumentationStatus.FAILED, str(e))
            self._cleanup_branch(path, branch_name)
            return DocumentationOutput(
                summary=f"Documentation update failed: {e}",
            )

    def run_final_review(
        self,
        repo_path: str | Path,
        repo_name: str,
        spec_content: str,
        architecture: Any,
        completed_task_ids: List[str],
        on_status: Optional[Callable[[DocumentationStatus, str], None]] = None,
    ) -> DocumentationOutput:
        """
        Run comprehensive documentation review after all tasks complete.
        Reads the codebase, generates full README and CONTRIBUTORS, and commits.
        """
        path = Path(repo_path).resolve()
        task_id = f"final-docs-{repo_name}"
        logger.info(
            "DocAgent [%s]: starting final comprehensive documentation review (%d tasks completed)",
            task_id,
            len(completed_task_ids),
        )
        try:
            extensions = [".py"] if repo_name == "backend" else [".ts", ".tsx", ".html", ".scss"]
            codebase_content = _read_repo_code(path, extensions)
        except Exception as e:
            logger.warning("DocAgent [%s]: failed to read codebase: %s", task_id, e)
            return DocumentationOutput(
                summary=f"Final review skipped: could not read codebase ({e})"
            )

        return self.run_full_workflow(
            repo_path=path,
            task_id=task_id,
            task_summary="Final comprehensive documentation review: all tasks complete. Document the full project.",
            agent_type=repo_name,
            spec_content=spec_content,
            architecture=architecture,
            codebase_content=codebase_content,
            on_status=on_status,
            is_final_review=True,
            completed_task_ids=completed_task_ids,
        )

    def _cleanup_branch(self, repo_path: Path, branch_name: str) -> None:
        """
        Clean up a documentation branch on failure.

        Preconditions:
            - repo_path is a valid git repository path

        Postconditions:
            - Repository is checked out to the development branch
            - The docs branch is deleted if it exists
        """
        try:
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            delete_branch(repo_path, branch_name)
        except Exception as cleanup_err:
            logger.warning(
                "DocAgent: branch cleanup failed for %s: %s",
                branch_name,
                cleanup_err,
            )

    @staticmethod
    def _read_file(file_path: Path) -> str:
        """
        Read a file and return its content, or empty string if not found.

        Preconditions:
            - file_path is a Path object

        Postconditions:
            - Returns the file content as a string, or empty string if file doesn't exist
        """
        try:
            if file_path.exists():
                return file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("DocAgent: failed to read %s: %s", file_path, e)
        return ""
