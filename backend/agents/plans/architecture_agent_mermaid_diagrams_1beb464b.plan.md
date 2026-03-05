---
name: Architecture agent Mermaid diagrams
overview: Extend the Architecture Expert agent so it consistently produces a defined set of Mermaid diagrams (frontend/backend structure, client-server, infrastructure, security, and multi-cloud options) from the spec, and ensure they are written to the development plan with proper Mermaid code fences for rendering.
todos: []
isProject: false
---

# Architecture Agent: Variety of Mermaid Diagrams from Spec

## Current behavior

- **Architecture agent** ([software_engineering_team/architecture_agent/agent.py](software_engineering_team/architecture_agent/agent.py)) takes parsed product requirements (title, description, acceptance criteria, constraints) and optional technology preferences. It calls the LLM with [ARCHITECTURE_PROMPT](software_engineering_team/architecture_agent/prompts.py) and builds a `SystemArchitecture` from the JSON response.
- **Diagrams** are already part of the contract: `SystemArchitecture.diagrams` is a `Dict[str, str]` and the prompt asks for a `"diagrams"` object with "diagram names as keys and description/mermaid as values" — but the prompt does not enumerate which diagrams to produce or require Mermaid.
- **Writing** ([software_engineering_team/shared/development_plan_writer.py](software_engineering_team/shared/development_plan_writer.py)) writes each diagram under `## Diagrams` as `### {name}` plus raw `content` — **no** ````mermaid` fence — so Mermaid blocks would not render in Markdown viewers.

## Target behavior

- The architect produces a **defined set of diagrams**, all as **valid Mermaid** (no prose “descriptions” for these).
- Diagrams are written so that Markdown renderers (e.g. GitHub) display them (wrap in ````mermaid` ... `````).

## 1. Define required and optional diagram set

**Required (always produced):**


| Key                          | Purpose                                                              |
| ---------------------------- | -------------------------------------------------------------------- |
| `client_server_architecture` | Client–server view for this project (browsers, app server(s), APIs). |
| `frontend_code_structure`    | Front-end code layout (modules, layers, key directories).            |
| `backend_code_structure`     | Backend code layout (packages, layers, entrypoints).                 |
| `backend_infrastructure`     | Backend infra (servers, queues, DBs, caches).                        |
| `infrastructure`             | Overall infrastructure (hosting, networking, CI/CD).                 |
| `security_architecture`      | Security boundaries, auth flow, data protection.                     |


**Optional (produce when relevant or as “suggested deployment”):**

- `backend_code_architecture` — logical/component view of backend (if different from code structure).
- `cloud_aws`, `cloud_gcp`, `cloud_digital_ocean` — deployment view for each provider (one or more; can be high-level “suggested” even if spec doesn’t mandate a cloud).

The agent can also add extra keys (e.g. `data_flow`, `sequence_auth`) for “anything else that might be helpful.”

## 2. Prompt changes ([software_engineering_team/architecture_agent/prompts.py](software_engineering_team/architecture_agent/prompts.py))

- State that **all diagram values must be valid Mermaid only** (no explanatory text inside the value); optional note: “Do not wrap in markdown code fences; output raw Mermaid.”
- Enumerate the **required** diagram keys and what each should show (one short line each).
- Enumerate **optional** diagram keys (backend_code_architecture, cloud_aws, cloud_gcp, cloud_digital_ocean) and when to include them.
- In the **output format** section, document the `diagrams` object with these keys and “Mermaid source code (no code fences)”.
- Keep the rest of the prompt (overview, components, architecture_document, decisions) unchanged.

## 3. Development plan writer ([software_engineering_team/shared/development_plan_writer.py](software_engineering_team/shared/development_plan_writer.py))

- When writing each entry in `architecture.diagrams`:
  - Normalize content: if the string is already wrapped in `mermaid` ... ````` (or  `mermaid`), strip the fence and any leading/trailing newlines.
  - Wrap the resulting Mermaid block in a single ````mermaid`... ````` block and write that under`### {name}` so viewers render it.

This keeps the file valid Markdown and ensures one Mermaid block per diagram.

## 4. Tests and mocks

- **DummyLLMClient** ([software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)): Extend the architecture stub to include a minimal `diagrams` dict (e.g. one or two keys with trivial Mermaid like `graph LR\n  A-->B`) so `write_architecture_plan` and any test that asserts on the written file can rely on diagrams being present.
- **Tests**: Add or adjust a test that verifies the architecture plan file contains a `## Diagrams` section and at least one ````mermaid`block when the agent returns diagrams (e.g. in [software_engineering_team/tests/test_architecture_agent.py](software_engineering_team/tests/test_architecture_agent.py) or a test that runs`write_architecture_plan`with a`SystemArchitecture`that has`diagrams`). Optionally assert that required diagram keys appear when using a mock that returns them.

## 5. No schema or API change

- `SystemArchitecture.diagrams` remains `Dict[str, str]`. The LLM is guided by the prompt to populate the agreed keys; no Pydantic enum or extra model is required.
- Orchestrator and other consumers of `architecture` do not need changes; they already receive `architecture.diagrams` and the written plan path is unchanged.

## Summary of files to touch


| File                                                                                                                                                                | Change                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| [software_engineering_team/architecture_agent/prompts.py](software_engineering_team/architecture_agent/prompts.py)                                                  | Require Mermaid-only diagrams; list required and optional diagram keys and their intent.       |
| [software_engineering_team/shared/development_plan_writer.py](software_engineering_team/shared/development_plan_writer.py)                                          | Normalize and wrap each diagram value in ````mermaid` ... ````` when writing.                  |
| [software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)                                                                                  | Add minimal `diagrams` (and optionally `decisions`) to DummyLLMClient architecture response.   |
| [software_engineering_team/tests/test_architecture_agent.py](software_engineering_team/tests/test_architecture_agent.py) and/or tests for `development_plan_writer` | Ensure diagrams are present in stub; add test that written plan contains a Mermaid code block. |


## Optional follow-ups

- If the LLM often omits optional diagrams, add a short post-processing step in the agent that injects placeholder Mermaid for missing optional keys (e.g. “TBD: deploy to AWS”) so the document structure is consistent.
- If token limits become an issue, consider splitting “architecture document” and “diagrams” into two LLM calls (document first, then diagrams from document + components).

