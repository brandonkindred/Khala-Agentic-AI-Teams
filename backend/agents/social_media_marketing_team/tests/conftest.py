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

from social_media_marketing_team.models import BrandGoals  # noqa: E402


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
