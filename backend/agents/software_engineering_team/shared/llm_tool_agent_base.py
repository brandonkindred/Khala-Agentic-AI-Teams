"""Dependency-light base shared by the LLM tool-agent classes.

Holds the ``_agent_factory`` monkeypatch resolver, an opt-in class-attribute
parameterized model-resolution step, and an opt-in fallback-handling step
(no-model / call-error / empty-parse, plus partial-failure-tolerant calls).
Deliberately imports nothing from ``code_review_agent`` so it can be depended
on from any team without pulling in the code-review engine. LLM invocation and
JSON parsing remain out of scope here; fallback helpers are available capability
and are not auto-wired into subclasses.

Preconditions:
    None beyond standard Python import semantics.

Postconditions:
    Importing this module never triggers an import of ``code_review_agent``
    (verified by ``tests/test_llm_tool_agent_base.py``).

Invariants:
    ``LlmToolAgentBase`` always stores ``self.llm``. When ``resolve_models`` is
    true it also stores ``self._model``, and ``self._model_json`` when
    ``uses_json_model`` is true. Fallback helpers never mutate class-attr lists
    shared across instances.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Literal, Optional, Sequence, Tuple, Union

FallbackTier = Literal["no_model", "call_error", "empty_parse"]


@dataclass(frozen=True)
class FallbackPayload:
    """Generic fallback result for callers to wrap into their own output types.

    Preconditions:
        ``tier`` is one of the three taxonomy labels; ``recommendations`` is a
        list (copied by helpers before construction).

    Postconditions:
        Immutable; ``recommendations`` and ``summary`` are safe to read without
        mutating shared class state.
    """

    tier: FallbackTier
    recommendations: List[str]
    summary: str


class LlmToolAgentBase:
    """Bare constructor, shared ``_agent_factory``, model resolution, and fallbacks.

    Subclasses opt into resolution by setting ``resolve_models = True`` and
    (when needed) overriding ``response_format``, ``uses_json_model``, and/or
    ``get_strands_model_fn``.

    Fallback helpers are call-site opt-in: override the Plan-shaped class-attr
    vocabulary and invoke ``_fallback_no_model``, ``_call_with_single_fallback``,
    ``_call_partial_tolerant``, and/or ``_fallback_empty_parse``. Nothing in
    ``__init__`` enables them automatically.

    Recipes:
        Review-like — ``resolve_models = True`` (defaults give text mode; set
        ``uses_json_model = True`` for a second JSON-mode model).
        Plan/Json-like — ``resolve_models = True``, ``response_format = "json"``,
        ``get_strands_model_fn = <callable>``.

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
        is not ``None``. Fallback list attrs are copied before return.
    """

    resolve_models: bool = False
    response_format: str = "text"
    uses_json_model: bool = False
    get_strands_model_fn: Optional[Callable[..., Any]] = None

    # Plan-shaped fallback vocabulary (subclasses override; helpers copy lists).
    no_model_recommendations: List[str] = []
    no_model_summary: str = ""
    llm_error_recommendations: List[str] = []
    llm_error_summary: str = ""
    empty_recommendations: List[str] = []
    default_summary: str = ""
    empty_summary_override: Optional[str] = None

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

    def _fallback_no_model(self, model: Any) -> Optional[FallbackPayload]:
        """Return the no-model payload when ``model`` is falsy; else ``None``.

        Preconditions:
            None.

        Postconditions:
            Falsy ``model`` yields ``tier="no_model"`` with a copy of
            ``no_model_recommendations`` and ``no_model_summary``. Truthy
            ``model`` yields ``None``. Does not log.
        """
        if model:
            return None
        return FallbackPayload(
            tier="no_model",
            recommendations=list(type(self).no_model_recommendations),
            summary=type(self).no_model_summary,
        )

    def _call_with_single_fallback(
        self,
        fn: Callable[[], Any],
        *,
        log_label: str = "",
    ) -> Union[Tuple[Literal["ok"], Any], Tuple[Literal["error"], FallbackPayload]]:
        """Run ``fn`` once; on ``Exception`` return the call-error fallback.

        Preconditions:
            ``fn`` is a zero-argument callable.

        Postconditions:
            Success → ``("ok", fn())``. Any ``Exception`` → warning log on the
            subclass module logger, then ``("error", FallbackPayload)`` with
            ``tier="call_error"`` and the ``llm_error_*`` class attrs (lists
            copied).
        """
        try:
            return ("ok", fn())
        except Exception as e:
            label = log_label or type(self).__name__
            logging.getLogger(type(self).__module__).warning("%s LLM call failed: %s", label, e)
            return (
                "error",
                FallbackPayload(
                    tier="call_error",
                    recommendations=list(type(self).llm_error_recommendations),
                    summary=type(self).llm_error_summary,
                ),
            )

    def _call_partial_tolerant(
        self,
        items: Iterable[Any],
        fn: Callable[[Any], Any],
        *,
        log_label: str = "",
    ) -> List[Any]:
        """Map ``fn`` over ``items``, skipping items that raise ``Exception``.

        Preconditions:
            ``items`` is iterable; ``fn`` accepts one item.

        Postconditions:
            Returns a list of successful ``fn(item)`` results in encounter order.
            Failures are logged at warning and omitted. Does not build a
            ``FallbackPayload``.
        """
        label = log_label or type(self).__name__
        logger = logging.getLogger(type(self).__module__)
        successes: List[Any] = []
        for item in items:
            try:
                successes.append(fn(item))
            except Exception as e:
                context = str(item)
                if len(context) > 50:
                    context = context[:50]
                logger.warning("%s item failed (%s): %s", label, context, e)
        return successes

    def _fallback_empty_parse(
        self,
        *,
        recommendations: Optional[Sequence[str]] = None,
        summary: Optional[str] = None,
    ) -> FallbackPayload:
        """Apply empty-parse tier messages to recommendations and summary.

        Preconditions:
            ``recommendations``, when provided, is a sequence of strings.

        Postconditions:
            Returns ``tier="empty_parse"``. Empty or ``None`` recommendations
            become a copy of ``empty_recommendations``. Summary starts as
            ``summary`` when not ``None``, else ``default_summary``; if that
            value is falsy and ``empty_summary_override`` is not ``None``, the
            override is used. Does not log.
        """
        cls = type(self)
        if recommendations:
            resolved_recs = list(recommendations)
        else:
            resolved_recs = list(cls.empty_recommendations)

        resolved_summary = cls.default_summary if summary is None else summary
        if not resolved_summary and cls.empty_summary_override is not None:
            resolved_summary = cls.empty_summary_override

        return FallbackPayload(
            tier="empty_parse",
            recommendations=resolved_recs,
            summary=resolved_summary,
        )
