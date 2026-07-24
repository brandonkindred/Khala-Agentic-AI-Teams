"""Import-time filesystem independence for design agent prompts."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def test_design_module_import_does_not_read_prompt_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing design must not call Path.read_text for strategy_lab prompts.

    Preconditions: ``investment_team.strategy_lab.agents.design`` may already
    be loaded; this test reloads it under a patched ``Path.read_text``.
    Postconditions: reload succeeds and the patch recorded zero reads whose
    path is under the design prompt directory.
    """
    prompt_dir = (
        Path(__file__).resolve().parent.parent / "strategy_lab" / "prompts"
    ).resolve()
    reads: list[Path] = []
    real_read_text = Path.read_text

    def tracking_read_text(self: Path, *args: object, **kwargs: object) -> str:
        resolved = self.resolve()
        try:
            resolved.relative_to(prompt_dir)
        except ValueError:
            return real_read_text(self, *args, **kwargs)
        reads.append(resolved)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    mod_name = "investment_team.strategy_lab.agents.design"
    sys.modules.pop(mod_name, None)
    importlib.import_module(mod_name)

    assert reads == [], f"import read prompt files: {reads}"
