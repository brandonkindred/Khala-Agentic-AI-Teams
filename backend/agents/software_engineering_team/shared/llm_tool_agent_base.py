"""Dependency-light base shared by the LLM tool-agent classes.

Holds the ``_agent_factory`` monkeypatch resolver, an opt-in class-attribute
parameterized model-resolution step, an opt-in parameterized LLM invocation
step (inline vs ``run_strands_agent``), an opt-in parameterized JSON-parsing
step (lenient vs extract), and an opt-in fallback-handling step (no-model /
call-error / empty-parse, plus partial-failure-tolerant calls). Deliberately
imports nothing from ``code_review_agent`` so it can be depended on from any
team without pulling in the code-review engine. Fallback helpers are available
as capabilities but are not auto-wired into subclasses.

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

import hashlib
import importlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence, Tuple, Union

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
    """Bare constructor, shared ``_agent_factory``, model resolution, invocation,
    JSON parsing, and fallbacks.

    Subclasses opt into resolution by setting ``resolve_models = True`` and
    (when needed) overriding ``response_format``, ``uses_json_model``, and/or
    ``get_strands_model_fn``.

    Subclasses opt into the ``run_strands_agent`` wrapper by setting
    ``use_run_strands_agent = True``; the default keeps the inline
    ``str(agent(prompt)).strip()`` call.

    Fallback helpers are call-site opt-in: override the Plan-shaped class-attr
    vocabulary and invoke ``_fallback_no_model``, ``_call_with_single_fallback``,
    ``_call_partial_tolerant``, and/or ``_fallback_empty_parse``. Nothing in
    ``__init__`` enables them automatically.

    Caching is likewise call-site opt-in: set ``cache_namespace`` and
    ``cache_capacity_env`` (and, optionally, ``cache_default_capacity``) and
    call ``_cached_invoke_llm`` instead of ``_invoke_llm`` directly. Leaving
    ``cache_namespace`` unset (the default) disables caching entirely, so
    subclasses that never opt in are byte-for-byte unaffected.

    Recipes:
        Review-like — ``resolve_models = True`` (defaults:
        ``response_format="text"``, ``uses_json_model=False``,
        ``use_run_strands_agent=False``; set ``uses_json_model = True`` for a
        second JSON-mode model); ``use_run_strands_agent = True``.
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
        ``use_run_strands_agent`` is true. Fallback list attrs are copied
        before return.
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

    # Plan-shaped fallback vocabulary (subclasses override; helpers copy lists).
    no_model_recommendations: List[str] = []
    no_model_summary: str = ""
    llm_error_recommendations: List[str] = []
    llm_error_summary: str = ""
    empty_recommendations: List[str] = []
    default_summary: str = ""
    empty_summary_override: Optional[str] = None

    # Cache vocabulary (opt-in; see ``_cached_invoke_llm``). ``cache_namespace``
    # unset disables caching regardless of the other two attrs.
    cache_namespace: Optional[str] = None
    cache_capacity_env: Optional[str] = None
    cache_default_capacity: int = 256

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

        Tests must ``monkeypatch.setattr(<subclass_module>, "Agent", ...)`` on
        the *subclass's* module to intercept LLM calls — patching ``Agent`` on
        this base class's module (``llm_tool_agent_base``) has no effect,
        since resolution always uses ``type(self).__module__``.

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
            running the agent propagate unchanged on both paths:
            ``run_strands_agent`` (used when ``use_run_strands_agent`` is
            true) is a thin passthrough with no internal retry or exception
            handling, so it behaves identically to the inline call for
            propagation purposes.
        """
        if self.use_run_strands_agent:
            from llm_service.strands_model import run_strands_agent

            return run_strands_agent(self._agent_factory(), model, prompt)
        return str(self._agent_factory()(model=model)(prompt)).strip()

    def _cache_namespace(self) -> Optional[str]:
        """Shared-cache namespace for this class's cached LLM calls, or ``None``.

        Preconditions:
            None.

        Postconditions:
            Returns ``None`` when ``cache_namespace`` is unset (caching
            disabled). Otherwise returns ``cache_namespace`` suffixed with the
            deploy build id via ``shared.cache.with_cache_build_id``, so a
            deploy naturally starts with a cold cache.
        """
        ns = type(self).cache_namespace
        if not ns:
            return None
        from shared.cache import with_cache_build_id  # noqa: PLC0415

        return with_cache_build_id(ns)

    def _cache_capacity(self) -> int:
        """Resolve this class's cache capacity from its configured env var.

        Preconditions:
            None.

        Postconditions:
            Returns 0 (cache disabled) when ``cache_capacity_env`` is unset.
            Otherwise returns the env var parsed as an int and clamped to a
            floor of 0: unset/unparseable falls back to
            ``cache_default_capacity``, a negative value clamps to 0. 0
            (explicit or clamped) disables the cache.
        """
        cls = type(self)
        if not cls.cache_capacity_env:
            return 0
        from shared.env_config import env_int  # noqa: PLC0415

        return env_int(cls.cache_capacity_env, cls.cache_default_capacity, 0)

    def _cache_key(self, model: Any, prompt: str) -> str:
        """Hash of this agent's identity, the resolved model, and the rendered prompt.

        By the time a caller has a ``prompt`` string to invoke the LLM with,
        whatever ``phase_inp`` shape produced it (``current_files`` +
        ``task_description``, or something else entirely) has already been
        flattened into that string — so keying on the prompt itself (rather
        than on ``phase_inp`` fields) generalizes across every tool-agent
        kind without per-kind key-building logic.

        Preconditions:
            ``model`` is a resolved Strands model (or any object
            ``llm_service.strands_model.model_fingerprint`` accepts).
            ``prompt`` is a str.

        Postconditions:
            Returns a stable hex digest: identical (class, model, prompt)
            always yields the same key; any change to any of the three
            changes the key.
        """
        from llm_service.strands_model import model_fingerprint  # noqa: PLC0415

        payload = {
            "agent": f"{type(self).__module__}.{type(self).__qualname__}",
            "model": model_fingerprint(model),
            "prompt": prompt,
        }
        body = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _cached_invoke_llm(self, model: Any, prompt: str) -> str:
        """Cache-checked ``_invoke_llm``: a hit skips the LLM call entirely.

        Preconditions:
            Same as ``_invoke_llm``.

        Postconditions:
            When caching is disabled (``cache_namespace`` unset,
            ``cache_capacity_env`` unset, or the resolved capacity is
            ``<= 0``), delegates to ``_invoke_llm`` directly — identical to
            calling it without this wrapper. Otherwise: a cache hit returns
            the previously cached string without invoking the LLM. A cache
            miss, a corrupt cache entry, or any cache backend error falls
            open to a genuine ``_invoke_llm`` call — never raises for a
            cache failure, and the fallen-open call's own exceptions (e.g. an
            LLM provider error) propagate unchanged to the caller, exactly as
            an uncached ``_invoke_llm`` call would. Only a successful
            ``_invoke_llm`` result is written back to the cache.
        """
        namespace = self._cache_namespace()
        capacity = self._cache_capacity() if namespace is not None else 0
        if namespace is None or capacity <= 0:
            return self._invoke_llm(model, prompt)

        from shared.cache import get_shared_cache  # noqa: PLC0415

        logger = logging.getLogger(type(self).__module__)
        cache = get_shared_cache(namespace)
        key = self._cache_key(model, prompt)

        try:
            raw = cache.get(key)
        except Exception:
            logger.warning(
                "%s: cache get failed; treating as miss", type(self).__name__, exc_info=True
            )
            raw = None

        if raw is not None:
            try:
                return raw.decode("utf-8")
            except Exception:
                logger.warning(
                    "%s: corrupt cache entry for %s; evicting",
                    type(self).__name__,
                    key,
                    exc_info=True,
                )
                try:
                    cache.delete(key)
                except Exception:
                    logger.warning(
                        "%s: cache delete failed after corrupt entry",
                        type(self).__name__,
                        exc_info=True,
                    )

        result = self._invoke_llm(model, prompt)

        try:
            cache.set(key, result.encode("utf-8"), max_entries=capacity)
        except Exception:
            logger.warning(
                "%s: cache set failed; continuing without cache write",
                type(self).__name__,
                exc_info=True,
            )

        return result

    def _parse_llm_json(
        self,
        raw: str,
        *,
        context: Optional[str] = None,
        on_fail_msg: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Parse model output via the selected JSON-salvage strategy.

        When ``json_parse_strategy`` is ``"extract"``, delegates to
        ``software_engineering_team.shared.json_utils.parse_json_object``
        (failure → ``None``). When ``"lenient"`` and
        ``review_parse_mode == "text"``, calls
        ``type(self)._parse_review(raw)``. Otherwise uses
        ``tool_agent_base.lenient_json_object`` (failure → ``{}``).

        Preconditions:
            ``raw`` is a ``str``. ``json_parse_strategy`` is ``"lenient"`` or
            ``"extract"``. If strategy is ``"lenient"``, ``review_parse_mode``
            is ``"json"`` or ``"text"``. If mode is ``"text"``,
            ``_parse_review`` is not ``None``.

        Postconditions:
            Returns a ``dict`` for lenient/text paths (``{}`` on lenient JSON
            failure). Returns ``dict | None`` for extract (``None`` on failure).
            Does not import ``software_engineering_team.shared.json_utils`` or
            ``tool_agent_base.lenient_json_object`` until the corresponding
            branch runs. For the lenient-JSON branch, ``context``/``on_fail_msg``
            are forwarded to ``lenient_json_object`` when given; otherwise the
            call falls back to ``self.parse_context``/``self.parse_on_fail_msg``
            (the class-level defaults), so callers that only rely on those
            defaults need not pass either kwarg.
        """
        strategy = type(self).json_parse_strategy
        assert strategy in ("lenient", "extract"), strategy

        if strategy == "extract":
            from llm_service import LLMJsonParseError
            from software_engineering_team.shared.json_utils import parse_json_object

            try:
                return parse_json_object(raw)
            except (LLMJsonParseError, TypeError):
                return None

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
            context=context if context is not None else self.parse_context,
            on_fail_msg=on_fail_msg if on_fail_msg is not None else self.parse_on_fail_msg,
        )

    # Fallback helpers read class attrs via type(self), not self: that keeps
    # unbound callables from being bound as methods and avoids mutating shared
    # class-level list defaults through an instance attribute.
    def _fallback_no_model(self, model: Any) -> Optional[FallbackPayload]:
        """Return the no-model payload when ``model`` is falsy; else ``None``.

        Preconditions:
            None.

        Postconditions:
            Any falsy ``model`` (``None``, ``False``, ``0``, ``[]``, ``""``,
            etc.) yields ``tier="no_model"`` with a copy of
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
            Success → ``("ok", fn())``. Any ``Exception`` (not ``BaseException``) →
            warning log on the subclass module logger, then ``("error", FallbackPayload)`` with
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
            Any ``Exception`` raised by ``fn`` is treated as a failure: it is
            logged at warning and the item is omitted. Does not build a
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
            ``summary`` when not ``None`` (even if an empty string), else
            ``default_summary``; if that value is falsy and
            ``empty_summary_override`` is not ``None``, the override is used.
            Does not log.
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
