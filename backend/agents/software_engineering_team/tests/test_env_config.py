"""The SE env_config module re-exports the shared readers (impl tested in
``shared_env_config/tests/test_config.py``)."""

from __future__ import annotations


def test_se_env_config_reexports_shared() -> None:
    from shared_env_config import env_bool as shared_env_bool
    from shared_env_config import env_float as shared_env_float
    from shared_env_config import env_int as shared_env_int
    from software_engineering_team.shared import env_config

    assert env_config.env_bool is shared_env_bool
    assert env_config.env_int is shared_env_int
    assert env_config.env_float is shared_env_float
