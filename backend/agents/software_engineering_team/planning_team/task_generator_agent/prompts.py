"""Prompts for the Task Generator agent."""

TASK_GENERATOR_CONTEXT_NOTE = """**IMPORTANT – You are using pre-analyzed spec data:**
The full spec has been analyzed in chunks and merged. Below you will find:
1. **DEEP SPEC ANALYSIS** – A consolidated JSON analysis of every requirement, entity, endpoint, screen, flow, etc. from the spec. Use this as your primary source of truth.
2. **Truncated spec** – The first portion of the initial_spec.md for reference. The analysis above is comprehensive; you do NOT need the full spec.
3. **Codebase analysis** – What already exists in the repo.
4. **Existing code** – A sample of the current codebase.

Generate your task plan from the DEEP SPEC ANALYSIS and truncated spec. Ensure every item in the analysis is covered by at least one task.

"""
