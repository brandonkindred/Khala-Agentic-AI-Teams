"""FastAPI application for the Agentic Team Provisioning service.

This module is the thin app-assembly hub: it builds the app and mounts every
extracted router. Responsibility-focused sub-modules hold the actual logic:

* ``api.state`` — shared mutable globals (singletons, constants) and the
  business-logic helpers those endpoint groups share (roster enrichment,
  conversation-turn persistence, per-team infra/team lookups, retroactive
  startup provisioning).
* ``api.lifecycle`` — the ASGI startup hook.
* ``api.routes.teams`` / ``api.services.teams`` — teams CRUD + roster
* ``api.routes.conversations`` / ``api.services.conversations`` — conversations
* ``api.routes.testing`` / ``api.services.testing`` — mode, test-chat, test-pipeline
* ``api.routes.processes`` / ``api.services.processes`` — process CRUD
* ``api.routes.jobs`` / ``api.services.jobs`` — team job status
* ``api.routes.questions`` / ``api.services.questions`` — pending questions
* ``api.routes.assets`` / ``api.services.assets`` — team assets (file system)
* ``api.routes.forms`` / ``api.services.forms`` — team form records (database)
* ``api.routes.health`` — the ``/health`` liveness probe

Every moved public symbol is re-imported here so ``from …api.main import X``
and ``monkeypatch.setattr(main, "X", …)`` keep working unchanged: this module
remains the single owning namespace for the monkeypatched collaborators
(``_store``, ``_agent``, ``_test_store``, ``_pipeline_runner``,
``_save_agents_from_llm``, ``_save_agents_and_process``, ``_after_process_saved``,
``_get_infra_or_404``, ``_get_team_or_404``, ``_roster_agent_from_manifest``,
``enrich_roster_agent``, ``resolve_persona``, ``_chat_context_agents``,
``_build_test_agent``, ``_call_test_agent``, ``get_team_infrastructure``,
``register_team_manifests``, ``schedule_provision_step_agents``,
``provision_team``, …), which route and service modules dereference through
``main`` at call time.
"""

from __future__ import annotations

import asyncio  # noqa: F401 — re-export: tests monkeypatch main.asyncio.to_thread
import logging

from agent_team_studio.agentic_team_provisioning.api.lifecycle import _startup
from agent_team_studio.agentic_team_provisioning.api.state import (  # noqa: F401
    DEFAULT_SUGGESTIONS,
    GREETING,
    _after_process_saved,
    _agent,
    _build_test_agent,
    _call_test_agent,
    _chat_context_agents,
    _get_infra_or_404,
    _get_team_or_404,
    _pipeline_runner,
    _save_agents_and_process,
    _save_agents_from_llm,
    _store,
    _test_store,
    enrich_roster_agent,
    get_team_infrastructure,
    initialize_service,
    provision_team,
    register_team_manifests,
    resolve_persona,
    schedule_provision_step_agents,
)
from agent_team_studio.agentic_team_provisioning.postgres import SCHEMA as AGENTIC_POSTGRES_SCHEMA
from shared.app import create_team_app

logger = logging.getLogger(__name__)

app = create_team_app(
    service_name="agentic-team-provisioning",
    team_key="agentic_team_provisioning",
    title="Agentic Team Provisioning API",
    description="Create agentic teams and define their processes through conversation",
    version="0.1.0",
    postgres_schema=AGENTIC_POSTGRES_SCHEMA,
    on_startup=_startup,
)

# --- Mount extracted routers last (hub + globals already defined above, so the
# route modules' `from …api import main as _main` binds a fully-populated hub) ---
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    assets as assets_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    conversations as conversations_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    forms as forms_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    health as health_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    jobs as jobs_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    processes as processes_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    questions as questions_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    teams as teams_routes,
)
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    testing as testing_routes,
)
from agent_team_studio.agentic_team_provisioning.api.services.assets import (  # noqa: E402,F401
    _ASSET_UPLOAD_CHUNK_BYTES,  # re-export: tests monkeypatch via main
    _safe_asset_name,  # re-export: tests call directly via main
)
from agent_team_studio.agentic_team_provisioning.api.services.teams import (  # noqa: E402,F401
    _roster_agent_from_manifest,  # re-export: tests import + monkeypatch via main
)
from agent_team_studio.agentic_team_provisioning.api.services.testing import (  # noqa: E402,F401
    _dispatch_pipeline_run,  # re-export: hub call surface
    _find_agent_in_roster,  # re-export: tests monkeypatch via main
    _temporal_enabled,  # re-export: tests monkeypatch via main
)

_teams_router = teams_routes.router
_conversations_router = conversations_routes.router
_testing_router = testing_routes.router
_processes_router = processes_routes.router
_jobs_router = jobs_routes.router
_questions_router = questions_routes.router
_assets_router = assets_routes.router
_forms_router = forms_routes.router
_health_router = health_routes.router
app.include_router(_teams_router)
app.include_router(_conversations_router)
app.include_router(_testing_router)
app.include_router(_processes_router)
app.include_router(_jobs_router)
app.include_router(_questions_router)
app.include_router(_assets_router)
app.include_router(_forms_router)
app.include_router(_health_router)
