"""Verify Nutrition & Meal Planning team is registered in unified API (config, mount, shutdown)."""

import sys
from pathlib import Path

# Add backend to path so unified_api can be imported
_backend = Path(__file__).resolve().parent.parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))


def test_team_in_config():
    from unified_api.config import TEAM_CONFIGS
    assert "nutrition_meal_planning" in TEAM_CONFIGS
    config = TEAM_CONFIGS["nutrition_meal_planning"]
    assert config.prefix == "/api/nutrition-meal-planning"
    assert "nutrition" in config.tags or "meal-planning" in config.tags


def test_mount_function_registered():
    from unified_api.main import _try_mount_nutrition_meal_planning
    assert callable(_try_mount_nutrition_meal_planning)


def test_shutdown_hook_registered():
    from unified_api.main import SHUTDOWN_HOOKS
    assert "nutrition_meal_planning" in SHUTDOWN_HOOKS
    module_path, func_name = SHUTDOWN_HOOKS["nutrition_meal_planning"]
    assert module_path == "nutrition_meal_planning_team.shared.job_store"
    assert func_name == "mark_all_running_jobs_failed"


def test_mark_all_running_jobs_failed_callable():
    from nutrition_meal_planning_team.shared.job_store import mark_all_running_jobs_failed
    mark_all_running_jobs_failed("test reason")
    # No exception; can be called with no running jobs
