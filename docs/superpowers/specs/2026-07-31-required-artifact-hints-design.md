# Required artifact hints as a shared team constant

**Date:** 2026-07-31  
**Status:** approved design

## Problem

`REQUIRED_ARTIFACT_HINTS` is a module-level tuple of literal strings in
`ai_agent_development_team/phases/review.py`. The deterministic artifact-name
hint check is the quality gate for generated AI-agent deliverables, but the
same category set is not owned as team configuration: problem-solving only
parses free-text review issue descriptions, and intake/planning prompts do not
reference the list. As deliverable expectations evolve, those call sites can
diverge.

## Decision

Keep the current five hint strings
(`blueprint`, `evaluation`, `safety`, `runbook`, `mcp`). Do not introduce
per-run overrides, env configuration, or new deterministic planning/intake
gates in this change.

Move the tuple to a team-level `constants.py` module and have review,
problem-solving, and intake/planning system prompts all derive from it
(Approach 1: shared constant + formatted system prompts).

## Design

### Constants

Add `backend/agents/software_engineering_team/ai_agent_development_team/constants.py`:

- `REQUIRED_ARTIFACT_HINTS: tuple[str, ...] = ("blueprint", "evaluation", "safety", "runbook", "mcp")`
- Public name (no leading underscore); values unchanged

### Review

In `phases/review.py`:

- Import `REQUIRED_ARTIFACT_HINTS` from `..constants`
- Delete the local module-level tuple
- Gate logic unchanged: one `high` `artifact_gate` issue per missing hint
  substring in joined file paths

### Problem-solving

In `phases/problem_solving.py`:

- Import `REQUIRED_ARTIFACT_HINTS` from `..constants`
- Keep extracting the category token from `artifact_gate` issue descriptions
  (text after the final `:`)
- Only synthesize a placeholder when that token is in `REQUIRED_ARTIFACT_HINTS`
- Placeholder path remains `ai_system/{token}_placeholder.md`
- Unknown-token `artifact_gate` issues produce no placeholder (no resolve credit)

### Prompts (intake / planning)

In `prompts.py`:

- Replace static `INTAKE_PROMPT` / `PLANNING_PROMPT` module strings with
  `intake_system_prompt()` / `planning_system_prompt()` builders that inject a
  joined hint list into the system prompt from `REQUIRED_ARTIFACT_HINTS`
- Wording (or equivalent): required artifact path categories that must each
  appear in at least one generated artifact filename later, listing the
  constant values joined by `", "`
- Keep existing JSON response shapes and specialist role text
- Leave `DELIVER_PROMPT` unchanged

In `phases/intake.py` and `phases/planning.py`:

- Call the builders when passing `system_prompt=` to the LLM helper

### Out of scope

- Changing the five hint string values
- Deterministic post-planning / post-intake coverage gates
- Per-run overrides / `TeamContext` / env-var configuration
- Deliver-phase prompt changes
- Broader prompt redesign beyond injecting the hint list

## Testing

- Existing `test_ai_agent_development_team.py` workflow tests remain the
  primary regression net (outcomes unchanged for the current hint set)
- Add a focused unit assertion that `REQUIRED_ARTIFACT_HINTS` equals the
  five current strings and that intake/planning system prompts contain each
  hint
- Cover one unknown-token `artifact_gate` issue that does not create a
  placeholder under the new problem-solving guard

## Success criteria

- One definition of the hint tuple under `constants.py`
- Review, problem-solving, and intake/planning prompts all derive from it
- No local duplicate literals of the five-string set in those modules
- Runtime review / problem-solving outcomes unchanged for the current hint set
- No new planning/intake hard gates
