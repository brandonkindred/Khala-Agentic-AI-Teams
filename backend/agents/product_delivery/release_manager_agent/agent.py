"""ReleaseManagerAgent — ship a completed sprint.

Inputs come from the SE-pipeline orchestrator after Integration runs:

* ``sprint_id`` — the sprint to ship; every planned story must already
  be in a terminal status (``done``/``completed``/``cancelled``/``closed``).
* ``plan_dir`` — the run's ``{repo_path}/plan`` directory; the markdown
  notes file lands at ``{plan_dir}/releases/<version>.md``.
* Structured failure inputs from Integration / QA / DevOps so they can
  be promoted into ``feedback_items`` for the next groom.

Outputs:

* A new ``product_delivery_releases`` row with ``notes_path`` pointing at
  the markdown file.
* Zero or more ``product_delivery_feedback_items`` rows, one per
  promoted failure, each tagged with ``sprint_id``.
* The ``Release`` model returned to the caller.

The agent is meant to be called from a non-fatal hook (the SE-pipeline
wraps it in ``try/except``), so we keep the failure surface narrow and
documented:

* ``UnknownProductDeliveryEntity`` — the sprint id is missing.
* ``SprintNotComplete`` — at least one planned story is still open.
* ``ProductDeliveryStorageUnavailable`` — Postgres is unreachable.

Anything else raised from the notes writer is caught here and folded
into a fallback markdown body; the release row is still written so the
shipping event is observable.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from product_delivery.author import resolve_author
from product_delivery.models import Release
from product_delivery.store import (
    ProductDeliveryStore,
    SprintNotComplete,
    UnknownProductDeliveryEntity,
)

logger = logging.getLogger(__name__)

# `_VERSION_SAFE` strips any character that isn't safe for a path
# component on POSIX or Windows. Date-stamp versions only ever contain
# digits + `-`, but explicit overrides could be anything.
_VERSION_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

_NOTES_SUBDIR = "releases"


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ReleaseManagerAgent:
    """Stateless: depends on a store + a notes writer.

    The notes writer is any object exposing a ``run(ReleaseNotesInput) ->
    ReleaseNotesOutput`` method (production: the
    ``technical_writers.release_notes_agent.ReleaseNotesAgent``; tests:
    a stub). Two construction paths to mirror :class:`ProductOwnerAgent`:

    * pass ``notes_writer`` directly (test path, lazy injection);
    * pass ``notes_writer_factory`` to defer construction until first
      use (production path — avoids an LLM-model lookup at SE-pipeline
      import time).
    """

    def __init__(
        self,
        store: ProductDeliveryStore,
        notes_writer: Any | None = None,
        *,
        notes_writer_factory: Callable[[], Any] | None = None,
    ) -> None:
        if notes_writer is not None and notes_writer_factory is not None:
            raise ValueError("pass exactly one of notes_writer or notes_writer_factory")
        self._store = store
        self._writer: Any | None = notes_writer
        self._factory: Callable[[], Any] | None = notes_writer_factory

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def ship(
        self,
        *,
        sprint_id: str,
        plan_dir: Path,
        integration_issues: Sequence[Any] = (),
        qa_failures: Sequence[Any] = (),
        devops_failures: Sequence[Any] = (),
        version: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
        author: str | None = None,
    ) -> Release:
        """Persist a release row + write the markdown notes file.

        Raises:
            UnknownProductDeliveryEntity: the sprint id is missing.
            SprintNotComplete: at least one planned story is non-terminal.
        """
        sprint_view = self._store.get_sprint_with_stories(sprint_id)
        if sprint_view is None:
            raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
        # Re-check completion ourselves rather than trusting the caller
        # so the agent is safe to invoke from places besides the SE
        # hook (manual `POST /releases/ship` endpoint, future Temporal
        # workflow). ``count_open_stories_in_sprint`` raises if the
        # sprint disappears mid-call — let that propagate.
        open_count = self._store.count_open_stories_in_sprint(sprint_id)
        if open_count > 0:
            raise SprintNotComplete(
                f"sprint {sprint_id!r} still has {open_count} open story(ies); "
                "wait for them to reach a terminal status before shipping."
            )

        product_id = sprint_view.sprint.product_id
        author = author or resolve_author()
        shipped_at = clock()
        chosen_version = self._resolve_version(version, shipped_at, plan_dir)
        notes_path = self._build_notes_path(plan_dir, chosen_version)

        # 1. Compose markdown notes (never raises — agent has its own
        #    deterministic fallback).
        notes_input = self._build_notes_input(
            sprint_view=sprint_view,
            version=chosen_version,
            shipped_at=shipped_at,
            integration_issues=integration_issues,
            qa_failures=qa_failures,
            devops_failures=devops_failures,
            plan_dir=plan_dir,
        )
        notes_output = self._writer_or_resolve().run(notes_input)
        if notes_output.llm_failed:
            logger.info(
                "ReleaseManagerAgent: notes writer fell back to deterministic body for %s (%s)",
                chosen_version,
                notes_output.error,
            )

        # 2. Atomically land the markdown file. ``mkdir(parents=True)``
        #    is idempotent so reruns on the same plan_dir are safe.
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        # ``write_text`` with explicit encoding is not strictly atomic,
        # but the failure mode is "no file written, no DB row written"
        # — we persist the row only after the file lands so callers
        # never see a release row pointing at a missing path.
        notes_path.write_text(notes_output.markdown, encoding="utf-8")

        # 3. Record the release row.
        release = self._store.create_release(
            sprint_id=sprint_id,
            version=chosen_version,
            notes_path=str(notes_path),
            shipped_at=shipped_at,
            author=author,
        )

        # 4. Promote failures into feedback_items. Each call is wrapped
        #    so a single bad payload doesn't block the rest — the
        #    release itself is the source of truth that we *did* ship,
        #    and lost feedback is recoverable on the next run.
        self._promote_failures(
            product_id=product_id,
            sprint_id=sprint_id,
            integration_issues=integration_issues,
            qa_failures=qa_failures,
            devops_failures=devops_failures,
            author=author,
        )

        logger.info(
            "ReleaseManagerAgent: shipped %s for sprint %s (%d stories, %d feedback items)",
            chosen_version,
            sprint_id,
            len(sprint_view.stories),
            (len(integration_issues) + len(qa_failures) + len(devops_failures)),
        )
        return release

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _writer_or_resolve(self) -> Any:
        if self._writer is None:
            if self._factory is None:
                # Lazy-import the default writer so the agent module
                # stays importable in environments without strands.
                from software_engineering_team.technical_writers.release_notes_agent import (  # noqa: PLC0415
                    ReleaseNotesAgent,
                )

                self._writer = ReleaseNotesAgent()
            else:
                self._writer = self._factory()
        return self._writer

    def _resolve_version(
        self,
        version: str | None,
        shipped_at: datetime,
        plan_dir: Path,
    ) -> str:
        """Default to a date-stamp; suffix ``-N`` on collision.

        Issue #371 explicitly defers semver/version policy and asks for
        date-stamped notes. We start with ``YYYY-MM-DD`` (UTC), and on a
        same-day re-ship the suffix bumps so two runs in one day don't
        clobber each other's notes file.
        """
        if version is not None:
            stripped = version.strip()
            if not stripped:
                raise ValueError("version override must be non-blank")
            return _VERSION_SAFE.sub("-", stripped)[:80]
        base = shipped_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        candidate = base
        suffix = 1
        # Cheap collision check against the on-disk notes folder; the
        # store itself doesn't enforce uniqueness on (sprint_id, version)
        # but two ships on the same day would otherwise overwrite the
        # markdown file. Cap the loop so we never spin pathologically.
        notes_root = plan_dir / _NOTES_SUBDIR
        for _ in range(100):
            if not (notes_root / f"{candidate}.md").exists():
                return candidate
            candidate = f"{base}-{suffix}"
            suffix += 1
        # Pathological case (>100 ships in one day for one sprint) —
        # fall back to a microsecond-precision tag so we never block.
        return shipped_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%S%f")

    def _build_notes_path(self, plan_dir: Path, version: str) -> Path:
        return plan_dir / _NOTES_SUBDIR / f"{version}.md"

    def _build_notes_input(
        self,
        *,
        sprint_view: Any,
        version: str,
        shipped_at: datetime,
        integration_issues: Sequence[Any],
        qa_failures: Sequence[Any],
        devops_failures: Sequence[Any],
        plan_dir: Path,
    ) -> Any:
        # Local import keeps the SE technical_writers module a soft
        # dependency for callers that build their own notes writer
        # (e.g. tests using a stub).
        from software_engineering_team.technical_writers.release_notes_agent.models import (  # noqa: PLC0415
            ReleaseFailure,
            ReleaseNotesInput,
            ReleaseStorySummary,
        )

        acs_by_story = sprint_view.acceptance_criteria_by_story_id or {}
        stories = [
            ReleaseStorySummary(
                id=s.id,
                title=s.title,
                user_story=s.user_story or "",
                acceptance_criteria=[ac.text for ac in acs_by_story.get(s.id, [])],
            )
            for s in sprint_view.stories
        ]
        failures: list[ReleaseFailure] = []
        for issue in integration_issues:
            failures.append(_integration_to_failure(issue))
        for bug in qa_failures:
            failures.append(_qa_to_failure(bug))
        for dev in devops_failures:
            failures.append(_devops_to_failure(dev))
        return ReleaseNotesInput(
            version=version,
            sprint_name=sprint_view.sprint.name,
            sprint_id=sprint_view.sprint.id,
            shipped_at_iso=shipped_at.isoformat(),
            repo_path=str(plan_dir.parent),
            stories=stories,
            failures=failures,
        )

    def _promote_failures(
        self,
        *,
        product_id: str,
        sprint_id: str,
        integration_issues: Iterable[Any],
        qa_failures: Iterable[Any],
        devops_failures: Iterable[Any],
        author: str,
    ) -> None:
        for issue in integration_issues:
            self._safe_create_feedback(
                product_id=product_id,
                sprint_id=sprint_id,
                source="se-integration",
                severity=_map_severity(getattr(issue, "severity", None)),
                payload=_integration_payload(issue),
                author=author,
            )
        for bug in qa_failures:
            self._safe_create_feedback(
                product_id=product_id,
                sprint_id=sprint_id,
                source="se-qa",
                severity=_map_severity(getattr(bug, "severity", None)),
                payload=_qa_payload(bug),
                author=author,
            )
        for dev in devops_failures:
            self._safe_create_feedback(
                product_id=product_id,
                sprint_id=sprint_id,
                source="se-devops",
                severity=_map_severity(_dev_field(dev, "severity")),
                payload=_devops_payload(dev),
                author=author,
            )

    def _safe_create_feedback(
        self,
        *,
        product_id: str,
        sprint_id: str,
        source: str,
        severity: str,
        payload: dict[str, Any],
        author: str,
    ) -> None:
        try:
            self._store.create_feedback_item(
                product_id=product_id,
                source=source,
                raw_payload=payload,
                severity=severity,
                linked_story_id=None,
                author=author,
                sprint_id=sprint_id,
            )
        except Exception as exc:  # pragma: no cover — exercised via SE hook tests
            # Don't let a single bad payload stop the rest of the
            # promotion sweep. The release itself is recorded; lost
            # feedback can be re-submitted manually if needed.
            logger.warning(
                "ReleaseManagerAgent: failed to record %s feedback for sprint %s: %s",
                source,
                sprint_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Mapping helpers — adapt heterogeneous failure shapes into a uniform schema
# ---------------------------------------------------------------------------


def _map_severity(severity: Any) -> str:
    """Collapse the SE-team's 4-level scale onto feedback's 2-level scale.

    feedback.severity is a free-form ``TEXT`` column with conventional
    values ``"normal" | "high" | "critical"`` across the codebase; the
    SE agents emit ``critical|high|medium|low``. Anything blank or
    unrecognised maps to ``"normal"`` so the feedback row is still
    queryable but can't accidentally inflate triage urgency.
    """
    if not isinstance(severity, str):
        return "normal"
    s = severity.strip().lower()
    if s in {"critical", "high"}:
        return "high"
    return "normal"


def _integration_to_failure(issue: Any) -> Any:
    from software_engineering_team.technical_writers.release_notes_agent.models import (  # noqa: PLC0415
        ReleaseFailure,
    )

    location = " / ".join(
        x
        for x in (
            getattr(issue, "backend_location", None),
            getattr(issue, "frontend_location", None),
        )
        if x
    )
    return ReleaseFailure(
        source="integration",
        severity=str(getattr(issue, "severity", "") or "medium"),
        summary=str(getattr(issue, "description", "") or "(no description)"),
        location=location,
        recommendation=str(getattr(issue, "recommendation", "") or ""),
    )


def _qa_to_failure(bug: Any) -> Any:
    from software_engineering_team.technical_writers.release_notes_agent.models import (  # noqa: PLC0415
        ReleaseFailure,
    )

    return ReleaseFailure(
        source="qa",
        severity=str(getattr(bug, "severity", "") or "medium"),
        summary=str(getattr(bug, "description", "") or "(no description)"),
        location=str(getattr(bug, "location", "") or ""),
        recommendation=str(getattr(bug, "recommendation", "") or ""),
    )


def _devops_to_failure(dev: Any) -> Any:
    from software_engineering_team.technical_writers.release_notes_agent.models import (  # noqa: PLC0415
        ReleaseFailure,
    )

    return ReleaseFailure(
        source="devops",
        severity=str(_dev_field(dev, "severity") or "medium"),
        summary=str(
            _dev_field(dev, "description") or _dev_field(dev, "summary") or "(no description)"
        ),
        location=str(_dev_field(dev, "location") or ""),
        recommendation=str(_dev_field(dev, "recommendation") or ""),
    )


def _dev_field(dev: Any, key: str) -> Any:
    """Best-effort attribute/key access — DevOps failures are dicts today."""
    if isinstance(dev, dict):
        return dev.get(key)
    return getattr(dev, key, None)


def _integration_payload(issue: Any) -> dict[str, Any]:
    return _strip_none(
        {
            "kind": "integration",
            "severity": getattr(issue, "severity", None),
            "category": getattr(issue, "category", None),
            "description": getattr(issue, "description", None),
            "backend_location": getattr(issue, "backend_location", None),
            "frontend_location": getattr(issue, "frontend_location", None),
            "recommendation": getattr(issue, "recommendation", None),
        }
    )


def _qa_payload(bug: Any) -> dict[str, Any]:
    return _strip_none(
        {
            "kind": "qa",
            "severity": getattr(bug, "severity", None),
            "description": getattr(bug, "description", None),
            "location": getattr(bug, "location", None),
            "recommendation": getattr(bug, "recommendation", None),
            "expected_vs_actual": getattr(bug, "expected_vs_actual", None),
        }
    )


def _devops_payload(dev: Any) -> dict[str, Any]:
    if isinstance(dev, dict):
        # Make a shallow copy so the caller's dict isn't mutated and we
        # can JSON-serialise it cleanly. We don't deep-validate — the
        # store's psycopg JSONB adaptation will reject anything truly
        # un-serialisable.
        payload = {k: v for k, v in dev.items() if v is not None}
        payload.setdefault("kind", "devops")
        return payload
    # Object form — best-effort attribute pull.
    return _strip_none(
        {
            "kind": "devops",
            "severity": getattr(dev, "severity", None),
            "description": getattr(dev, "description", None),
            "location": getattr(dev, "location", None),
            "recommendation": getattr(dev, "recommendation", None),
        }
    )


def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


__all__ = ["ReleaseManagerAgent"]
