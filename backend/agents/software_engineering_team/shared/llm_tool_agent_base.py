"""Dependency-light base shared by the LLM tool-agent classes.

Holds the ``_agent_factory`` monkeypatch resolver, an opt-in, class-attribute
parameterized model-resolution step, an opt-in parameterized LLM invocation
step (inline vs ``run_strands_agent``), and an opt-in parameterized JSON-
parsing step (lenient vs extract). Deliberately imports nothing from
``code_review_agent`` so it can be depended on from any team without pulling
in the code-review engine.

Preconditions:
    None beyond standard Python import semantics.

Postconditions:
    Importing this module never triggers an import of ``code_review_agent``
    (verified by ``tests/test_llm_tool_agent_base.py``).

Invariants:
    ``LlmToolAgentBase`` always stores ``self.llm``. When ``resolve_models`` is
    true it also stores ``self._model``, and ``self._model_json`` when
    ``uses_json_model`` is true.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict, Optional


class LlmToolAgentBase:
    """Bare constructor, shared ``_agent_factory``, opt-in model resolution,
    and opt-in LLM invocation.

    Subclasses opt into resolution by setting ``resolve_models = True`` and
    (when needed) overriding ``response_format``, ``uses_json_model``, and/or
    ``get_strands_model_fn``.

    Subclasses opt into the ``run_strands_agent`` wrapper by setting
    ``use_run_strands_agent = True``; the default keeps the inline
    ``str(agent(prompt)).strip()`` call.

    Recipes:
        Review-like — ``resolve_models = True`` (defaults give text mode; set
        ``uses_json_model = True`` for a second JSON-mode model);
        ``use_run_strands_agent = True``.
        Plan/Json-like — ``resolve_models = True``, ``response_format = "json"``,
        ``get_strands_model_fn = <callable>``; leave
        ``use_run_strands_agent`` false for the inline path.
        Review JSON parse — ``json_parse_strategy = "lenient"``,
        ``review_parse_mode = "json"`` (failure → ``{}``).
        Review text parse — ``json_parse_strategy = "lenient"``,
        ``review_parse_mode = "text"``, ``_parse_review = <callable>``.
        Plan/Json extract parse — ``json_parse_strategy = "extract"``
        (failure → ``None``).

    Preconditions:
        ``llm``, if provided, is whatever ``resolve_strands_model`` accepts
        (Strands ``Model``, ``LLMClient``, or ``None``).

    Postconditions:
        ``self.llm`` holds the constructor argument. If ``resolve_models`` is
        true, ``self._model`` is set; if ``uses_json_model`` is also true,
        ``self._model_json`` is set.

    Invariants:
        Resolution runs only when ``resolve_models`` is true. The
        ``get_strands_model_fn`` kwarg is forwarded only when the class attr
        is not ``None``. Invocation uses ``run_strands_agent`` only when
        ``use_run_strands_agent`` is true.
    """

    resolve_models: bool = False
    response_format: str = "text"
    uses_json_model: bool = False
    get_strands_model_fn: Optional[Callable[..., Any]] = None
    use_run_strands_agent: bool = False
    json_parse_strategy: str = "lenient"  # "lenient" | "extract"
    review_parse_mode: str = "json"  # "json" | "text"; only for lenient
    parse_context: str = ""
    parse_on_fail_msg: str = "reporting empty result."
    _parse_review: Optional[Callable[[str], Dict[str, Any]]] = None

    def __init__(self, llm=None) -> None:
        self.llm = llm
        if not self.resolve_models:
            return

        from llm_service.strands_model import resolve_strands_model

        get_strands_model_fn = type(self).get_strands_model_fn
        resolve_kwargs: dict[str, Any] = {"response_format": self.response_format}
        if get_strands_model_fn is not None:
            resolve_kwargs["get_strands_model_fn"] = get_strands_model_fn

        self._model = resolve_strands_model(llm, **resolve_kwargs)

        if self.uses_json_model:
            json_kwargs: dict[str, Any] = {"response_format": "json"}
            if get_strands_model_fn is not None:
                json_kwargs["get_strands_model_fn"] = get_strands_model_fn
            self._model_json = resolve_strands_model(llm, **json_kwargs)

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass's defining module.

        This is what lets ``monkeypatch.setattr(<agent_module>, "Agent", ...)``
        intercept LLM calls made from this shared base.

        Preconditions:
            ``type(self).__module__`` names a module that defines an ``Agent``
            symbol (patched in tests, or the real Strands ``Agent`` in
            production).

        Postconditions:
            Returns the ``Agent`` symbol from that module.
        """
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")

    def _invoke_llm(self, model, prompt: str) -> str:
        """Run a one-shot LLM call via the selected invocation path.

        When ``use_run_strands_agent`` is true, delegates to
        ``llm_service.strands_model.run_strands_agent`` (matching today's
        review-agent wrapper path). Otherwise uses the inline
        ``str(agent(prompt)).strip()`` call (matching today's plan/json
        generators).

        Preconditions:
            ``self._agent_factory()(model=model)`` returns a callable that
            accepts ``prompt`` and returns a value coercible with ``str``.

        Postconditions:
            Returns the stripped string result. Exceptions from building or
            running the agent propagate unchanged.
        """
        if self.use_run_strands_agent:
            from llm_service.strands_model import run_strands_agent

            return run_strands_agent(self._agent_factory(), model, prompt)
        return str(self._agent_factory()(model=model)(prompt)).strip()

    def _parse_llm_json(self, raw: str) -> Optional[Dict[str, Any]]:
        """Parse model output via the selected JSON-salvage strategy.

        When ``json_parse_strategy`` is ``"extract"``, delegates to
        ``shared.llm_recovery.extract_json_object`` (failure → ``None``).
        When ``"lenient"`` and ``review_parse_mode == "text"``, calls
        ``self._parse_review(raw)``. Otherwise uses
        ``tool_agent_base.lenient_json_object`` (failure → ``{}``).

        Preconditions:
            ``raw`` is a ``str``. ``json_parse_strategy`` is ``"lenient"`` or
            ``"extract"``. If strategy is ``"lenient"``, ``review_parse_mode``
            is ``"json"`` or ``"text"``. If mode is ``"text"``,
            ``_parse_review`` is not ``None``.

        Postconditions:
            Returns a ``dict`` for lenient/text paths (``{}`` on lenient JSON
            failure). Returns ``dict | None`` for extract (``None`` on failure).
            Does not import ``tool_agent_base`` or ``shared.llm_recovery`` until
            the corresponding branch runs.
        """
        strategy = type(self).json_parse_strategy
        assert strategy in ("lenient", "extract"), strategy

        if strategy == "extract":
            from shared.llm_recovery import extract_json_object

            return extract_json_object(raw)

        mode = type(self).review_parse_mode
        assert mode in ("json", "text"), mode

        if mode == "text":
            parse_review = type(self)._parse_review
            assert parse_review is not None, "_parse_review required for text mode"
            return parse_review(raw)

        from software_engineering_team.shared.tool_agent_base import lenient_json_object

        return lenient_json_object(
            raw,
            logger=logging.getLogger(type(self).__module__),
            context=self.parse_context,
            on_fail_msg=self.parse_on_fail_msg,
        )
