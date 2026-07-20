"""The SE env_config module re-exports the shared readers (impl tested in
``shared/env_config/tests/test_config.py``)."""

from __future__ import annotations


def test_se_env_config_reexports_shared() -> None:
    """The SE env_config re-exports the shared env_bool/env_int/env_float readers."""
    from shared.env_config import env_bool as shared.env_bool
    from shared.env_config import env_float as shared.env_float
    from shared.env_config import env_int as shared.env_int
    from software_engineering_team.shared import env_config

    assert env_config.env_bool is shared.env_bool
    assert env_config.env_int is shared.env_int
    assert env_config.env_float is shared.env_float
