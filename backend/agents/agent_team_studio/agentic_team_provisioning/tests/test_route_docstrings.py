"""DbC docstring coverage for the agentic team provisioning API surface.

Regression guard for the doc/contract fix applied against the parent issue's
finding that most route handlers here had only one-line docstrings, and for
the follow-up split of main.py into per-domain router/service pairs. Route
handlers that are thin delegates (api.routes.*) are allowed a short
docstring that points at the service function carrying the full contract,
rather than duplicating it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest

_HERE = Path(__file__).resolve().parent
_API_DIR = _HERE.parent / "api"
_MAIN_PY = _API_DIR / "main.py"
_ROUTES_TESTING_PY = _API_DIR / "routes" / "testing.py"
_SERVICES_TESTING_PY = _API_DIR / "services" / "testing.py"
_ROUTES_PROCESSES_PY = _API_DIR / "routes" / "processes.py"
_SERVICES_PROCESSES_PY = _API_DIR / "services" / "processes.py"
_ROUTES_JOBS_PY = _API_DIR / "routes" / "jobs.py"
_SERVICES_JOBS_PY = _API_DIR / "services" / "jobs.py"
_ROUTES_QUESTIONS_PY = _API_DIR / "routes" / "questions.py"
_SERVICES_QUESTIONS_PY = _API_DIR / "services" / "questions.py"
_ROUTES_ASSETS_PY = _API_DIR / "routes" / "assets.py"
_SERVICES_ASSETS_PY = _API_DIR / "services" / "assets.py"
_ROUTES_FORMS_PY = _API_DIR / "routes" / "forms.py"
_SERVICES_FORMS_PY = _API_DIR / "services" / "forms.py"

# Only the liveness probe is still defined directly on main.py; every other
# endpoint group has been extracted into a dedicated router/service pair.
_MAIN_ROUTE_HANDLERS = {
    "health",
}

_ROUTES_PROCESSES_HANDLERS = {
    "list_processes",
    "get_process",
    "create_process",
    "update_process",
    "recommend_agents_for_step",
    "list_team_agent_environments",
}

_SERVICES_PROCESSES_PUBLIC_FUNCS = set(_ROUTES_PROCESSES_HANDLERS)

_ROUTES_JOBS_HANDLERS = {
    "list_team_jobs",
    "get_team_job",
}

_SERVICES_JOBS_PUBLIC_FUNCS = set(_ROUTES_JOBS_HANDLERS)

_ROUTES_QUESTIONS_HANDLERS = {
    "list_team_questions",
    "submit_team_answers",
}

_SERVICES_QUESTIONS_PUBLIC_FUNCS = set(_ROUTES_QUESTIONS_HANDLERS)

_ROUTES_ASSETS_HANDLERS = {
    "list_team_assets",
    "download_team_asset",
    "upload_team_asset",
}

_SERVICES_ASSETS_PUBLIC_FUNCS = set(_ROUTES_ASSETS_HANDLERS)

_ROUTES_FORMS_HANDLERS = {
    "list_team_form_keys",
    "list_team_form_records",
    "create_team_form_record",
    "update_team_form_record",
    "delete_team_form_record",
}

_SERVICES_FORMS_PUBLIC_FUNCS = set(_ROUTES_FORMS_HANDLERS)

_ROUTES_TESTING_HANDLERS = {
    "set_team_mode",
    "create_test_chat_session",
    "list_test_chat_sessions",
    "get_test_chat_session",
    "rename_test_chat_session",
    "delete_test_chat_session",
    "send_test_chat_message",
    "export_test_chat_session",
    "rate_test_chat_message",
    "get_agent_quality_scores",
    "start_pipeline_run",
    "list_pipeline_runs",
    "get_pipeline_run",
    "submit_pipeline_input",
    "cancel_pipeline_run",
}

_SERVICES_TESTING_PUBLIC_FUNCS = {
    "set_team_mode",
    "create_test_chat_session",
    "list_test_chat_sessions",
    "get_test_chat_session",
    "rename_test_chat_session",
    "delete_test_chat_session",
    "send_test_chat_message",
    "export_test_chat_session",
    "rate_test_chat_message",
    "get_agent_quality_scores",
    "start_pipeline_run",
    "list_pipeline_runs",
    "get_pipeline_run",
    "submit_pipeline_input",
    "cancel_pipeline_run",
}


def _top_level_funcs(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _missing_contract_sections(doc: str | None) -> List[str]:
    if not doc:
        return ["docstring"]
    missing = []
    if "Preconditions" not in doc:
        missing.append("Preconditions")
    if "Postconditions" not in doc:
        missing.append("Postconditions")
    return missing


def test_main_route_handlers_document_contracts():
    funcs = _top_level_funcs(_MAIN_PY)
    violations = []
    for name in sorted(_MAIN_ROUTE_HANDLERS):
        node = funcs.get(name)
        assert node is not None, f"{name} not found as a top-level function in main.py"
        missing = _missing_contract_sections(ast.get_docstring(node))
        if missing:
            violations.append(f"main.py:{node.lineno}: {name} missing {', '.join(missing)}")
    assert violations == []


def test_routes_testing_handlers_document_delegation():
    funcs = _top_level_funcs(_ROUTES_TESTING_PY)
    violations = []
    for name in sorted(_ROUTES_TESTING_HANDLERS):
        node = funcs.get(name)
        assert node is not None, f"{name} not found as a top-level function in routes/testing.py"
        doc = ast.get_docstring(node)
        if not doc or "api.services.testing" not in doc:
            violations.append(
                f"routes/testing.py:{node.lineno}: {name} missing a docstring "
                "pointing at its api.services.testing delegate"
            )
    assert violations == []


def test_services_testing_functions_document_contracts():
    funcs = _top_level_funcs(_SERVICES_TESTING_PY)
    violations = []
    for name in sorted(_SERVICES_TESTING_PUBLIC_FUNCS):
        node = funcs.get(name)
        assert node is not None, f"{name} not found as a top-level function in services/testing.py"
        missing = _missing_contract_sections(ast.get_docstring(node))
        if missing:
            violations.append(
                f"services/testing.py:{node.lineno}: {name} missing {', '.join(missing)}"
            )
    assert violations == []


# Remaining domains split out of main.py (processes, jobs, questions, assets,
# forms) share the same two checks (routes delegate, services document
# contracts) — parametrized rather than duplicating the testing triplet above
# five more times.
_DOMAINS = {
    "processes": (
        _ROUTES_PROCESSES_PY,
        _ROUTES_PROCESSES_HANDLERS,
        _SERVICES_PROCESSES_PY,
        _SERVICES_PROCESSES_PUBLIC_FUNCS,
    ),
    "jobs": (
        _ROUTES_JOBS_PY,
        _ROUTES_JOBS_HANDLERS,
        _SERVICES_JOBS_PY,
        _SERVICES_JOBS_PUBLIC_FUNCS,
    ),
    "questions": (
        _ROUTES_QUESTIONS_PY,
        _ROUTES_QUESTIONS_HANDLERS,
        _SERVICES_QUESTIONS_PY,
        _SERVICES_QUESTIONS_PUBLIC_FUNCS,
    ),
    "assets": (
        _ROUTES_ASSETS_PY,
        _ROUTES_ASSETS_HANDLERS,
        _SERVICES_ASSETS_PY,
        _SERVICES_ASSETS_PUBLIC_FUNCS,
    ),
    "forms": (
        _ROUTES_FORMS_PY,
        _ROUTES_FORMS_HANDLERS,
        _SERVICES_FORMS_PY,
        _SERVICES_FORMS_PUBLIC_FUNCS,
    ),
}


@pytest.mark.parametrize("domain", sorted(_DOMAINS))
def test_routes_handlers_document_delegation(domain: str):
    routes_path, handlers, _services_path, _services_funcs = _DOMAINS[domain]
    funcs = _top_level_funcs(routes_path)
    violations = []
    for name in sorted(handlers):
        node = funcs.get(name)
        assert node is not None, f"{name} not found as a top-level function in routes/{domain}.py"
        doc = ast.get_docstring(node)
        if not doc or f"api.services.{domain}" not in doc:
            violations.append(
                f"routes/{domain}.py:{node.lineno}: {name} missing a docstring "
                f"pointing at its api.services.{domain} delegate"
            )
    assert violations == []


@pytest.mark.parametrize("domain", sorted(_DOMAINS))
def test_services_functions_document_contracts(domain: str):
    _routes_path, _handlers, services_path, services_funcs = _DOMAINS[domain]
    funcs = _top_level_funcs(services_path)
    violations = []
    for name in sorted(services_funcs):
        node = funcs.get(name)
        assert node is not None, f"{name} not found as a top-level function in services/{domain}.py"
        missing = _missing_contract_sections(ast.get_docstring(node))
        if missing:
            violations.append(
                f"services/{domain}.py:{node.lineno}: {name} missing {', '.join(missing)}"
            )
    assert violations == []
