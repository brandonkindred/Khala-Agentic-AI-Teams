"""Release notes agent — composes the markdown body for a sprint release.

Lives in the ``technical_writers`` package. The ReleaseManagerAgent (in ``product_delivery``) calls
``ReleaseNotesAgent.run`` to turn a structured set of shipped stories +
collected Integration / DevOps / QA failures into the prose that lands
in ``plan/releases/<version>.md``.
"""

from software_engineering_team.technical_writers.release_notes_agent.agent import (
    ReleaseNotesAgent,
    build_fallback_release_notes,
)
from software_engineering_team.technical_writers.release_notes_agent.models import (
    ReleaseFailure,
    ReleaseNotesInput,
    ReleaseNotesOutput,
    ReleaseStorySummary,
)

__all__ = [
    "ReleaseFailure",
    "ReleaseNotesAgent",
    "ReleaseNotesInput",
    "ReleaseNotesOutput",
    "ReleaseStorySummary",
    "build_fallback_release_notes",
]
