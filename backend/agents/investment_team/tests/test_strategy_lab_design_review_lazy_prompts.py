"""Import-time filesystem independence for design-review agent prompts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Prompt files that ``design_review.py`` used to read at import time. Sibling
# agents under ``strategy_lab.agents`` may still read other prompts during
# package import; this suite only guards the design-review agent.
_DESIGN_REVIEW_IMPORT_PROMPT_NAMES = frozenset(
    {
        "design_review_system.md",
    }
)


def test_design_review_module_import_does_not_read_prompt_files() -> None:
    """Importing design_review must not read its system prompt file.

    Preconditions: a fresh Python process can import ``investment_team`` with
    ``agents/`` on ``PYTHONPATH``.
    Postconditions: the child process exits 0; ``design_review_system.md`` is
    not read during import; the design-review module's prompt caches remain
    empty (unwarmed).
    Runs in a subprocess so the check does not pollute this session's
    ``sys.modules``.
    """
    backend_root = Path(__file__).resolve().parents[3]
    agents_root = backend_root / "agents"
    prompt_dir = (
        Path(__file__).resolve().parent.parent / "strategy_lab" / "prompts"
    ).resolve()

    script = f"""
from pathlib import Path
import importlib

prompt_dir = Path({str(prompt_dir)!r}).resolve()
watched = {set(_DESIGN_REVIEW_IMPORT_PROMPT_NAMES)!r}
reads: list[str] = []
real_read_text = Path.read_text

def tracking_read_text(self, *args, **kwargs):
    resolved = self.resolve()
    try:
        resolved.relative_to(prompt_dir)
    except ValueError:
        return real_read_text(self, *args, **kwargs)
    if resolved.name in watched:
        reads.append(str(resolved))
    return real_read_text(self, *args, **kwargs)

Path.read_text = tracking_read_text
mod = importlib.import_module("investment_team.strategy_lab.agents.design_review")
if reads:
    raise SystemExit(f"import read design-review prompt files: {{reads!r}}")
if mod._get_stop_order_semantics.cache_info().currsize != 0:
    raise SystemExit("stop-order cache warmed at import")
if mod._get_sizing_risk_framing.cache_info().currsize != 0:
    raise SystemExit("sizing-risk-framing cache warmed at import")
if mod._get_system_prompt.cache_info().currsize != 0:
    raise SystemExit("system prompt cache warmed at import")
"""

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(agents_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(backend_root),
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_design_review_prompt_helpers_include_stop_order_and_body() -> None:
    """Helper concatenates stop-order semantics after the prompt body.

    Preconditions: prompt markdown files exist under strategy_lab/prompts.
    Postconditions: stop-order text is non-empty and appears after the body in
    the design-review system prompt.
    """
    from investment_team.strategy_lab.agents.design_review import (
        _get_stop_order_semantics,
        _get_system_prompt,
    )

    stop = _get_stop_order_semantics()
    prompt = _get_system_prompt()

    assert "NOT a defect" in stop
    assert stop in prompt
    assert prompt.index(stop) > 0


def test_design_review_prompt_helper_includes_sizing_risk_framing() -> None:
    """Helper concatenates sizing/risk framing after the stop-order block.

    Preconditions: prompt markdown files exist under strategy_lab/prompts.
    Postconditions: sizing/risk framing text is non-empty and appears after
    the stop-order block in the design-review system prompt.
    """
    from investment_team.strategy_lab.agents.design_review import (
        _get_sizing_risk_framing,
        _get_stop_order_semantics,
        _get_system_prompt,
    )

    sizing = _get_sizing_risk_framing()
    stop = _get_stop_order_semantics()
    prompt = _get_system_prompt()

    assert "per-trade loss cap" in sizing
    assert sizing in prompt
    assert prompt.index(sizing) > prompt.index(stop)


def test_design_review_prompt_helpers_cache_composed_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second helper calls must reuse the composed cache (no extra disk reads).

    Preconditions: prompt files exist; caches may already be warm from earlier
    tests in this module — this test still verifies identity stability and that
    further helper calls do not invoke ``Path.read_text``.
    Postconditions: repeated helper calls return the same string object and
    perform zero additional prompt-directory reads.
    """
    import investment_team.strategy_lab.agents.design_review as design_review_mod

    prompt_dir = design_review_mod._PROMPT_DIR.resolve()
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

    # Warm caches first (may already be warm).
    first_prompt = design_review_mod._get_system_prompt()

    monkeypatch.setattr(Path, "read_text", tracking_read_text)
    second_prompt = design_review_mod._get_system_prompt()

    assert second_prompt is first_prompt
    assert reads == []
