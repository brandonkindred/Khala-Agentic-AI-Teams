from pathlib import Path

from integrations.adapters import ApiMcpAdapter
from integrations.contracts import IntegrationOperation, IntegrationRequest
from integrations.registry import IntegrationRegistry, McpToolConfig, ProviderConfig
from integrations.router import CapabilityRouter
from integrations.service import IntegrationService


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/providers.example.yaml"

REQUIRED_PROVIDERS = {
    "figma",
    "fireflies",
    "jira",
    "trello",
    "obsidian",
    "slack",
    "google_drive",
    "dropbox",
    "google_workspace",
    "aws",
    "google_cloud",
    "digitalocean",
    "heroku",
    "zapier",
    "n8n",
    "github",
    "gitlab",
}


def test_registry_filters_enabled_providers() -> None:
    registry = IntegrationRegistry(
        [
            ProviderConfig(name="jira", transport="api", capabilities=["ticketing.create"]),
            ProviderConfig(name="trello", transport="api", capabilities=["ticketing.create"], enabled=False),
        ]
    )

    providers = registry.providers_for_capability("ticketing.create")

    assert [provider.name for provider in providers] == ["jira"]


def test_router_uses_preferred_provider_when_available() -> None:
    registry = IntegrationRegistry(
        [
            ProviderConfig(name="jira", transport="api", capabilities=["ticketing.create"]),
            ProviderConfig(name="trello", transport="api", capabilities=["ticketing.create"]),
        ]
    )
    router = CapabilityRouter(registry, preferred_providers={"ticketing.create": "trello"})

    provider = router.resolve(
        IntegrationRequest(
            operation=IntegrationOperation.CREATE,
            capability="ticketing.create",
            payload={"title": "Task"},
            actor_id="agent:test",
            purpose="create task",
        )
    )

    assert provider.name == "trello"


def test_service_returns_normalized_execution_plan() -> None:
    registry = IntegrationRegistry([ProviderConfig(name="slack", transport="mcp", capabilities=["chat.notify"])])
    router = CapabilityRouter(registry)
    service = IntegrationService(router=router, adapter=ApiMcpAdapter())

    response = service.call(
        IntegrationRequest(
            operation=IntegrationOperation.NOTIFY,
            capability="chat.notify",
            payload={"message": "hello"},
            actor_id="agent:writer",
            purpose="notify reviewer",
        )
    )

    assert response.provider == "slack"
    assert response.status == "planned"
    assert response.result["transport"] == "mcp"


def test_registry_loads_yaml_configuration() -> None:
    registry = IntegrationRegistry.from_yaml(CONFIG_PATH)

    assert registry.providers_for_capability("cloud.deploy")
    assert registry.providers_for_capability("meeting.transcript.read")


def test_registry_yaml_includes_required_enterprise_providers() -> None:
    registry = IntegrationRegistry.from_yaml(CONFIG_PATH)
    provider_names = {provider.name for provider in registry.list_enabled()}

    assert REQUIRED_PROVIDERS.issubset(provider_names)


def test_discover_integrations_returns_capability_index_and_actions() -> None:
    registry = IntegrationRegistry.from_yaml(CONFIG_PATH)
    service = IntegrationService(router=CapabilityRouter(registry), adapter=ApiMcpAdapter())

    catalog = service.discover_integrations()

    assert "integrations" in catalog
    assert "capability_index" in catalog
    assert "chat.notify" in catalog["capability_index"]
    slack = next(item for item in catalog["integrations"] if item["name"] == "slack")
    assert "chat.notify" in slack["actions"]


def test_slack_notify_uses_default_channel_from_provider_settings() -> None:
    registry = IntegrationRegistry(
        [
            ProviderConfig(
                name="slack",
                transport="mcp",
                capabilities=["chat.notify"],
                settings={"default_channel": "#agent-updates"},
            )
        ]
    )
    service = IntegrationService(router=CapabilityRouter(registry), adapter=ApiMcpAdapter())

    response = service.call(
        IntegrationRequest(
            operation=IntegrationOperation.NOTIFY,
            capability="chat.notify",
            payload={"message": "hello"},
            actor_id="agent:writer",
            purpose="notify reviewer",
        )
    )

    assert response.result["target"]["channel"] == "#agent-updates"


def test_slack_notify_prefers_payload_channel_over_default() -> None:
    registry = IntegrationRegistry(
        [
            ProviderConfig(
                name="slack",
                transport="mcp",
                capabilities=["chat.notify"],
                settings={"default_channel": "#agent-updates"},
            )
        ]
    )
    service = IntegrationService(router=CapabilityRouter(registry), adapter=ApiMcpAdapter())

    response = service.call(
        IntegrationRequest(
            operation=IntegrationOperation.NOTIFY,
            capability="chat.notify",
            payload={"message": "hello", "channel": "#release-alerts"},
            actor_id="agent:writer",
            purpose="notify reviewer",
        )
    )

    assert response.result["target"]["channel"] == "#release-alerts"


def test_discover_integrations_exposes_mcp_tools() -> None:
    registry = IntegrationRegistry.from_yaml(CONFIG_PATH)
    service = IntegrationService(router=CapabilityRouter(registry), adapter=ApiMcpAdapter())

    catalog = service.discover_integrations()

    assert "mcp_tools" in catalog
    post_message_tool = next(tool for tool in catalog["mcp_tools"] if tool["name"] == "post_message")
    assert post_message_tool["provider"] == "slack"


def test_service_can_add_and_configure_mcp_tool() -> None:
    registry = IntegrationRegistry(
        [
            ProviderConfig(
                name="slack",
                transport="mcp",
                capabilities=["chat.notify"],
                mcp_tools=[McpToolConfig(name="post_message", capabilities=["chat.notify"])],
            )
        ]
    )
    service = IntegrationService(router=CapabilityRouter(registry), adapter=ApiMcpAdapter())

    service.add_mcp_tool(
        "slack",
        McpToolConfig(name="pin_message", description="Pin a message", capabilities=["chat.pin"]),
    )
    updated_tool = service.configure_mcp_tool("slack", "pin_message", {"max_pins": 10})

    assert updated_tool.config["max_pins"] == 10
    names = {tool["name"] for tool in service.list_mcp_tools("slack")}
    assert {"post_message", "pin_message"}.issubset(names)
