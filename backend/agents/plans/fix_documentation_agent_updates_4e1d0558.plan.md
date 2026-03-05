---
name: Fix documentation agent updates
overview: "Ensure documentation (README.md and CONTRIBUTORS.md) is populated with actual content by fixing the documentation workflow to: (1) always update documentation on first task completion in each repo, (2) force a comprehensive documentation review after all tasks complete for each repository."
todos:
  - id: force-empty-readme-update
    content: Modify Tech Lead's trigger_documentation_update to force update when README is empty or < 100 chars
    status: completed
  - id: final-doc-review
    content: Add unconditional final documentation review in orchestrator after all tasks complete
    status: completed
  - id: doc-agent-final-mode
    content: Add run_final_review method to DocumentationAgent for comprehensive end-of-project documentation
    status: completed
  - id: improve-logging
    content: Add detailed logging to track when/why documentation updates are skipped or triggered
    status: completed
isProject: false
---

# Fix Documentation Agent Updates

## Problem Summary

README.md and CONTRIBUTORS.md files are being created as empty placeholders during repo initialization but never populated with content. The documentation agent is either not being triggered or the LLM is declining to update.

## Root Causes

1. **Empty files created at init**: `initialize_new_repo()` in [shared/git_utils.py](software_engineering_team/shared/git_utils.py) creates empty README.md and CONTRIBUTORS.md (lines 268-271)
2. **Conditional documentation triggers**: The Tech Lead asks an LLM if docs need updating, which can return `false`:

```717:786:software_engineering_team/tech_lead_agent/agent.py
# Tech Lead decides if docs need updating via LLM call
should_update = bool(data.get("should_update_docs", False))
```

1. **Final pass only runs for empty files**: The final documentation pass in [orchestrator.py](software_engineering_team/orchestrator.py) lines 1931-1968 only runs if README is missing/empty, but doesn't do a comprehensive review
2. **No documentation review after all tasks complete**: There's no explicit "review and finalize documentation" phase

## Implementation Plan

### 1. Force documentation update when README is empty or minimal

In [tech_lead_agent/agent.py](software_engineering_team/tech_lead_agent/agent.py), strengthen the `force_docs_because_readme_empty` logic to:

- Force update if README content is less than 100 characters
- Force update on first backend/frontend task completion in that repo

### 2. Add comprehensive final documentation review

In [orchestrator.py](software_engineering_team/orchestrator.py), modify the final documentation pass (around line 1931) to:

- Always run documentation agent for each repo at the end, regardless of README content
- Pass a flag to indicate this is a "final comprehensive review"
- Include a full codebase summary for proper documentation generation

### 3. Add explicit documentation review method to DocumentationAgent

In [documentation_agent/agent.py](software_engineering_team/documentation_agent/agent.py), add a `run_final_review()` method that:

- Reviews the entire repository structure
- Generates comprehensive README with all sections
- Updates CONTRIBUTORS.md with all agents that contributed

### 4. Improve logging for documentation operations

Add more visible logging when:

- Documentation update is skipped and why
- Documentation agent generates content
- Files are written or skipped

## Files to Modify

- [software_engineering_team/tech_lead_agent/agent.py](software_engineering_team/tech_lead_agent/agent.py) - Force docs update when README is minimal
- [software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py) - Add unconditional final documentation pass
- [software_engineering_team/documentation_agent/agent.py](software_engineering_team/documentation_agent/agent.py) - Add final review mode
- [software_engineering_team/documentation_agent/prompts.py](software_engineering_team/documentation_agent/prompts.py) - Add final review prompt emphasizing completeness

