"""CI/CD tool agent for frontend-code-v2: generates CI/CD artifacts in plan and deliver phases."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Dict, Optional

from ...models import ToolAgentInput, ToolAgentOutput, ToolAgentPhaseInput, ToolAgentPhaseOutput

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

MAX_SPEC_CHARS = 6_000

BUILD_RELEASE_PROMPT = """You are a Build and Release (Frontend DevOps) Agent. Your job is to ensure the frontend can be shipped safely. If you cannot ship safely, you are not "done," you are "nearly done forever."

**Your expertise:**
- CI checks: lint, typecheck, tests, bundle analysis, vuln scan
- Preview environments (per PR)
- Release and rollback plan
- Source maps, error reporting integration, artifact retention
- GitHub Actions, GitLab CI, or similar for frontend projects

**Input:**
- Task description
- Optional: spec, architecture, existing pipeline config, repo summary

**Your task:**
Produce build and release artifacts for the frontend repo:

1. **CI Plan** – What checks run on each PR: ESLint, typecheck (build), unit tests, e2e (Cypress/Playwright if applicable), bundle size analysis, dependency vulnerability scan (npm audit). Order and failure behavior.
2. **Preview Environment Plan** – How to get a preview URL per PR (e.g. Vercel, Netlify, GitHub Pages, Docker + cloud). What gets deployed.
3. **Release and Rollback Plan** – How releases are cut (tag, branch strategy). How to rollback if a release fails. Versioning strategy.
4. **Source Maps and Error Reporting** – Source maps for production (obfuscated but debuggable). Integration with error reporting (Sentry, LogRocket, etc.). Artifact retention (how long to keep build artifacts).
5. **Pipeline YAML** – If applicable, produce or update CI pipeline configuration (e.g. .github/workflows/frontend.yml). Include: install, lint, build, test, and optionally deploy to preview.

**Output format:**
Return a single JSON object with:
- "ci_plan": string (CI checks and order)
- "preview_env_plan": string (preview per PR)
- "release_rollback_plan": string (release and rollback)
- "source_maps_error_reporting": string (source maps, error reporting, retention)
- "pipeline_yaml": string (optional YAML for CI; empty if not producing)
- "summary": string (2-3 sentence summary)

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""


class CicdAdapterAgent:
    """CI/CD tool agent: generates CI/CD artifacts in plan and deliver phases."""

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        logger.info("CI/CD: microtask %s (execute stub)", inp.microtask.id)
        return ToolAgentOutput(summary="CI/CD execute — no changes applied.")

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate CI/CD artifacts: CI plan, preview env, release/rollback, error reporting, pipeline YAML."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                recommendations=[
                    "Configure CI pipeline with lint, typecheck, test, and build steps.",
                    "Set up preview environments for PRs (Vercel, Netlify, or similar).",
                    "Define release strategy with semantic versioning and rollback plan.",
                    "Enable source maps and integrate error reporting (Sentry).",
                ],
                summary="CI/CD planning (no LLM).",
            )
        spec_excerpt = (inp.spec_context or "")[:MAX_SPEC_CHARS]
        task_desc = inp.task_description or inp.task_title or "Frontend CI/CD setup"
        prompt = BUILD_RELEASE_PROMPT.format(
            task_description=task_desc,
            spec_content=spec_excerpt if spec_excerpt.strip() else "(no spec provided)",
        )
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("CI/CD plan LLM call failed: %s", e)
            return ToolAgentPhaseOutput(
                recommendations=["CI/CD planning failed (LLM error)."],
                summary="CI/CD planning failed.",
            )
        data = self._parse_json(raw)
        recommendations = []
        if data.get("ci_plan"):
            recommendations.append(f"CI Plan: {data['ci_plan'][:500]}")
        if data.get("preview_env_plan"):
            recommendations.append(f"Preview Env: {data['preview_env_plan'][:500]}")
        if data.get("release_rollback_plan"):
            recommendations.append(f"Release/Rollback: {data['release_rollback_plan'][:500]}")
        if data.get("source_maps_error_reporting"):
            recommendations.append(f"Error Reporting: {data['source_maps_error_reporting'][:500]}")
        summary = data.get("summary", "CI/CD artifacts generated.")
        return ToolAgentPhaseOutput(
            recommendations=recommendations if recommendations else ["CI/CD artifacts generated."],
            summary=summary[:500] if summary else "CI/CD planning complete.",
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="CI/CD review (no issues to report).")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(summary="CI/CD problem-solving (no fixes needed).")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate pipeline YAML files if needed during deliver phase."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="CI/CD deliver (no LLM).")
        spec_excerpt = (inp.spec_context or "")[:MAX_SPEC_CHARS]
        task_desc = inp.task_description or inp.task_title or "Frontend CI/CD setup"
        prompt = BUILD_RELEASE_PROMPT.format(
            task_description=task_desc,
            spec_content=spec_excerpt if spec_excerpt.strip() else "(no spec provided)",
        )
        try:
            raw = self.llm.complete_text(prompt)
        except Exception as e:
            logger.warning("CI/CD deliver LLM call failed: %s", e)
            return ToolAgentPhaseOutput(summary="CI/CD deliver failed (LLM error).")
        data = self._parse_json(raw)
        files: Dict[str, str] = {}
        pipeline_yaml = data.get("pipeline_yaml", "")
        if pipeline_yaml and pipeline_yaml.strip():
            files[".github/workflows/frontend.yml"] = pipeline_yaml.strip()
        return ToolAgentPhaseOutput(
            files=files,
            summary=f"CI/CD deliver: {'generated pipeline YAML' if files else 'no files generated'}.",
        )

    def _parse_json(self, raw: str) -> dict:
        """Parse JSON from LLM response, handling common formatting issues."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            import re
            match = re.search(r"\{[\s\S]*\}", raw)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            return {}
