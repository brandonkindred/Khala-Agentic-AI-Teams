"""Temporal task queue, workflow IDs, and activity/workflow names for the AI systems team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_AI_SYSTEMS", "ai-systems").strip()
WORKFLOW_ID_PREFIX_BUILD = "ai-systems-build-"

WORKFLOW_BUILD = "AISystemsBuildWorkflow"

# Per-phase activity names — the build pipeline is decomposed into one
# ``@activity.defn`` per orchestrator phase (plus begin/finalize book-ends), so
# every phase runs, retries, and shows up in the Temporal UI as its own span.
ACTIVITY_BEGIN = "ai_systems_begin_run"
ACTIVITY_SPEC_INTAKE = "ai_systems_spec_intake"
ACTIVITY_ARCHITECTURE = "ai_systems_architecture"
ACTIVITY_CAPABILITIES = "ai_systems_capabilities"
ACTIVITY_EVALUATION = "ai_systems_evaluation"
ACTIVITY_SAFETY = "ai_systems_safety"
ACTIVITY_BUILD_PHASE = "ai_systems_build_phase"
ACTIVITY_FINALIZE = "ai_systems_finalize"

# Legacy whole-pipeline activity, still registered so workflow histories recorded
# before the per-phase decomposition can drain out (see the workflow's unpatched
# replay branch).
ACTIVITY_BUILD = "run_ai_systems_build"
