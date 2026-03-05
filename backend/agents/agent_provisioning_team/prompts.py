"""
Prompt templates for the Agent Provisioning Team.

Used primarily for documentation generation.
"""

ONBOARDING_SUMMARY_PROMPT = """Generate a concise onboarding summary for an AI agent.

Agent ID: {agent_id}
Access Tier: {access_tier}
Tools Provisioned: {tool_names}

The summary should:
1. Welcome the agent to the environment
2. List the available tools briefly
3. Mention the access tier and its implications
4. Point to key environment variables

Keep the summary under 3 sentences.
"""

TOOL_GETTING_STARTED_PROMPT = """Generate getting-started documentation for a tool.

Tool: {tool_name}
Description: {description}
Connection Details: {connection_details}
Permissions: {permissions}

Provide clear, actionable steps for an AI agent to start using this tool.
Include any environment variables they should use.
Keep it concise and technical.
"""

ENVIRONMENT_OVERVIEW_PROMPT = """Generate an environment overview document.

Container: {container_name}
Workspace: {workspace_path}
Tools Available:
{tools_list}

Environment Variables:
{env_vars}

Create a brief technical overview suitable for an AI agent to understand
their execution environment and available resources.
"""
