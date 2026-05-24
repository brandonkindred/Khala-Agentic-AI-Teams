import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Add blogging directory so blog_research_agent tools are importable
BLOGGING_DIR = ROOT / "blogging"
if str(BLOGGING_DIR) not in sys.path:
    sys.path.insert(0, str(BLOGGING_DIR))

from social_media_marketing_team.api import main as api_main  # noqa: E402
from social_media_marketing_team.models import BrandGoals  # noqa: E402


@pytest.fixture(autouse=True)
def _patched_job_manager(monkeypatch, fake_job_client):
    """Route the SMM ``_job_manager`` through the in-memory fake.

    Preconditions: ``fake_job_client`` resolves to the function-scoped
        ``FakeJobServiceClient`` defined in ``backend/conftest.py``.
    Postconditions: every call to ``api_main._job_manager.*`` within a test
        hits the in-memory fake; the real ``JobServiceClient`` is never used.
    """
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    return fake_job_client


@pytest.fixture()
def _inline_threading(monkeypatch):
    """Run dispatched jobs synchronously so tests don't need polling.

    Not autouse — only test modules that exercise the ``/run`` endpoint
    should opt in via ``pytest.mark.usefixtures("_inline_threading")``.
    Patching ``api_main.threading.Thread`` globally would break OTel's
    ``ThreadingInstrumentor`` in tests that create Strands agents.

    Postconditions: ``threading.Thread`` in ``api_main`` is replaced with
        an inline variant that calls ``target(*args)`` on ``start()``.
    """

    class _InlineThread:
        def __init__(self, target, args=(), daemon=False, **kw):
            self._target, self._args = target, args

        def run(self):
            self._target(*self._args)

        def start(self):
            self.run()

    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)


def make_goals(
    brand_name: str = "Northstar",
    target_audience: str = "growth leaders",
    goals: list[str] | None = None,
    cadence_posts_per_day: int = 2,
    duration_days: int = 14,
    **kwargs,
) -> BrandGoals:
    """Build a ``BrandGoals`` with sensible defaults for tests."""
    return BrandGoals(
        brand_name=brand_name,
        target_audience=target_audience,
        goals=goals or ["engagement", "followers"],
        cadence_posts_per_day=cadence_posts_per_day,
        duration_days=duration_days,
        **kwargs,
    )


@pytest.fixture()
def default_goals() -> BrandGoals:
    """Pytest fixture exposing ``make_goals()`` with all defaults."""
    return make_goals()
