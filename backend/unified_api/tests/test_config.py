"""Unit tests for unified_api.config."""

import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))


from unified_api.config import TEAM_CONFIGS, TeamConfig, get_enabled_teams


def test_team_config_defaults():
    """TeamConfig.enabled defaults to True and tags defaults to empty list."""
    cfg = TeamConfig(name="Test", prefix="/api/test", description="A test team")
    assert cfg.enabled is True
    assert cfg.tags == []


def test_team_config_can_be_disabled():
    """TeamConfig accepts enabled=False."""
    cfg = TeamConfig(name="Test", prefix="/api/test", description="desc", enabled=False)
    assert cfg.enabled is False


def test_team_configs_has_expected_teams():
    """TEAM_CONFIGS contains all expected team keys."""
    expected = {
        "blogging",
        "software_engineering",
        "personal_assistant",
        "market_research",
        "soc2_compliance",
        "social_marketing",
        "branding",
        "agent_provisioning",
        "accessibility_audit",
        "ai_systems",
        "investment",
        "nutrition_meal_planning",
        "planning_v3",
        "coding_team",
        "studio_grid",
        "sales_team",
        "road_trip_planning",
    }
    assert expected.issubset(set(TEAM_CONFIGS.keys()))


def test_team_configs_prefixes_start_with_api():
    """All team prefixes start with /api/."""
    for key, cfg in TEAM_CONFIGS.items():
        assert cfg.prefix.startswith("/api/"), f"{key} prefix {cfg.prefix!r} does not start with /api/"


def test_team_configs_prefixes_are_unique():
    """No two teams share the same prefix."""
    prefixes = [cfg.prefix for cfg in TEAM_CONFIGS.values()]
    assert len(prefixes) == len(set(prefixes)), "Duplicate prefixes found"


def test_team_configs_all_have_non_empty_name_and_description():
    """All team configs have a non-empty name and description."""
    for key, cfg in TEAM_CONFIGS.items():
        assert cfg.name, f"Team {key} has empty name"
        assert cfg.description, f"Team {key} has empty description"


def test_get_enabled_teams_returns_only_enabled():
    """get_enabled_teams() returns only teams where enabled=True."""
    enabled = get_enabled_teams()
    for key, cfg in enabled.items():
        assert cfg.enabled is True, f"Team {key} should be enabled but enabled={cfg.enabled}"


def test_get_enabled_teams_excludes_disabled():
    """get_enabled_teams() excludes teams set to enabled=False."""
    import unified_api.config as cfg_mod

    original = cfg_mod.TEAM_CONFIGS["blogging"]
    try:
        cfg_mod.TEAM_CONFIGS["blogging"] = TeamConfig(
            name="Blogging", prefix="/api/blogging", description="test", enabled=False
        )
        enabled = cfg_mod.get_enabled_teams()
        assert "blogging" not in enabled
    finally:
        cfg_mod.TEAM_CONFIGS["blogging"] = original


def test_get_enabled_teams_is_subset_of_all_teams():
    """get_enabled_teams() is always a subset of all TEAM_CONFIGS."""
    enabled = get_enabled_teams()
    assert set(enabled.keys()).issubset(set(TEAM_CONFIGS.keys()))


def test_blogging_team_config_structure():
    """Blogging team config has correct prefix and tags."""
    cfg = TEAM_CONFIGS["blogging"]
    assert cfg.prefix == "/api/blogging"
    assert "blogging" in cfg.tags


def test_software_engineering_team_config_structure():
    """Software engineering team config has correct prefix."""
    cfg = TEAM_CONFIGS["software_engineering"]
    assert cfg.prefix == "/api/software-engineering"


def test_team_config_tags_is_list():
    """All TeamConfig.tags fields are lists."""
    for key, cfg in TEAM_CONFIGS.items():
        assert isinstance(cfg.tags, list), f"Team {key} tags is not a list"
