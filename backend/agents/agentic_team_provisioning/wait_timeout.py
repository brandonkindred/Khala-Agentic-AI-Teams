"""Shared WAIT-step human-input timeout bounds for the pipeline test run.

Both dispatch paths resolve the same ``AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S`` knob with
the same clamps, so the bounds live here once rather than being duplicated:

- the daemon-thread runner (``runtime/pipeline_runner.py``) reads it when the
  ``PipelineRunner`` singleton is constructed, and
- the Temporal dispatch bridge (``temporal/start_workflow.py``) reads it per dispatch (in
  the API process, outside the temporalio sandbox) to pass as a workflow argument.

Keeping one source of truth prevents the two paths from drifting apart. This module
imports only ``shared.env`` (a lightweight env parser), so it is safe to import from the
runtime module without pulling in anything heavy.
"""

from __future__ import annotations

from shared.env import parse_int

_DEFAULT_WAIT_TIMEOUT_S = 259200  # 72h — tolerates runs left overnight/weekend.
_MIN_WAIT_TIMEOUT_S = 60
# 7d upper clamp so a fat-fingered value can't recreate the original unbounded-wait bug.
_MAX_WAIT_TIMEOUT_S = 604800


def resolve_wait_timeout_s() -> int:
    """Resolve the WAIT human-input timeout from the environment, within bounds.

    Preconditions: none.
    Postconditions: returns ``AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S`` parsed as an int and
        clamped to ``[60, 604800]`` seconds; unset/garbage falls back to ``259200`` (72h).
    """
    return parse_int(
        "AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S",
        _DEFAULT_WAIT_TIMEOUT_S,
        minimum=_MIN_WAIT_TIMEOUT_S,
        maximum=_MAX_WAIT_TIMEOUT_S,
    )
