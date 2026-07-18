"""Module-level constants for the blog writing pipeline."""

import os
from pathlib import Path

# Three .parent hops from pipeline/constants.py: pipeline/ -> agent_implementations/
# -> agents/blogging/ -- must land on agents/blogging/docs, matching where the
# monolith's own two-hop path (agent_implementations/blog_writing_process_v2.py ->
# agent_implementations/ -> agents/blogging/) resolved before the split.
_blogging_docs = Path(__file__).resolve().parent.parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"
# Hard upper bound on the draft/copy-edit loop iterations (the `for iteration in
# range(1, draft_editor_iterations + 1)` cap in run_draft_stage). The loop normally
# exits *early* when the copy editor approves the draft, or escalates to the author
# every COPY_EDIT_ESCALATION_THRESHOLD iterations — 30 is a runaway-safety ceiling
# (3x the escalation threshold), not an expected iteration count.
DRAFT_EDITOR_ITERATIONS = 30
MAX_REWRITE_ITERATIONS = 10
# After this many copy-edit revisions without editor approval, escalate to the user
COPY_EDIT_ESCALATION_THRESHOLD = 10

# Poll cadence (seconds) for every human-in-the-loop wait loop (draft feedback,
# uncertainty answers, title selection). One value keeps the loops consistent and
# configurable in one place. This is independent of the Temporal activity heartbeat:
# ``start_pipeline_heartbeat`` runs a background thread that heartbeats on its own
# schedule, so these blocking sleeps never risk a heartbeat timeout.
HITL_POLL_INTERVAL_S = int(os.getenv("BLOGGING_HITL_POLL_INTERVAL_S", "10"))

# A human-in-the-loop wait can poll the job store for up to ~1h; a single transient
# job-store read blip should not fail the whole job. Tolerate this many CONSECUTIVE
# read failures (sleeping a poll interval between each) before giving up and letting the
# error propagate — a persistent outage still surfaces, a momentary one is ridden out.
HITL_MAX_CONSECUTIVE_READ_ERRORS = 5
