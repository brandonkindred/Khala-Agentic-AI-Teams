"""Testing mode, test-chat, and test-pipeline domain logic for agentic HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers, including Temporal vs
    thread dispatch. Collaborators are read from ``api.main`` at call time.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import HTTPException, Response

from agent_team_studio.agentic_team_provisioning.models import (
    AgenticTeamAgent,
    AgentQualityScore,
    CreateTestChatSessionRequest,
    ProcessDefinition,
    RateMessageRequest,
    RenameTestChatSessionRequest,
    SendTestChatMessageRequest,
    SetTeamModeRequest,
    StartPipelineRunRequest,
    SubmitPipelineInputRequest,
    TestChatMessage,
    TestChatSession,
    TestChatSessionDetail,
    TestPipelineRun,
)
from agent_team_studio.agentic_team_provisioning.runtime.agent_builder import (
    generate_starter_prompts,
)

logger = logging.getLogger(__name__)


def set_team_mode(team_id: str, req: SetTeamModeRequest):
    """Toggle team between development and testing mode."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    _main._test_store.set_team_mode(team_id, req.mode.value)
    return {"team_id": team_id, "mode": req.mode.value}


def _find_agent_in_roster(team_id: str, agent_name: str) -> AgenticTeamAgent:
    """Look up an agent by name in the team roster."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    agents = _main._store.list_team_agents(team_id)
    for a in agents:
        if a.agent_name == agent_name:
            return a
    raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found in team roster")


def create_test_chat_session(team_id: str, req: CreateTestChatSessionRequest):
    """Create a new chat test session for an agent."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    _main._find_agent_in_roster(team_id, req.agent_name)
    session_id = str(uuid.uuid4())
    row = _main._test_store.create_chat_session(session_id, team_id, req.agent_name)
    return TestChatSession(**row)


def list_test_chat_sessions(team_id: str, agent_name: Optional[str] = None):
    """List chat test sessions for a team, optionally filtered by agent.

    Preconditions: ``team_id`` is a non-empty string.
    Postconditions: ``200`` with a ``TestChatSession`` for each session belonging
        to ``team_id`` (filtered to ``agent_name`` when given); ``404`` if
        ``team_id`` is unknown, consistent with the sibling test-chat endpoints.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    _main._get_team_or_404(team_id)
    rows = _main._test_store.list_chat_sessions(team_id, agent_name=agent_name)
    return [TestChatSession(**r) for r in rows]


def get_test_chat_session(team_id: str, session_id: str):
    """Get a chat session with full message history and suggested prompts.

    Preconditions: ``team_id`` and ``session_id`` are non-empty strings.
    Postconditions: ``200`` with the session, its messages, and starter prompts
        (only generated when the session has no messages yet); ``404`` if the
        session doesn't exist or belongs to a different team. If starter-prompt
        generation raises a 404 because the session's agent isn't on the
        roster, or ``LookupError`` because the linked Manifest is missing
        (orphan ``manifest_id``), the prompts list is empty rather than failing
        the request; any other failure (e.g. a registry 500) propagates instead
        of being swallowed.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    session_row = _main._test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = _main._test_store.list_chat_messages(session_id)
    session = TestChatSession(**session_row)

    prompts: list[str] = []
    if not messages:
        try:
            agent_def = _main._find_agent_in_roster(team_id, session.agent_name)
            persona = _main.resolve_persona(agent_def.manifest_id)
            prompts = generate_starter_prompts(
                agent_def.agent_name, persona.role, persona.skills, persona.expertise
            )
        except LookupError as exc:
            # Orphan manifest_id: soft-fail like list enrichment — empty prompts,
            # not a 500 on an otherwise-valid session GET.
            # Log via main's logger so hub-scoped warning assertions keep working.
            _main.logger.warning(
                "Could not generate starter prompts for session %s (agent=%s): %s",
                session_id,
                session.agent_name,
                exc,
            )
        except HTTPException as exc:
            # Only the genuine "agent not on roster" case falls back to an empty
            # prompt list. Anything else (e.g. a registry 500) is a real failure
            # worth surfacing to the caller, not silently swallowing.
            if exc.status_code != 404:
                raise
            _main.logger.warning(
                "Could not generate starter prompts for session %s (agent=%s): %s",
                session_id,
                session.agent_name,
                exc.detail,
            )

    return TestChatSessionDetail(
        session=session,
        messages=[TestChatMessage(**m) for m in messages],
        suggested_prompts=prompts,
    )


