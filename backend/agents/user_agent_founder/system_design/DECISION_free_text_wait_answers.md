# Decision — free-text WAIT answer quality for persona-driven agentic teams

**Status:** Accepted · **Scope:** `user_agent_founder` persona answering + `AgenticTeamAdapter`

## Context

A testing persona (the "founder") can autonomously drive any assembled agentic team through
`AgenticTeamAdapter` (`targets/agentic_team.py`). The founder was built for a three-phase target
(spec → analysis → build) that asks **batched multiple-choice** questions. An agentic-team test
pipeline is shaped differently: a single linear DAG that occasionally pauses on a **`WAIT` step**
whose prompt is **open-ended free text** (the step's `description`, presented as `human_prompt`).

The adapter collapses the two shapes: each `waiting_for_input` WAIT step is wrapped as a **single
question with empty `options`**, which forces `FounderAgent.answer_question` down the free-text
(`selected_option_id == "other"`) path — the only field the pipeline acts on is `other_text`.

## Risk assessed

The persona's answering machinery is built for **option selection**. `QUESTION_ANSWERING_PROMPT`
frames the task as *"Choose the option that best fits your values… If none of the options are
right, provide a custom answer."* Reusing it for open-ended WAIT prompts is weak: it never tells
the persona to author a decisive, self-contained answer, and it never signals that the answer is
consumed by an **automated pipeline with no human in the loop** to clarify or ask a follow-up.
The `context` handed in was also thin (`"Pipeline run X, step Y."`). Left unchanged, free-text
answer quality was unproven — and low-quality answers degrade the core value of persona-driven
team tests (the persona drives the team end-to-end).

## Decision

**Prompt changes are warranted before GA.** We tune the free-text path rather than generalize the
founder Protocol (the collapsing adapter stays — see the adapter module docstring):

1. **Dedicated free-text prompt** — `FREE_TEXT_ANSWERING_PROMPT` (`agent.py`) replaces the
   multiple-choice template when a question has no options. It instructs the persona to write a
   **decisive, specific, self-contained, actionable** answer in the founder's budget → speed → UX
   voice, and states explicitly that the answer is fed **directly back into an automated pipeline**
   (no human clarification loop). `answer_question` branches on `if options:` — options present →
   `QUESTION_ANSWERING_PROMPT`; no options → `FREE_TEXT_ANSWERING_PROMPT`. The bounded schema
   (`_build_answer_schema`) is unchanged, so `other_text` remains required and validated.
2. **Richer adapter context** — `poll_build` now grounds the WAIT question with a short context
   naming the open-ended nature, the originating step, and the no-follow-up resume, instead of a
   bare "run X, step Y". This flows verbatim into the prompt's `{context}`.

Return shape, orchestrator glue, `submit_build_answers`, and the contract-drift tripwire are all
unaffected — this is a prompt/wording change, not a contract change.

## Empirical read — how to reproduce

CI mocks the LLM, so a live-answer test cannot run there (it would be skipped and would count as
uncovered code against the 90% floor while adding no signal). The empirical read is a **manual
procedure** against a live provider:

1. Configure an LLM provider (see `docs/ENV_VARS.md` / the LLM Provider settings UI) so
   `FounderAgent` resolves a real client.
2. For each prompt in `REPRESENTATIVE_WAIT_PROMPTS` below, call:

   ```python
   from user_agent_founder.agent import FounderAgent
   founder = FounderAgent()  # built-in founder persona
   ans = founder.answer_question({
       "id": "wait-eval",
       "question_text": prompt,
       "context": "Open-ended request from an automated agentic-team pipeline; "
                  "your reply resumes the run with no follow-up.",
       "options": [],
   })
   print(prompt, "→", ans["other_text"], "|", ans["rationale"])
   ```

3. Judge each `other_text` for: **decisiveness** (one committed choice, no hedging/return
   questions), **specificity/self-containment** (concrete details, actionable without prior
   conversation), and **voice** (budget → speed → UX applied). A GA-ready read is: the free-text
   prompt yields materially more decisive, self-contained answers than the multiple-choice
   template did on the same prompts.

Representative WAIT prompts (`REPRESENTATIVE_WAIT_PROMPTS`):

- "Which tone should the launch blog post take?"
- "The design exceeds the sprint budget. What scope do you want to cut?"
- "What should we name the primary call-to-action button?"
- "Write the one-line value proposition for the landing page hero."
- "Ready to publish, or hold for another review pass? If hold, what must change first?"

Alternatively, exercise the full path end-to-end (Agent Studio Stage 4): launch a persona test
against `agentic_team:<id>` with a process containing WAIT steps and inspect the audit panel's
recorded decisions.
