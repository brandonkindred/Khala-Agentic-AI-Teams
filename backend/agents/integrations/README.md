# Shared Integrations Layer

This module provides a **single, shared integrations layer** that any agent team can use.

It is intentionally outside individual teams so integrations are configured once and reused by all workflows.

## What this gives you

- A canonical request/response contract (`IntegrationRequest`/`IntegrationResponse`)
- A registry-driven provider catalog loaded from YAML
- Capability routing (map `ticketing.create` to Jira/Trello, `chat.notify` to Slack, etc.)
- One service entry point (`IntegrationService`) for all agent teams
- Agent discovery API (`discover_integrations`) so agents can determine what each integration can do
- Transport-agnostic adapter surface that supports API and MCP-backed providers
- MCP Tool Gateway for discovering, selecting, adding, and configuring MCP tools per provider

## Module layout

```text
agents/integrations/
  contracts.py                  # canonical request/response model
  registry.py                   # provider registration and YAML loading
  router.py                     # capability -> provider resolution
  adapters.py                   # API/MCP adapter abstraction
  service.py                    # shared integration entry point + MCP gateway + discovery
  config/providers.example.yaml # sample provider config
```

## Supported integration catalog (configurable)

The example config now includes detailed capability/action metadata for:

- Slack
- Fireflies.ai
- Jira
- Trello
- Obsidian
- Figma
- Google Drive
- Dropbox
- Google Workspace
- AWS
- Google Cloud
- DigitalOcean
- Heroku
- Zapier
- n8n
- GitHub
- GitLab

Each provider can define:

- `capabilities` — canonical capability IDs agents can request.
- `actions` — human-readable action descriptions agents/planners can inspect.
- `auth` — auth type/scopes needed.
- `settings` — tenant/workspace/project defaults.
- `mcp_tools` — tool catalog for MCP providers, including per-tool capabilities and config.

## Agent discovery flow

Agents can inspect all enabled integrations and capabilities before selecting tools:

```python
from integrations.adapters import ApiMcpAdapter
from integrations.registry import IntegrationRegistry
from integrations.router import CapabilityRouter
from integrations.service import IntegrationService

registry = IntegrationRegistry.from_yaml("integrations/config/providers.example.yaml")
service = IntegrationService(router=CapabilityRouter(registry), adapter=ApiMcpAdapter())

catalog = service.discover_integrations()
# catalog["integrations"] -> full provider metadata
# catalog["capability_index"] -> capability -> providers mapping
```

## Slack channel targeting

Slack can be configured to post to a specific channel in two ways:

1. Set a provider default in `config/providers.example.yaml` via `settings.default_channel`.
2. Override per request with `payload["channel"]` when sending `chat.notify`.

The adapter resolves channel target as: request payload channel -> provider default channel.

```python
from integrations.contracts import IntegrationOperation, IntegrationRequest

response = service.call(
    IntegrationRequest(
        operation=IntegrationOperation.NOTIFY,
        capability="chat.notify",
        payload={"message": "Build passed", "channel": "#release-alerts"},
        actor_id="agent:devops",
        purpose="Notify deployment status",
    )
)
```

## Next implementation steps

1. Add concrete API/MCP clients per provider under `agents/integrations/providers/`.
2. Add policy gates (approval, scope enforcement, DLP) before write actions.
3. Add auth broker for OAuth/service-account token management.
4. Add audit logging and reliability controls (retry/circuit breaker/idempotency).


## MCP tool gateway

`IntegrationService` exposes MCP tool lifecycle helpers for MCP-backed providers:

- `list_mcp_tools(provider_name=None)`
- `add_mcp_tool(provider_name, tool)`
- `configure_mcp_tool(provider_name, tool_name, config)`

This keeps MCP tool interaction behind one interface while still allowing provider-specific tool catalogs in config.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
