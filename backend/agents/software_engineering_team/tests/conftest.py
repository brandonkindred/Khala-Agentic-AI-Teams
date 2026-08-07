"""Shared test fixtures for the software engineering team."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict

import pytest

# Fire-and-forget daemon threads (job heartbeats, the stale-job monitor, the
# background workflow workers) can emit a log record after pytest has torn down
# a test's capture streams, surfacing as a noisy
# "--- Logging error --- ValueError: I/O operation on closed file" traceback on
# stderr. That is a teardown-race artifact, not a test failure: the handler's
# emit() raises because the captured stream is already closed. Flip logging to
# its documented production setting so a failed handler emit is swallowed instead
# of dumping a traceback. (The heavy workers are still stubbed at the source in
# the per-endpoint API tests; this only silences the residual race.)
logging.raiseExceptions = False

# Mirror the env defaults from ``backend/conftest.py`` (not auto-discovered
# here because this team overrides pytest's rootdir).  The placeholder
# JOB_SERVICE_URL lets module-level ``JobServiceClient(team=…)`` construction
# succeed; real HTTP calls will fail loudly.
os.environ.setdefault("LLM_MAX_RETRIES", "0")
os.environ.setdefault("JOB_SERVICE_URL", "http://127.0.0.1:1")
# Disable the slow 429 rate-limit backoff so no test can ever sleep the 300s+
# schedule.  Mirrored here because this team overrides pytest's rootdir, so the
# matching default in ``backend/conftest.py`` is not auto-discovered.
os.environ.setdefault("LLM_RATE_LIMIT_MAX_RETRIES", "0")
# The provider list is the sole source of LLM resolution — get_client raises
# LLMNotConfiguredError with no list configured. SE unit tests construct agents
# (whose __init__ builds a Strands model via get_client) without a real provider,
# so default to the ``dummy`` no-LLM harness. Tests that need a specific provider
# override this via monkeypatch / patch.dict.
os.environ.setdefault("LLM_PROVIDER", "dummy")

# Re-export the in-memory FakeJobServiceClient + ``fake_job_client`` fixture so
# unit tests in this team can use them.  The SE team's ``pyproject.toml``
# overrides pytest's rootdir, which means ``backend/conftest.py`` is not
# auto-discovered here, so we pull the fixture in explicitly (and re-register
# the ``integration`` marker / default-skip behaviour for the same reason).
from job_service_client_fake import fake_job_client  # noqa: F401, E402
from llm_service import DummyLLMClient, clear_compaction_cache  # noqa: E402

# The coordinator's caches exist once per module identity: production code
# imports the dotted ``software_engineering_team.code_review_agent`` package,
# while some tests still drive the bare ``code_review_agent`` name (resolved
# via the pytest ``pythonpath`` entry). Each identity carries its own cache
# dicts, so the reset fixture below must clear every LOADED identity or one
# side's cached outcome leaks into the next test. Identities are resolved
# lazily through ``sys.modules`` so a targeted test run that never touches
# code review does not pay a second full import of the package.
_COORDINATOR_IDENTITIES = (
    "code_review_agent.coordinator",
    "software_engineering_team.code_review_agent.coordinator",
)


def _clear_coordinator_caches() -> None:
    """Clear the code-review coordinator caches on every loaded module identity.

    Preconditions:
        - None. Identities that are not present in ``sys.modules`` are skipped
          (an unimported module cannot have populated its caches).

    Postconditions:
        - For each loaded identity in ``_COORDINATOR_IDENTITIES``, the chunk
          and submission outcome caches are empty.
    """
    for name in _COORDINATOR_IDENTITIES:
        mod = sys.modules.get(name)
        if mod is not None:
            mod.clear_chunk_outcome_cache()
            mod.clear_submission_outcome_cache()


def _ensure_real_modules() -> None:
    """Evict synthetic module stubs other test files may have installed.

    ``test_coding_team_github_source._stub_heavy_modules()`` registers a fake
    ``shared.git.git_utils`` in ``sys.modules`` with no ``__file__``; tests
    that drive real ``_prepare_issue_branch``/coding-team-main git calls need
    the real implementation (and an ``api.main`` bound to it), under any
    test-execution order.
    """
    stale = False
    for name in (
        "shared.git.git_utils",
        "software_engineering_team.shared",
        "software_engineering_team",
        "software_engineering_team.coding_team_orchestrator",
    ):
        mod = sys.modules.get(name)
        if mod is not None and not getattr(mod, "__file__", None):
            del sys.modules[name]
            stale = True
    if stale:
        sys.modules.pop("software_engineering_team.api.coding_team_main", None)


def _stub_orchestrator_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep api.main importable without the heavy agent stack.

    Installs via ``monkeypatch.setitem`` (not a bare ``sys.modules[...] =
    stub`` assignment) so pytest automatically reverts this ``sys.modules``
    entry at the end of the test that requested it — otherwise the stub
    outlives this test and can leak into an unrelated test (in this file, or
    another sharing the same xdist worker process) that imports the real
    ``coding_team.orchestrator`` and gets this no-op stand-in instead.
    """
    import types

    if "software_engineering_team.coding_team_orchestrator" not in sys.modules:
        stub = types.ModuleType("software_engineering_team.coding_team_orchestrator")
        stub.run_coding_team_orchestrator = lambda *a, **kw: None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "software_engineering_team.coding_team_orchestrator", stub)


def _expected_basic_header(token: str) -> str:
    """Expected git auth header for a fake token, built at runtime so a
    credential-shaped Base64 literal never appears in source — secret
    scanners (GitGuardian etc.) flag the pattern regardless of how fake
    the values are."""
    import base64

    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


