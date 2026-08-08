"""DbC docstring coverage for main.py + routes/testing.py route handlers.

Each test asserts a specific route handler's docstring documents its contract
under explicit ``Preconditions:``/``Postconditions:`` sections, per the
project's Design by Contract standard. Mirrors the existing pattern in
``test_api_router_scaffold.py`` (e.g. ``test_get_process_route_has_dbc_docstring``).
"""

from __future__ import annotations


def _assert_dbc_docstring(func) -> None:
    doc = func.__doc__
    assert doc
    assert "Preconditions:" in doc
    assert "Postconditions:" in doc


# ---------------------------------------------------------------------------
# api/main.py
# ---------------------------------------------------------------------------


def test_health_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import health

    _assert_dbc_docstring(health)


def test_list_team_agent_environments_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import (
        list_team_agent_environments,
    )

    _assert_dbc_docstring(list_team_agent_environments)


def test_list_team_jobs_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import list_team_jobs

    _assert_dbc_docstring(list_team_jobs)


def test_get_team_job_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import get_team_job

    _assert_dbc_docstring(get_team_job)


def test_list_team_questions_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import list_team_questions

    _assert_dbc_docstring(list_team_questions)


def test_submit_team_answers_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import submit_team_answers

    _assert_dbc_docstring(submit_team_answers)


def test_list_team_assets_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import list_team_assets

    _assert_dbc_docstring(list_team_assets)


def test_list_team_form_keys_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import list_team_form_keys

    _assert_dbc_docstring(list_team_form_keys)


def test_list_team_form_records_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import list_team_form_records

    _assert_dbc_docstring(list_team_form_records)


def test_create_team_form_record_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import create_team_form_record

    _assert_dbc_docstring(create_team_form_record)


def test_update_team_form_record_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import update_team_form_record

    _assert_dbc_docstring(update_team_form_record)


def test_delete_team_form_record_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.main import delete_team_form_record

    _assert_dbc_docstring(delete_team_form_record)


# ---------------------------------------------------------------------------
# api/routes/testing.py
# ---------------------------------------------------------------------------


def test_set_team_mode_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import set_team_mode

    _assert_dbc_docstring(set_team_mode)


def test_create_test_chat_session_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        create_test_chat_session,
    )

    _assert_dbc_docstring(create_test_chat_session)


def test_list_test_chat_sessions_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        list_test_chat_sessions,
    )

    _assert_dbc_docstring(list_test_chat_sessions)


def test_get_test_chat_session_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        get_test_chat_session,
    )

    _assert_dbc_docstring(get_test_chat_session)


def test_rename_test_chat_session_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        rename_test_chat_session,
    )

    _assert_dbc_docstring(rename_test_chat_session)


def test_delete_test_chat_session_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        delete_test_chat_session,
    )

    _assert_dbc_docstring(delete_test_chat_session)


def test_send_test_chat_message_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        send_test_chat_message,
    )

    _assert_dbc_docstring(send_test_chat_message)


def test_export_test_chat_session_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        export_test_chat_session,
    )

    _assert_dbc_docstring(export_test_chat_session)


def test_rate_test_chat_message_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        rate_test_chat_message,
    )

    _assert_dbc_docstring(rate_test_chat_message)


def test_get_agent_quality_scores_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        get_agent_quality_scores,
    )

    _assert_dbc_docstring(get_agent_quality_scores)


def test_start_pipeline_run_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import start_pipeline_run

    _assert_dbc_docstring(start_pipeline_run)


def test_list_pipeline_runs_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import list_pipeline_runs

    _assert_dbc_docstring(list_pipeline_runs)


def test_get_pipeline_run_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import get_pipeline_run

    _assert_dbc_docstring(get_pipeline_run)


def test_submit_pipeline_input_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        submit_pipeline_input,
    )

    _assert_dbc_docstring(submit_pipeline_input)


def test_cancel_pipeline_run_route_has_dbc_docstring() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes.testing import (
        cancel_pipeline_run,
    )

    _assert_dbc_docstring(cancel_pipeline_run)
