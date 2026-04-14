# Enterprise Architect Agent System

Lead Orchestrator + Specialist Agents-as-Tools pattern for enterprise architecture design. Produces ADRs, diagrams, cost estimates, and the full deliverable set from product specs.

## Requirements

- Python 3.10+
- AWS credentials (for Bedrock models and optional S3 session storage)
- `pip install -r requirements.txt`

## Quick Start

```bash
cd architect-agents
pip install -r requirements.txt
python main.py path/to/spec.md
# or
echo "# My Project Spec\n\nBuild a web app..." | python main.py
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ARCHITECT_MODEL_ORCHESTRATOR` | Bedrock model for orchestrator (default: claude-opus-4-6) |
| `ARCHITECT_MODEL_SPECIALIST` | Bedrock model for specialists (default: claude-sonnet-4) |
| `ARCHITECT_MODEL_OBSERVABILITY` | Bedrock model for observability (default: claude-haiku-4-5) |
| `ARCHITECT_OUTPUT_DIR` | Output directory for deliverables |
| `ARCHITECT_SESSION_BUCKET` | S3 bucket for session persistence (optional) |
| `ARCHITECT_SESSION_DISABLED` | Set to 1 to disable session persistence |

## Architecture Expert (architecture_expert)

Architect-agents includes the **ArchitectureExpertAgent** (`architecture_expert` subpackage) used by the software engineering team for the standard planning pipeline. It uses the shared LLMClient and produces `SystemArchitecture` output compatible with `write_architecture_plan` and downstream planners.

The Enterprise Orchestrator + specialists remain the full enterprise mode for comprehensive architecture packages.

## Integration with Software Engineering Team

Set `SW_USE_ENTERPRISE_ARCHITECT=true` when running the software engineering team orchestrator. The enterprise architect will run first and its output will enrich the ArchitectureExpertAgent context.

## Bedrock AgentCore Deployment

```bash
python agentcore_main.py
# Test: curl -X POST http://localhost:8080/invocations -H "Content-Type: application/json" -d '{"spec": "..."}'
```

## Deliverables

- `architecture-overview.md`
- `adr/*.md`
- `diagrams/*.mmd`
- `technology-selections.md`
- `cost-estimate.md`
- `security-requirements.md`
- `data-architecture.md`
- `observability-plan.md`
- `open-questions.md`

## Khala platform

This package is part of the [Khala](../../../../README.md) monorepo (Unified API, Angular UI, and full team index).
