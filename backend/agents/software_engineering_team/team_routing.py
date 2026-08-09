"""Stack/team label routing helpers for the coding-team swarm.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
Normalizes Tech-Lead-emitted or persisted stack/team labels (e.g. "Node.js",
"frontend-v2", ".NET Core") to the canonical ``frontend_v2``/``backend_v2`` keys
used for task-to-worker routing and quality-gate agent-type selection.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from software_engineering_team.models import StackSpec, Task

logger = logging.getLogger(__name__)

_FRONTEND_V2_EXPLICIT = {
    "frontend_v2",
    "frontend-v2",
    "frontend v2",
    "frontend_code_v2",
    "frontend-code-v2",
    "frontend code v2",
}
_BACKEND_V2_EXPLICIT = {
    "backend_v2",
    "backend-v2",
    "backend v2",
    "backend_code_v2",
    "backend-code-v2",
    "backend code v2",
}
_FRONTEND_HINTS = {
    "angular",
    "react",
    "vue",
    "typescript",
    "javascript",
    "html",
    "css",
    "scss",
    "ui",
    "ux",
    "frontend",
}
_BACKEND_HINTS = {
    "api",
    "apis",
    "backend",
    "build",
    "ci",
    "ci_cd",
    "cicd",
    "database",
    "databases",
    "devops",
    "django",
    "express",
    "fastapi",
    "flask",
    "infrastructure",
    "java",
    "node",
    "postgres",
    "python",
    "server",
    "servers",
    "service",
    "services",
    "spring",
}
_BACKEND_TEAM_ALIASES = {
    # Compact separator-less form of the canonical team name. The token-exact frontend/
    # backend check below only matches when "backend" is its own token (so "backendv2"
    # collapses to a single token); this alias preserves the old substring behavior for
    # the v2 label without re-introducing matches on unrelated words like "mybackend".
    "backendv2",
    "api",
    "apis",
    "backend_api",
    "ci",
    "ci_cd",
    "cicd",
    "database",
    "databases",
    "data",
    "db",
    "devops",
    "infra",
    "infrastructure",
    "node",
    "platform",
    "server",
    "servers",
    "service",
    "services",
    # The entries above are legacy generic aliases (e.g. "service", "data") kept for
    # backward compatibility. The entries below are concrete backend languages/frameworks
    # a Tech Lead may name as the target_team instead of the canonical "backend_v2"; new
    # additions should stay unambiguous tech tokens (never generic words like "build").
    "python",
    "java",
    "nodejs",
    "node_js",
    "golang",
    "rust",
    "ruby",
    "php",
    "dotnet",
    # .NET spellings: "_team_key" normalizes ".NET" -> "net" and ".NET Core" -> "net_core",
    # so alias those normalized forms (not just "dotnet") or they fail to route.
    "net",
    "netcore",
    "net_core",
    "aspnet",
    "asp_net",
    "django",
    "flask",
    "fastapi",
    "spring",
    "springboot",
    "spring_boot",
    "express",
    "express_js",
    "postgres",
    "postgresql",
    "mysql",
    "mongodb",
}
# Frontend-owned target_team/stack aliases. Mirrors _BACKEND_TEAM_ALIASES so common
# UI/UX target labels a Tech Lead may emit (or copy from a "UI" stack name) canonicalize
# to frontend_v2 rather than failing to match the available frontend worker. Compared by
# exact normalized-label equality in _team_key (``text in _FRONTEND_TEAM_ALIASES``), so
# unrelated words containing these as a substring (e.g. "build", "guides") are unaffected.
_FRONTEND_TEAM_ALIASES = {
    # Compact separator-less form of the canonical team name (see _BACKEND_TEAM_ALIASES).
    "frontendv2",
    "ui",
    "ux",
    "ui_ux",
    "ux_ui",
    "web",
    "webapp",
    "web_app",
    "client",
    # The entries above are legacy generic aliases (e.g. "client", "webapp") kept for
    # backward compatibility. The entries below are concrete frontend languages/frameworks
    # a Tech Lead may name as the target_team instead of the canonical "frontend_v2"; new
    # additions should stay unambiguous tech tokens only.
    "angular",
    "angularjs",
    "angular_js",
    "react",
    "reactjs",
    "react_js",
    "vue",
    "vuejs",
    "vue_js",
    "svelte",
    "html",
    "css",
    "scss",
    "sass",
    "tailwind",
    "nextjs",
    "next_js",
    # NOTE: bare "typescript"/"javascript" are intentionally NOT aliased here. They are
    # ambiguous — this team's backend_v2 stack includes Node.js — so a "TypeScript"/
    # "JavaScript" target_team must not be hard-routed to frontend. The Tech Lead should
    # emit the canonical frontend_v2/backend_v2 (or a specific framework) for those.
}
_BACKEND_V2_STACK_SPEC = {
    "name": "backend_v2",
    "tools_services": ["Java", "Python", "Node.js", "Databases", "APIs", "DevOps"],
}


def _team_key(value: Optional[str]) -> str:
    """Normalize a stack/team label for routing comparisons."""
    # Collapse every run of non-alphanumeric characters (dots, hyphens, spaces, repeats)
    # to a single underscore, so "Node.js", "Node. JS" and "node-js" all normalize to the
    # same token sequence; strip leading/trailing separators.
    raw_text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", raw_text).strip("_")
    stripped = re.sub(r"[^a-z0-9]+", "", raw_text)
    if stripped == "frontend":
        return "frontend_v2"
    if stripped == "backend":
        return "backend_v2"
    # Exact-token membership (not substring) so unrelated labels that merely contain
    # "frontend"/"backend" as a substring (e.g. "myfrontend") are not misclassified.
    tokens = set(text.split("_"))
    has_frontend = "frontend" in tokens
    has_backend = "backend" in tokens
    if has_frontend and has_backend:
        logger.warning(
            "Ambiguous team label %r contains both frontend and backend; "
            "using existing frontend_v2 precedence",
            value,
        )
    # Heuristic: frontend takes precedence when a raw label contains multiple stack names.
    if has_frontend:
        return "frontend_v2"
    if has_backend:
        return "backend_v2"
    if text in _FRONTEND_TEAM_ALIASES:
        return "frontend_v2"
    if text in _BACKEND_TEAM_ALIASES:
        return "backend_v2"
    return text


def _stack_hint_tokens(spec: StackSpec) -> set[str]:
    """Return normalized stack hint tokens without substring false positives."""
    tokens: set[str] = set()
    for raw_part in [spec.name, *(spec.tools_services or [])]:
        part = str(raw_part or "").strip().lower()
        if not part:
            continue
        normalized = re.sub(r"[^a-z0-9]+", "_", part).strip("_")
        if normalized:
            tokens.add(normalized)
        for token in re.findall(r"[a-z0-9]+", part):
            tokens.add(token)
            if token.endswith("s") and len(token) > 3:
                tokens.add(token[:-1])
    return tokens


def _v2_team_kind_for_stack(spec: StackSpec) -> Optional[str]:
    """Return 'frontend'/'backend' when this stack should be backed by a v2 team."""
    raw_name = (spec.name or "").strip().lower()
    normalized_name = raw_name.replace("_", " ").replace("-", " ")
    exactish = {raw_name, normalized_name}
    hint_tokens = _stack_hint_tokens(spec)
    if exactish & _FRONTEND_V2_EXPLICIT:
        return "frontend"
    if exactish & _BACKEND_V2_EXPLICIT:
        return "backend"
    canonical_key = _team_key(spec.name)
    if canonical_key == "frontend_v2":
        return "frontend"
    if canonical_key == "backend_v2":
        return "backend"
    if hint_tokens & _FRONTEND_HINTS:
        return "frontend"
    if hint_tokens & _BACKEND_HINTS:
        return "backend"
    return None


def _quality_gate_agent_type(stack_name: Optional[str]) -> str:
    """Map coding-team/v2 stack names to quality-gate agent types."""
    key = _team_key(stack_name)
    if key == "frontend_v2":
        return "frontend"
    if key == "backend_v2":
        return "backend"
    inferred = _v2_team_kind_for_stack(
        StackSpec(name=(stack_name or "").strip(), tools_services=[])
    )
    if inferred in {"frontend", "backend"}:
        return inferred
    return (stack_name or "backend").strip() or "backend"


def _target_matches_agent(target_team: Optional[str], agent_id: str) -> bool:
    """Whether an agent can execute a task with the target_team hint."""
    target = _team_key(target_team)
    if not target:
        return True
    return target == _team_key(agent_id)


def _worker_team_key(worker: Any) -> str:
    """Return the scheduler team key for a worker instance."""
    kind = getattr(worker, "team_kind", None)
    if kind == "frontend":
        return "frontend_v2"
    if kind == "backend":
        return "backend_v2"
    spec = getattr(worker, "stack_spec", None)
    raw_name = (getattr(spec, "name", "") or "").strip().lower()
    normalized_name = raw_name.replace("_", " ").replace("-", " ")
    exactish = {raw_name, normalized_name}
    if exactish & _FRONTEND_V2_EXPLICIT:
        return "frontend_v2"
    if exactish & _BACKEND_V2_EXPLICIT:
        return "backend_v2"
    return raw_name.replace("-", "_").replace(" ", "_")


def _stack_spec_from_raw(entry: Any) -> StackSpec:
    """Build a StackSpec from a persisted/raw stack entry without raising."""
    spec = entry if isinstance(entry, dict) else {}
    name = str(spec.get("name") or "")
    tools = spec.get("tools_services")
    return StackSpec(name=name, tools_services=list(tools) if isinstance(tools, list) else [])


def _ensure_target_team_stack_specs(
    stacks_raw: Any,
    tasks: List[Task],
) -> List[Dict[str, Any]]:
    """Ensure targeted frontend/backend tasks have matching v2 team workers in the roster."""
    stacks = list(stacks_raw) if isinstance(stacks_raw, list) else []
    present = {_v2_team_kind_for_stack(_stack_spec_from_raw(entry)) for entry in stacks}
    target_keys = {_team_key(task.target_team) for task in tasks if task.target_team}

    if "frontend_v2" in target_keys and "frontend" not in present:
        stacks.append(
            {
                "name": "frontend_v2",
                "tools_services": ["Angular", "TypeScript", "React", "CSS", "HTML"],
            }
        )
    if "backend_v2" in target_keys and "backend" not in present:
        stacks.append(dict(_BACKEND_V2_STACK_SPEC))
    return stacks
