"""
Documentation phase: Generate onboarding packet for the agent.

This is phase 5 of the provisioning workflow.
"""

from typing import Callable, Dict, List, Optional

from ..anatomy_assets import try_materialize_anatomy_bundle
from ..models import (
    AccessTier,
    DocumentationResult,
    GeneratedCredentials,
    OnboardingPacket,
    ToolOnboardingInfo,
    ToolProvisionResult,
)
from ..prompts import (
    format_onboarding_summary_prompt,
    format_tool_getting_started_prompt,
)
from ..shared.llm_client import LLMClient, LLMRequest, sanitize_prompt_var
from ..shared.tool_manifest import ToolManifest

# Module-level shared client; cheap to construct, no network until is_configured.
_LLM = LLMClient()

_SUMMARY_SYSTEM = (
    "You are the onboarding writer for the Strands Agent Provisioning Team. "
    "Produce concise, technical onboarding text that complies with the canonical agent anatomy."
)
_TOOL_DOC_SYSTEM = (
    "You are the tool documentation writer. Write a short, accurate getting-started "
    "blurb for an AI agent that just received credentials for the named tool."
)


def run_documentation(
    agent_id: str,
    manifest: ToolManifest,
    credentials: Dict[str, GeneratedCredentials],
    tool_results: List[ToolProvisionResult],
    access_tier: AccessTier,
    workspace_path: str = "/workspace",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> DocumentationResult:
    """
    Execute the documentation phase.

    Generates a comprehensive onboarding packet with tool documentation,
    credentials info, and getting-started guides.

    Args:
        agent_id: Unique identifier for the agent
        manifest: Loaded tool manifest
        credentials: Generated credentials per tool
        tool_results: Results from account provisioning
        access_tier: Access tier
        workspace_path: Path to the workspace
        progress_callback: Callback for progress updates

    Returns:
        DocumentationResult with onboarding packet
    """
    if progress_callback:
        progress_callback("Generating onboarding documentation...")

    tool_docs: List[ToolOnboardingInfo] = []
    env_vars: Dict[str, str] = {}

    successful_tools = [r for r in tool_results if r.success]

    for result in successful_tools:
        tool_name = result.tool_name
        tool_def = manifest.get_tool(tool_name)

        if tool_def is None:
            continue

        onboarding_config = tool_def.onboarding
        creds = credentials.get(tool_name)

        description = onboarding_config.description or f"{tool_name} tool"
        getting_started = _generate_getting_started(
            tool_name=tool_name,
            onboarding_config=onboarding_config,
            credentials=creds,
            result=result,
        )

        env_var = onboarding_config.env_var
        if env_var and creds and creds.connection_string:
            env_vars[env_var] = creds.connection_string

        tool_docs.append(
            ToolOnboardingInfo(
                name=tool_name,
                description=description,
                env_var=env_var,
                getting_started=getting_started,
                permissions=result.permissions,
            )
        )

    env_vars["WORKSPACE"] = workspace_path
    env_vars["AGENT_ID"] = agent_id

    anatomy_bundle_path = try_materialize_anatomy_bundle(workspace_path)

    summary = _generate_summary(
        agent_id=agent_id,
        tool_count=len(successful_tools),
        access_tier=access_tier,
        tool_names=[r.tool_name for r in successful_tools],
    )

    onboarding = OnboardingPacket(
        summary=summary,
        tools=tool_docs,
        access_tier=access_tier.value,
        environment_variables=env_vars,
        anatomy_bundle_path=anatomy_bundle_path,
    )

    if progress_callback:
        progress_callback("Documentation complete")

    return DocumentationResult(
        success=True,
        onboarding=onboarding,
    )


def _generate_summary(
    agent_id: str,
    tool_count: int,
    access_tier: AccessTier,
    tool_names: Optional[List[str]] = None,
) -> str:
    """Generate the onboarding summary text.

    Calls the LLM client when configured; otherwise returns a deterministic
    template fallback so the pipeline keeps working until the LLM service
    integration lands this week.
    """
    tier_descriptions = {
        AccessTier.MINIMAL: "read-only access to resources",
        AccessTier.STANDARD: "read/write access to your own resources",
        AccessTier.ELEVATED: "administrative access to your own resources",
        AccessTier.FULL: "full administrative access",
    }
    tier_desc = tier_descriptions.get(access_tier, "standard access")

    if _LLM.is_configured:
        prompt = format_onboarding_summary_prompt(
            agent_id=sanitize_prompt_var(agent_id),
            access_tier=sanitize_prompt_var(access_tier.value),
            tool_names=sanitize_prompt_var(", ".join(tool_names or [])),
        )
        try:
            return _LLM.complete(
                LLMRequest(system=_SUMMARY_SYSTEM, user=prompt, max_tokens=300)
            ).strip()
        except Exception:  # noqa: BLE001 — fall through to deterministic template
            pass

    return (
        f"Your agent environment is ready with {tool_count} tool(s) configured. "
        f"You have {tier_desc}. "
        f"Use the environment variables listed below to connect to your tools."
    )


def _generate_getting_started(
    tool_name: str,
    onboarding_config,
    credentials: Optional[GeneratedCredentials],
    result: ToolProvisionResult,
) -> str:
    """Generate getting-started text for a tool."""
    if onboarding_config.getting_started:
        text = onboarding_config.getting_started

        if credentials:
            if credentials.username:
                text = text.replace("{username}", credentials.username)
            if credentials.connection_string:
                text = text.replace("{connection_string}", credentials.connection_string)
            for key, value in credentials.extra.items():
                text = text.replace(f"{{{key}}}", str(value))

        return text

    if _LLM.is_configured:
        prompt = format_tool_getting_started_prompt(
            tool_name=sanitize_prompt_var(tool_name),
            description=sanitize_prompt_var(getattr(onboarding_config, "description", "") or ""),
            connection_details=sanitize_prompt_var(
                "available via env var" if credentials and credentials.connection_string else "n/a"
            ),
            permissions=sanitize_prompt_var(", ".join(result.permissions or [])),
        )
        try:
            return _LLM.complete(
                LLMRequest(system=_TOOL_DOC_SYSTEM, user=prompt, max_tokens=400)
            ).strip()
        except Exception:  # noqa: BLE001
            pass

    lines = [f"To use {tool_name}:"]

    if credentials and credentials.connection_string:
        env_var = onboarding_config.env_var or f"{tool_name.upper()}_URL"
        lines.append(f"- Connection string available in ${env_var}")

    if result.permissions:
        lines.append(f"- Permissions: {', '.join(result.permissions)}")

    return "\n".join(lines)


def generate_readme(onboarding: OnboardingPacket) -> str:
    """
    Generate a README.md content from the onboarding packet.

    Args:
        onboarding: The onboarding packet

    Returns:
        Markdown-formatted README content
    """
    lines = [
        "# Agent Workspace",
        "",
        "## Overview",
        "",
        onboarding.summary,
        "",
        f"**Access Tier:** {onboarding.access_tier}",
        "",
        "## Standard AI agent anatomy",
        "",
        "Every AI agent in Strands must follow the canonical **Agent Provisioning** anatomy: "
        "Input/Output, Agent core, Tools, tiered Memory, Prompt roles (System/User/Assistant), "
        "Security Guardrails, and Subagents with recursive INPUT/OUTPUT.",
        "",
    ]
    if onboarding.anatomy_bundle_path:
        lines.extend(
            [
                f"On this workspace, the specification and diagrams were copied to: "
                f"`{onboarding.anatomy_bundle_path}/`",
                "",
                "- `AGENT_ANATOMY.md` — full checklist",
                "- `*.png` — reference diagrams (high-level, detailed, subagents, chaining)",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "When the workspace path is available on the provisioning host, "
                "`docs/agent_anatomy/` is populated with `AGENT_ANATOMY.md` and `design_assets/*.png`. "
                "In the repository they live under `backend/agents/agent_provisioning_team/`.",
                "",
            ]
        )
    lines.extend(
        [
            "## Available Tools",
            "",
        ]
    )

    for tool in onboarding.tools:
        lines.append(f"### {tool.name}")
        lines.append("")
        lines.append(tool.description)
        lines.append("")

        if tool.env_var:
            lines.append(f"**Environment Variable:** `{tool.env_var}`")
            lines.append("")

        if tool.permissions:
            lines.append(f"**Permissions:** {', '.join(tool.permissions)}")
            lines.append("")

        lines.append("**Getting Started:**")
        lines.append("")
        lines.append(tool.getting_started)
        lines.append("")

    lines.append("## Environment Variables")
    lines.append("")
    lines.append("```bash")
    for var, value in onboarding.environment_variables.items():
        display_value = value if "password" not in var.lower() else "***"
        lines.append(f'export {var}="{display_value}"')
    lines.append("```")

    return "\n".join(lines)
