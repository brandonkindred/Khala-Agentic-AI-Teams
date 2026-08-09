"""DbC docstring coverage for the testing-mode API surface (main.py forms/jobs/
questions/assets routes, api.routes.testing, api.services.testing).

Regression guard for the doc/contract fix applied against the parent issue's
finding that most route handlers here had only one-line docstrings. Route
handlers that are thin delegates (api.routes.testing) are allowed a short
docstring that points at the service function carrying the full contract,
rather than duplicating it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

_HERE = Path(__file__).resolve().parent
_API_DIR = _HERE.parent / "api"
_MAIN_PY = _API_DIR / "main.py"
_ROUTES_TESTING_PY = _API_DIR / "routes" / "testing.py"
_SERVICES_TESTING_PY = _API_DIR / "services" / "testing.py"

# Route handlers in main.py named in (or adjacent to) the parent issue's
# "one-line docstring" finding — jobs/questions/assets/forms endpoints plus
# health. Process CRUD and asset up/download were already fully documented
# before this fix and are covered incidentally.
_MAIN_ROUTE_HANDLERS = {
    "health",
    "list_processes",
    "get_process",
    "create_process",
    "update_process",
    "recommend_agents_for_step",
    "list_team_agent_environments",
    "list_team_jobs",
    "get_team_job",
    "list_team_questions",
    "submit_team_answers",
    "list_team_assets",
    "download_team_asset",
    "upload_team_asset",
    "list_team_form_keys",
    "list_team_form_records",
    "create_team_form_record",
    "update_team_form_record",
    "delete_team_form_record",
}

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
