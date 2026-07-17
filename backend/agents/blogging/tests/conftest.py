"""
Shared test fixtures and helpers for blogging agent tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest


def setup_artifacts_root(monkeypatch, tmp_path: Path) -> None:
    """Point ``BLOGGING_RUN_ARTIFACTS_ROOT`` at a test-owned temp directory.

    Preconditions:
        - ``tmp_path`` is a directory the calling test owns for its duration.
    Postconditions:
        - ``BLOGGING_RUN_ARTIFACTS_ROOT`` is set to ``str(tmp_path)`` for the test; monkeypatch
          restores the prior environment on teardown.
    """
    monkeypatch.setenv("BLOGGING_RUN_ARTIFACTS_ROOT", str(tmp_path))


def patch_job_event_bus_publish(monkeypatch, publish_fn: Callable[..., Any]) -> None:
    """Patch ``agents.blogging.shared.job_event_bus.publish`` for the duration of a test.

    Preconditions:
        - ``publish_fn`` matches ``job_event_bus.publish``'s call signature
          (``job_id, payload, event_type="update"``).
    Postconditions:
        - ``agents.blogging.shared.job_event_bus.publish`` is patched to ``publish_fn``.
    """
    from agents.blogging.shared import job_event_bus as bus

    monkeypatch.setattr(bus, "publish", publish_fn)


def make_writer_agent(
    *, writing_style_guide_content: str = "Style", brand_spec_content: str = "Brand"
) -> Any:
    """Build a BlogWriterAgent wired to DummyLLMClient with minimal style/brand guidelines.

    Preconditions:
        - None.
    Postconditions:
        - Returns a ``BlogWriterAgent`` constructed with ``DummyLLMClient()`` and the given
          (or default) style/brand content.
    """
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    return BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content=writing_style_guide_content,
        brand_spec_content=brand_spec_content,
    )


# Canonical draft bodies the stub writer emits. Named so the single shared value is
# deliberate (the per-file stubs this factory replaced used slightly different whitespace);
# no test asserts on the exact body, only that a draft starting with ``# Draft`` is produced.
_STUB_WRITER_DRAFT = "# Draft\n\nBody."
_STUB_WRITER_REVISED_DRAFT = "# Revised\n\nBody."


def make_stub_writer_class(*, escalation_summary: str = "") -> type:
    """Build a BlogWriterAgent stand-in class returning canned, always-approvable output.

    Preconditions:
        - None.
    Postconditions:
        - Returns a class (not an instance) suitable for monkeypatching a module's
          ``BlogWriterAgent`` reference; every method returns a deterministic ``WriterOutput``
          (``run`` yields ``_STUB_WRITER_DRAFT``, the revise variants yield
          ``_STUB_WRITER_REVISED_DRAFT``) so ``run_pipeline`` can proceed without an LLM.
          ``generate_escalation_summary`` returns ``escalation_summary`` (empty unless overridden).
    """
    from agents.blogging.blog_writer_agent.models import WriterOutput

    class _StubWriter:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return WriterOutput(draft=_STUB_WRITER_DRAFT)

        def revise(self, *a, **kw):
            return WriterOutput(draft=_STUB_WRITER_REVISED_DRAFT)

        def revise_from_user_feedback(self, *a, **kw):
            return WriterOutput(draft=_STUB_WRITER_REVISED_DRAFT)

        def identify_uncertainty_questions(self, *a, **kw):
            return []

        def analyze_user_feedback_for_guideline_updates(self, *a, **kw):
            return []

        def generate_escalation_summary(self, *a, **kw):
            return escalation_summary

    return _StubWriter


def make_stub_editor_class() -> type:
    """Build a BlogCopyEditorAgent stand-in class that always approves on the first pass.

    Preconditions:
        - None.
    Postconditions:
        - Returns a class (not an instance) suitable for monkeypatching a module's
          ``BlogCopyEditorAgent`` reference; its ``run`` always returns an approved report.
    """
    from agents.blogging.blog_copy_editor_agent.models import CopyEditorOutput

    class _StubEditor:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return CopyEditorOutput(approved=True, summary="ok", feedback_items=[])

    return _StubEditor


# Job-store helpers captured by reference inside ``api/main`` at import time. The
# ``patched_client`` fixture rebinds each to the fake-backed implementation so
# every endpoint hits the in-memory store.
_BLOG_JOB_HELPERS = (
    "create_blog_job",
    "delete_blog_job",
    "get_blog_job",
    "list_blog_jobs",
    "update_blog_job",
    "start_blog_job",
    "complete_blog_job",
    "fail_blog_job",
    "approve_blog_job",
    "unapprove_blog_job",
    "submit_title_selection",
    "submit_title_ratings",
    "submit_story_user_message",
    "skip_current_story_gap",
    "submit_blog_answers",
    "submit_draft_feedback",
    "is_waiting_for_draft_feedback",
)


@pytest.fixture(autouse=True)
def patched_blog_client(monkeypatch, fake_job_client) -> Any:
    """Back ``shared.blog_job_store`` with the in-memory fake for every test in this package.

    Preconditions:
        - ``fake_job_client`` (from ``job_service_client_fake``) is function-scoped, so every
          fixture/test in a given test function observes the same fake instance.
    Postconditions:
        - ``agents.blogging.shared.blog_job_store._client`` returns ``fake_job_client`` for the
          duration of the test; ``monkeypatch`` restores the original on teardown. Autouse, so no
          test needs to request this explicitly; ``patched_client`` below relies on this fixture
          for the base patch rather than re-applying it itself.
    """
    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


@pytest.fixture
def patched_client(patched_blog_client, monkeypatch, fake_job_client) -> Any:
    """Back the blogging API with the in-memory fake job store.

    Rebinds the job-store helper references captured inside ``api/main`` at import time, so
    every endpoint hits the fake. The base ``blog_job_store._client`` patch is already applied
    by the autouse ``patched_blog_client`` fixture (requested explicitly here to make the
    dependency clear); this fixture only adds the ``api_main`` helper rebinding.

    Preconditions:
        - ``patched_blog_client`` has already patched ``shared.blog_job_store._client``.
    Postconditions:
        - Every name in ``_BLOG_JOB_HELPERS`` that exists on ``bjs`` is rebound onto
          ``api_main``, so calls made through the FastAPI app resolve to the fake-backed
          implementation. Imports the shared app module lazily so test modules that never use
          this fixture do not pay the ``api/main`` import cost.
    """
    from _api_test_utils import api_main
    from agents.blogging.shared import blog_job_store as bjs

    for name in _BLOG_JOB_HELPERS:
        helper = getattr(bjs, name, None)
        if helper is not None:
            monkeypatch.setattr(api_main, name, helper)
    return fake_job_client


@pytest.fixture
def client(patched_client) -> Any:
    """A ``TestClient`` for the blogging app, backed by the fake job store."""
    from _api_test_utils import app
    from fastapi.testclient import TestClient

    return TestClient(app)
