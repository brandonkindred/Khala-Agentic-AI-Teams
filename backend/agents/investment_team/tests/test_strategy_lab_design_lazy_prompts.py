"""Import-time filesystem independence for design agent prompts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Prompt files that ``design.py`` used to read at import time. Sibling agents
# under ``strategy_lab.agents`` may still read other prompts during package
# import; this suite only guards the design agent.
_DESIGN_IMPORT_PROMPT_NAMES = frozenset(
    {
        "design_system.md",
        "design_self_review_system.md",
    }
)


def test_design_module_import_does_not_read_prompt_files() -> None:
    """Importing design must not read its system / self-review prompt files.

    Preconditions: a fresh Python process can import ``investment_team`` with
    ``agents/`` on ``PYTHONPATH``.
    Postconditions: the child process exits 0; ``design_system.md`` and
    ``design_self_review_system.md`` are not read during import; the design
    module's prompt caches remain empty (unwarmed).
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
watched = {set(_DESIGN_IMPORT_PROMPT_NAMES)!r}
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
mod = importlib.import_module("investment_team.strategy_lab.agents.design")
if reads:
    raise SystemExit(f"import read design prompt files: {{reads!r}}")
if mod._get_stop_order_semantics.cache_info().currsize != 0:
    raise SystemExit("stop-order cache warmed at import")
if mod._get_sizing_risk_framing.cache_info().currsize != 0:
    raise SystemExit("sizing-risk-framing cache warmed at import")
if mod._get_design_system_prompt.cache_info().currsize != 0:
    raise SystemExit("design system prompt cache warmed at import")
if mod._get_self_review_system_prompt.cache_info().currsize != 0:
    raise SystemExit("self-review prompt cache warmed at import")
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


def test_design_prompt_helpers_include_stop_order_and_bodies() -> None:
    """Helpers concatenate stop-order semantics after each prompt body.

    Preconditions: prompt markdown files exist under strategy_lab/prompts.
    Postconditions: stop-order text is non-empty and appears after the body in
    both design and self-review system prompts.
    """
    from investment_team.strategy_lab.agents.design import (
        _get_design_system_prompt,
        _get_self_review_system_prompt,
        _get_stop_order_semantics,
    )

    stop = _get_stop_order_semantics()
    design = _get_design_system_prompt()
    review = _get_self_review_system_prompt()

    assert "NOT a defect" in stop
    assert stop in design
    assert stop in review
    assert design.index(stop) > 0
    assert review.index(stop) > 0


def test_design_system_prompt_includes_sizing_risk_framing() -> None:
    """Both the designer and self-review system prompts append the sizing/risk block.

    Preconditions: prompt markdown files exist under strategy_lab/prompts.
    Postconditions: sizing/risk framing text is non-empty and appears after
    the stop-order block in both the design system prompt and the
    self-review system prompt.
    """
    from investment_team.strategy_lab.agents.design import (
        _get_design_system_prompt,
        _get_self_review_system_prompt,
        _get_sizing_risk_framing,
        _get_stop_order_semantics,
    )

    sizing = _get_sizing_risk_framing()
    stop = _get_stop_order_semantics()
    design = _get_design_system_prompt()
    review = _get_self_review_system_prompt()

    assert "per-trade loss cap" in sizing
    assert sizing in design
    assert design.index(sizing) > design.index(stop)
    assert sizing in review
    assert review.index(sizing) > review.index(stop)


def test_design_prompt_helpers_cache_composed_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second helper calls must reuse the composed cache (no extra disk reads).

    Preconditions: prompt files exist; caches may already be warm from earlier
    tests in this module — this test still verifies identity stability and that
    further helper calls do not invoke ``Path.read_text``.
    Postconditions: repeated helper calls return the same string object and
    perform zero additional prompt-directory reads.
    """
    import investment_team.strategy_lab.agents.design as design_mod

    prompt_dir = design_mod._PROMPT_DIR.resolve()
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
    first_design = design_mod._get_design_system_prompt()
    first_review = design_mod._get_self_review_system_prompt()

    monkeypatch.setattr(Path, "read_text", tracking_read_text)
    second_design = design_mod._get_design_system_prompt()
    second_review = design_mod._get_self_review_system_prompt()

    assert second_design is first_design
    assert second_review is first_review
    assert reads == []
