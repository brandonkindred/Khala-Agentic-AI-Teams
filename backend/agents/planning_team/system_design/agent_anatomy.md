# Agent Anatomy — Input → Agent → Output

Each planning phase's logic lives in an anatomy-conformant persona-agent package under
`agents/<phase>/` (`AGENT_ANATOMY.md` §1 typed I/O, §2 coordinator, §3 tools, §5 prompt
split, §6 code-level guardrails). The matching `phases/<phase>.py::run_*` is a thin adapter
that translates the workflow `context` dict to the agent's typed **Input**, invokes the
agent (injecting its declared **tools**), and maps the typed **Output** back to the
`(context_update, artifacts)` tuple that `orchestrator.run_workflow` and the Temporal
activities consume.

## Coordinator + adapter seam

```mermaid
flowchart LR
    subgraph Drivers
        ORC[orchestrator.run_workflow<br/>§2 coordinator]
        TMP[temporal/activities.py<br/>one activity per phase]
    end
    ORC -->|context dict| ADP
    TMP -->|context dict| ADP
    ADP[phases/&lt;phase&gt;.py::run_*<br/>thin adapter] -->|typed Input| AG[agents/&lt;phase&gt;<br/>stateless agent]
    AG -->|typed Output| ADP
    ADP -->|context_update, artifacts| ORC
    ADP -->|context_update, artifacts| TMP
    TOOLS[[declared tools §3<br/>llm / run_pra / wait_pra / …]] -.injected.-> AG
```

## Per-phase Input → Agent → Output

```mermaid
flowchart LR
    subgraph intake [Intake · deterministic]
        i_in["IntakeInput<br/>repo_path, client_name,<br/>initial_brief, spec_content,<br/>existing_artifacts"] --> i_ag[IntakeAgent]
        i_ag --> i_out["IntakeOutput<br/>client_context, repo_path,<br/>initial_brief, spec_content"]
    end
```

```mermaid
flowchart LR
    subgraph discovery [Discovery · LLM]
        d_in["DiscoveryInput<br/>client_context,<br/>initial_brief, spec_content"] --> d_ag[DiscoveryAgent]
        d_llm[[llm tool]] -. injected .-> d_ag
        d_ag --> d_out["DiscoveryOutput<br/>client_context (ClientContext),<br/>discovery (dict)"]
    end
```

```mermaid
flowchart LR
    subgraph requirements [Requirements · LLM]
        r_in["RequirementsInput<br/>client_context,<br/>initial_brief, spec_content"] --> r_ag[RequirementsAgent]
        r_llm[[llm tool]] -. injected .-> r_ag
        r_ag --> r_out["RequirementsOutput<br/>open_questions:<br/>list[OpenQuestion]"]
    end
```

```mermaid
flowchart LR
    subgraph synthesis [Synthesis · deterministic]
        s_in["SynthesisInput<br/>client_context,<br/>market_research_evidence"] --> s_ag[SynthesisAgent]
        s_ag --> s_out["SynthesisOutput<br/>evidence, evidence_attached,<br/>client_context"]
    end
```

```mermaid
flowchart LR
    subgraph docprod [Document Production]
        p_in["DocumentProductionInput<br/>repo_path, client_context,<br/>spec_content, initial_brief,<br/>use_product_analysis"] --> p_ag[DocumentProductionAgent]
        p_tools[[run_pra, wait_pra,<br/>answer_callback,<br/>run_architecture_fn]] -. injected .-> p_ag
        p_ag --> p_out["DocumentProductionOutput<br/>handoff_package (HandoffPackage),<br/>artifacts (dict)"]
    end
```

```mermaid
flowchart LR
    subgraph subagent [Sub-agent Provisioning]
        b_in["SubAgentProvisioningInput<br/>repo_path, capability_gap"] --> b_ag[SubAgentProvisioningAgent]
        b_tools[[start_build_fn,<br/>wait_build_fn]] -. injected .-> b_ag
        b_ag --> b_out["SubAgentProvisioningOutput<br/>sub_agent_blueprint, error"]
    end
```

## Prompt split (§5) — discovery & requirements

The LLM runtime (`LLMClient.complete_text`) accepts a single prompt string and has no
`system_prompt` parameter, so the System/User split is documented in code and re-joined into
a byte-identical single string before the call.

```mermaid
flowchart LR
    SYS["SYSTEM_PROMPT<br/>(identity + constraints)"] --> JOIN
    USR["build_user_prompt(input_text)<br/>(payload + JSON shape)"] --> JOIN
    JOIN["build_prompt(x)<br/>joins System + User with a<br/>blank-line separator"] --> CT["llm.complete_text(prompt, ...)"]
    GUARD{{"tests/test_prompts.py:<br/>byte-identical to the<br/>pre-split literal"}} -. pins .-> JOIN
```
