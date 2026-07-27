import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from soc2_compliance_team.models import RepoContext  # noqa: E402


def default_repo_context(**overrides: Any) -> RepoContext:
    """A minimal, valid ``RepoContext`` for tests that don't care about its
    specific contents — shared by ``test_agents.py`` and ``test_pipeline.py``
    so the default fixture only needs to change in one place."""
    base = RepoContext(
        repo_path="/repo",
        code_summary="print('hi')",
        readme_content="# Title",
        file_list=["main.py", "README.md"],
        tech_stack_hint="Python",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class FakeLLM:
    """Minimal LLM stand-in returning a canned JSON dict per call.

    Shared by ``test_agents.py`` (the class-based TSC/report-writer agents)
    and ``test_pipeline.py`` (the pipeline steps that wrap them) — both stub
    the LLM the same way, so a single fake keeps their expectations of the
    client contract (``complete_json``/``complete``/``get_max_context_tokens``)
    from drifting apart.
    """

    def __init__(self, response: Dict[str, Any], ctx_tokens: int = 16384) -> None:
        self._response = response
        self._ctx_tokens = ctx_tokens
        self.calls: List[Dict[str, Any]] = []
        self.reasoning_calls: List[Dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        think: bool | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, "temperature": temperature, "think": think, **kwargs})
        return self._response

    def complete(self, prompt: str, **kwargs: Any) -> str:
        # ``complete`` serves two callers here: ``compact_text``, which passes
        # ``max_chars`` and wants the text back, and the two-call split's
        # think=True reasoning pass, which always passes ``think=True``.
        # Gate on the reasoning-only marker (not just max_chars' absence) so a
        # future compact_text call that also sets max_chars alongside an
        # incidental ``think`` kwarg can't be misclassified. Record the
        # reasoning call so tests can assert the criterion's reasoning
        # temperature actually reaches the pass that does the analysis —
        # ``self.calls`` only ever sees the think=False formatting call.
        if "max_chars" in kwargs and not kwargs.get("think"):
            return prompt[: kwargs["max_chars"]]
        self.reasoning_calls.append({"prompt": prompt, **kwargs})
        return prompt

    def get_max_context_tokens(self) -> int:
        return self._ctx_tokens
