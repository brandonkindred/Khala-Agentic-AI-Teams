"""Smoke tests for deprecated frontend_team sub-agent initialization.

These agents' run() methods reference ``self._model`` which is no longer
populated in __init__, so they can only be exercised at the constructor
level today. This module pins down the constructor contract.
"""

from __future__ import annotations

from llm_service import DummyLLMClient


def test_ux_designer_init():
    from software_engineering_team.frontend_team_deprecated.ux_designer.agent import (
        UXDesignerAgent,
    )

    a = UXDesignerAgent(llm_client=DummyLLMClient())
    assert a.llm is not None


def test_ui_designer_init():
    from software_engineering_team.frontend_team_deprecated.ui_designer.agent import (
        UIDesignerAgent,
    )

    a = UIDesignerAgent(llm_client=DummyLLMClient())
    assert a.llm is not None


def test_design_system_init():
    from software_engineering_team.frontend_team_deprecated.design_system.agent import (
        DesignSystemAgent,
    )

    a = DesignSystemAgent(llm_client=DummyLLMClient())
    assert a.llm is not None


def test_frontend_architect_init():
    from software_engineering_team.frontend_team_deprecated.frontend_architect.agent import (
        FrontendArchitectAgent,
    )

    a = FrontendArchitectAgent(llm_client=DummyLLMClient())
    assert a.llm is not None


def test_ux_engineer_init():
    from software_engineering_team.frontend_team_deprecated.ux_engineer.agent import (
        UXEngineerAgent,
    )

    a = UXEngineerAgent(llm_client=DummyLLMClient())
    assert a.llm is not None


def test_performance_engineer_init():
    from software_engineering_team.frontend_team_deprecated.performance_engineer.agent import (
        PerformanceEngineerAgent,
    )

    a = PerformanceEngineerAgent(llm_client=DummyLLMClient())
    assert a.llm is not None


def test_build_release_init():
    from software_engineering_team.frontend_team_deprecated.build_release.agent import (
        BuildReleaseAgent,
    )

    a = BuildReleaseAgent(llm_client=DummyLLMClient())
    assert a.llm is not None