def rename_test_chat_session(team_id: str, session_id: str, req: RenameTestChatSessionRequest):
    """Rename a chat test session."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    session_row = _main._test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _main._test_store.rename_chat_session(session_id, req.session_name)
    return {"session_id": session_id, "session_name": req.session_name}


def delete_test_chat_session(team_id: str, session_id: str):
    """Delete a chat test session and its messages.

    Preconditions: ``team_id`` and ``session_id`` are non-empty strings.
    Postconditions: ``204`` and the session (plus messages) removed when the
        session belongs to ``team_id``; ``404`` when the session is unknown or
        owned by a different team (store unchanged).
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    session_row = _main._test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _main._test_store.delete_chat_session(session_id)
    return Response(status_code=204)


def send_test_chat_message(team_id: str, session_id: str, req: SendTestChatMessageRequest):
    """Send a message to an agent and get a synchronous response.

    The full conversation history is sent to the agent for multi-turn context.

    Preconditions: ``team_id``/``session_id`` refer to an existing session;
        ``req.content`` is non-empty (enforced by the request model).
    Postconditions: ``200`` with the session and its full message list,
        including the new user/assistant turn; ``404`` if the session is
        unknown or belongs to a different team; ``502`` if the agent
        invocation fails. The user and assistant messages are persisted
        together as a single turn only after the agent call succeeds, so a
        failed invocation leaves no orphaned user message for a retry to
        duplicate.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    session_row = _main._test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    agent_name = session_row["agent_name"]
    agent_def = _main._find_agent_in_roster(team_id, agent_name)

    history = _main._test_store.list_chat_messages(session_id)
    context_parts = []
    for msg in history:
        prefix = "User" if msg["role"] == "user" else "Assistant"
        context_parts.append(f"{prefix}: {msg['content']}")
    context_parts.append(f"User: {req.content}")
    full_context = "\n\n".join(context_parts)

    # Build and invoke the agent. This local test-chat path has no cognition
    # injector (no proxy / open side channel) and no idempotency ledger, so it
    # uses the plain runtime rather than the cognition-aware wrapper — advisory
    # rules + memory digest are rendered on the gated sandbox invoke path, where
    # the shim opens the channel.
    try:
        persona = _main.resolve_persona(agent_def.manifest_id)
        agent_instance = _main._build_test_agent(
            agent_def.agent_name,
            persona.role,
            persona.skills,
            persona.capabilities,
            persona.tools,
            persona.expertise,
        )
        response_text = _main._call_test_agent(agent_instance, full_context)
    except Exception as exc:
        logger.exception("Agent invocation failed for test-chat session %s", session_id)
        raise HTTPException(status_code=502, detail="Agent invocation failed") from exc

    user_msg_id = str(uuid.uuid4())
    _main._test_store.create_chat_message(user_msg_id, session_id, "user", req.content)
    asst_msg_id = str(uuid.uuid4())
    _main._test_store.create_chat_message(asst_msg_id, session_id, "assistant", response_text)

    all_messages = _main._test_store.list_chat_messages(session_id)
    return {
        "session": TestChatSession(**session_row),
        "messages": [TestChatMessage(**m) for m in all_messages],
    }


def export_test_chat_session(team_id: str, session_id: str):
    """Export a chat session transcript as Markdown text."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    session_row = _main._test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = _main._test_store.list_chat_messages(session_id)
    agent_name = session_row["agent_name"]
    session_name = session_row.get("session_name") or f"Chat with {agent_name}"

    lines = [f"# {session_name}", f"Agent: {agent_name}", ""]
    for msg in messages:
        role_label = "**User**" if msg["role"] == "user" else f"**{agent_name}**"
        rating_str = ""
        if msg.get("rating"):
            rating_str = " \u2705" if msg["rating"] == "thumbs_up" else " \u274c"
        lines.append(f"{role_label}{rating_str}:")
        lines.append(msg["content"])
        lines.append("")

    return Response(
        content="\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


def rate_test_chat_message(team_id: str, message_id: str, req: RateMessageRequest):
    """Rate an assistant message (thumbs up/thumbs down)."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    if not _main._test_store.update_message_rating(team_id, message_id, req.rating.value):
        raise HTTPException(status_code=404, detail="Message not found")
    return {"message_id": message_id, "rating": req.rating.value}


def get_agent_quality_scores(team_id: str):
    """Get aggregated quality scores per agent based on chat ratings."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    _main._get_team_or_404(team_id)
    rows = _main._test_store.get_agent_quality_scores(team_id)
    return [AgentQualityScore(**r) for r in rows]


def _temporal_enabled() -> bool:
    """Return whether Temporal dispatch is active (``TEMPORAL_ADDRESS`` set).

    Preconditions: none.
    Postconditions: ``True`` iff ``shared.temporal`` is importable and reports Temporal
        enabled; ``False`` if Temporal is disabled or ``shared.temporal`` is absent (so
        the daemon-thread path is always reachable).
    """
    try:
        from shared.temporal import is_temporal_enabled
    except ImportError:
        return False
    return is_temporal_enabled()


def _dispatch_pipeline_run(
    run_id: str,
    team_agents: list[AgenticTeamAgent],
    process_def: ProcessDefinition,
    initial_input: Optional[str],
    *,
    temporal_owned: bool,
) -> str:
    """Dispatch a pipeline run via Temporal when enabled, else a daemon thread.

    Preconditions:
        - ``run_id`` refers to a run already created in the store with
          ``temporal_owned`` set to the same value passed here.
        - ``temporal_owned`` is the single ``_temporal_enabled()`` reading the caller
          used for the run's stored flag — computed once and passed in, never
          recomputed here, so the dispatch path can't diverge from what was persisted
          if Temporal availability changes between the two checks.

    Postconditions:
        - Starts exactly one execution path, selected by ``temporal_owned``, and
          returns its label ("Temporal" or "thread"). When true the run is started as
          a durable ``AgenticPipelineWorkflow``; otherwise the legacy daemon-thread
          path runs unchanged.
        - Any failure while starting the workflow propagates to the caller, which marks
          the run FAILED — a Temporal-enabled run is never silently downgraded.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    if temporal_owned:
        from agent_team_studio.agentic_team_provisioning.temporal.start_workflow import (
            start_agentic_pipeline_workflow,
        )

        team_agents_json = [a.model_dump(mode="json") for a in team_agents]
        process_json = process_def.model_dump(mode="json")
        start_agentic_pipeline_workflow(run_id, team_agents_json, process_json, initial_input)
        return "Temporal"

    _main._pipeline_runner.start_run(run_id, team_agents, process_def)
    return "thread"


def start_pipeline_run(team_id: str, req: StartPipelineRunRequest):
    """Start an end-to-end pipeline test run."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    process = None
    for p in team.processes:
        if p.process_id == req.process_id:
            process = p
            break
    if process is None:
        raise HTTPException(status_code=404, detail="Process not found")

    process_def = (
        process if isinstance(process, ProcessDefinition) else ProcessDefinition(**process)
    )

    team_agents_raw = _main._store.list_team_agents(team_id)
    team_agents = [
        a if isinstance(a, AgenticTeamAgent) else AgenticTeamAgent(**a) for a in team_agents_raw
    ]

    temporal_owned = _main._temporal_enabled()
    run_id = str(uuid.uuid4())
    run_row = _main._test_store.create_pipeline_run(
        run_id, team_id, req.process_id, req.initial_input, temporal_owned=temporal_owned
    )

    try:
        dispatch_method = _main._dispatch_pipeline_run(
            run_id, team_agents, process_def, req.initial_input, temporal_owned=temporal_owned
        )
    except Exception as exc:
        logger.exception("Failed to dispatch agentic pipeline run %s", run_id)
        _main._test_store.try_fail_pipeline_run(run_id, f"Dispatch failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to start pipeline run.") from exc
    logger.info("Agentic pipeline run %s dispatched via %s", run_id, dispatch_method)

    return TestPipelineRun(**run_row)


def list_pipeline_runs(team_id: str):
    """List pipeline test runs for a team."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    _main._get_team_or_404(team_id)
    rows = _main._test_store.list_pipeline_runs(team_id)
    return [TestPipelineRun(**r) for r in rows]


def get_pipeline_run(team_id: str, run_id: str):
    """Get the current status and step results of a pipeline test run."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    row = _main._test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return TestPipelineRun(**row)


def submit_pipeline_input(team_id: str, run_id: str, req: SubmitPipelineInputRequest):
    """Submit human input at a WAIT step to resume the pipeline."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    row = _main._test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if row["status"] != "waiting_for_input":
        raise HTTPException(status_code=400, detail="Pipeline is not waiting for input")

    if _main._test_store.is_pipeline_run_temporal_owned(run_id):
        from agent_team_studio.agentic_team_provisioning.temporal import WORKFLOW_ID_PREFIX
        from shared.temporal import signal_workflow_sync

        if not _main._test_store.try_resume_pipeline_run_temporal(run_id, req.input):
            raise HTTPException(
                status_code=409,
                detail="Pipeline run is no longer resumable (it timed out, was cancelled, "
                "or was reaped). Start a new run.",
            )
        try:
            signal_workflow_sync(f"{WORKFLOW_ID_PREFIX}{run_id}", "submit_input", req.input)
        except Exception:
            logger.warning(
                "Failed to signal agentic pipeline run %s; the resume is durably recorded "
                "and will be reconciled at the WAIT timeout",
                run_id,
                exc_info=True,
            )
        updated = _main._test_store.get_pipeline_run(run_id)
        return TestPipelineRun(**(updated or row))

    if not _main._pipeline_runner.submit_human_input(run_id, req.input):
        raise HTTPException(
            status_code=409,
            detail="Pipeline run is no longer resumable (it timed out, was cancelled, "
            "or was reaped). Start a new run.",
        )
    updated = _main._test_store.get_pipeline_run(run_id)
    return TestPipelineRun(**(updated or row))


def cancel_pipeline_run(team_id: str, run_id: str):
    """Cancel a running or waiting pipeline test run."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    row = _main._test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if row["status"] not in ("running", "waiting_for_input"):
        raise HTTPException(status_code=400, detail="Pipeline is not in a cancellable state")

    if _main._test_store.is_pipeline_run_temporal_owned(run_id):
        from agent_team_studio.agentic_team_provisioning.temporal import WORKFLOW_ID_PREFIX
        from shared.temporal import cancel_workflow_sync

        _main._test_store.try_cancel_pipeline_run(run_id)
        try:
            cancel_workflow_sync(f"{WORKFLOW_ID_PREFIX}{run_id}")
        except Exception:
            logger.warning(
                "Failed to cancel agentic pipeline workflow for run %s", run_id, exc_info=True
            )
        updated = _main._test_store.get_pipeline_run(run_id)
        return TestPipelineRun(**(updated or row))

    _main._pipeline_runner.cancel_run(run_id)
    updated = _main._test_store.get_pipeline_run(run_id)
    return TestPipelineRun(**(updated or row))
