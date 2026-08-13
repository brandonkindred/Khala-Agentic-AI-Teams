"""Agent Studio backend — Stage 1 "build an agent" touchpoints.

This package implements the §5 Stage-1 backend the Agent Studio UX depends on
(``docs/design/agent-studio-ux-spec.md``):

* a **per-agent authoring assistant** (:mod:`agent_team_studio.agent_studio.assistant`) that
  co-authors a single :class:`~agent_team_studio.agent_studio.models.AgentDefinition` via LLM chat,
  modeled on ``agentic_team_provisioning``'s ``ProcessDesignerAgent``;
* **clone-from-registry** — projecting an existing registry manifest into an
  editable draft (the source manifest is never mutated);
* **save + register** — turning a finished definition into a live, invokable
  ``agent_platform.registry`` manifest, reusing the generated-agent runtime so a saved
  Studio agent is invokable exactly like a generated team agent.

Conversation state is held in-process (:mod:`agent_team_studio.agent_studio.store`); durable
cross-process persistence is a tracked follow-up, mirroring the same caveat the
generated-agent registration already carries.
"""
