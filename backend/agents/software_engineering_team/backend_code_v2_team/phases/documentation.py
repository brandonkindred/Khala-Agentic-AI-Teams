"""
Documentation phase: review all documentation and iterate until issues are resolved.

This phase runs after Execution completes and before Deliver.
It performs a comprehensive documentation review and fix cycle.

The review/fix loop is shared across the code-v2 teams; see
``shared/phases/documentation.py``. This module re-exports the shared
``_write_files`` patch surface and binds this team's models via
``make_run_documentation_phase``.
"""

from __future__ import annotations

from software_engineering_team.shared.phases.documentation import (  # noqa: F401
    MAX_DOCUMENTATION_ITERATIONS,
    _write_files,
    make_run_documentation_phase,
)

from .. import models as _models

__all__ = ["MAX_DOCUMENTATION_ITERATIONS", "run_documentation_phase", "_write_files"]

run_documentation_phase = make_run_documentation_phase(models=_models)