@pytest.fixture
def patched_job_store(monkeypatch, fake_job_client):  # noqa: F811 (pytest fixture name)
    """Route the SE ``job_store._client`` factory through the in-memory fake.

    Opt-in shared form of the pattern in ``test_job_store_heartbeat.py``.
    Tests that want it applied unconditionally should wrap it with their
    own ``autouse=True`` fixture (see e.g. ``test_api.py``).
    """
    from software_engineering_team.shared import job_store as js

    monkeypatch.setattr(js, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


@pytest.fixture(autouse=True)
def _reset_code_review_chunk_cache():
    """Clear the process-global map-phase and compaction caches around every test.

    Both caches persist across calls by design (that is what lets the
    review→fix→re-review loop skip unchanged chunks and reuse the compacted
    spec/architecture). The submission-level short-circuit cache is the same
    story one level up (an identical approved submission returns with no LLM
    call). Tests, however, drive the coordinator with scripted clients that
    return different output for byte-identical input across tests — the exact
    non-determinism the caches assume never happens in production. Without a
    reset, one test's cached outcome would be served to the next test whose
    content and context hash the same. Clearing empty caches is trivially cheap,
    so this runs for every SE test unconditionally.
    """
    _clear_coordinator_caches()
    clear_compaction_cache()
    yield
    _clear_coordinator_caches()
    clear_compaction_cache()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires real Postgres + the central job service. "
        "Skipped unless invoked with `-m integration`.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    selected = config.getoption("-m", default="") or ""
    if "integration" in selected:
        return
    skip = pytest.mark.skip(reason="integration test; run with `pytest -m integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


class _TrackingMock:
    """Lightweight mock that tracks calls and supports return_value / side_effect."""

    def __init__(self, fallback):
        self._fallback = fallback
        self._return_value = _SENTINEL
        self._side_effect = _SENTINEL
        self.call_count = 0
        self.call_args = None
        self.call_args_list = []

    @property
    def return_value(self):
        return self._return_value

    @return_value.setter
    def return_value(self, value):
        self._return_value = value

    @property
    def side_effect(self):
        return self._side_effect

    @side_effect.setter
    def side_effect(self, value):
        self._side_effect = value

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        self.call_args = (args, kwargs)
        self.call_args_list.append((args, kwargs))
        if self._side_effect is not _SENTINEL:
            if isinstance(self._side_effect, list):
                if self._side_effect:
                    item = self._side_effect.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    return item
            elif callable(self._side_effect):
                return self._side_effect(*args, **kwargs)
            elif isinstance(self._side_effect, Exception):
                raise self._side_effect
        if self._return_value is not _SENTINEL:
            return self._return_value
        return self._fallback(*args, **kwargs)

    def assert_called(self):
        assert self.call_count > 0, "Expected to have been called"

    def assert_not_called(self):
        assert self.call_count == 0, (
            f"Expected not to have been called, but was called {self.call_count} time(s)"
        )

    def assert_called_once(self):
        assert self.call_count == 1, (
            f"Expected to be called once, but was called {self.call_count} time(s)"
        )


_SENTINEL = object()


class ConfigurableLLM(DummyLLMClient):
    """DummyLLMClient subclass with MagicMock-style return_value support.

    Usage::

        llm = ConfigurableLLM()
        llm.complete_json_mock.return_value = {"code": "...", "files": {...}}
        agent = SomeSpecialistAgent(llm_client=llm)
        # ...
        assert llm.complete_json_mock.call_count == 1
    """

    def __init__(self) -> None:
        super().__init__()
        self.complete_json_mock = _TrackingMock(super().complete_json)
        self._max_context_tokens = 16384

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return self.complete_json_mock(prompt, **kwargs)

    def get_max_context_tokens(self) -> int:
        return self._max_context_tokens


def _strands_model_double():
    """Minimal double satisfying the Strands ``Model`` protocol for isinstance checks,
    so agent __init__ resolves ``self._model`` to this instance without touching the
    real get_client/get_strands_model machinery."""
    from strands.models.model import Model as StrandsModel

    class _M(StrandsModel):
        def update_config(self, *a, **kw):
            pass

        def get_config(self):
            return {}

        def structured_output(self, *a, **kw):  # pragma: no cover
            return {}

        async def stream(self, *a, **kw):  # pragma: no cover
            yield {}

    return _M()


def _fenced(payload: Dict[str, Any]) -> str:
    """Wrap a JSON-serializable payload in a markdown ```json fence, as models do."""
    return "```json\n" + json.dumps(payload) + "\n```"


class _FencedAgentInstance:
    """Callable standing in for a Strands ``Agent`` instance, always returning fixed text."""

    def __init__(self, text: str):
        self._text = text

    def __call__(self, prompt, **kwargs):
        return self._text


def _patch_fenced_response(monkeypatch, payload: Dict[str, Any], target_module=None) -> None:
    """Monkeypatch ``Agent`` on ``target_module`` (default: ``shared.llm``) to return
    ``payload`` wrapped in a markdown fence, proving a caller recovers it instead of
    crashing on a bare ``json.loads``. Pass ``target_module`` for call sites that build
    their own ``Agent`` directly rather than going through ``complete_json_with_continuation``.
    """
    if target_module is None:
        from software_engineering_team.shared import llm as target_module
    text = _fenced(payload)
    monkeypatch.setattr(target_module, "Agent", lambda *a, **kw: _FencedAgentInstance(text))
