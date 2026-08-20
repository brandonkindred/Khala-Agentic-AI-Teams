"""Shared test-double LLM clients for coordinator-level fallback review tests.

Extracted from ``test_v2_review_fallback_e2e.py`` and
``test_v2_fe_review_fallback_e2e.py`` to eliminate near-verbatim duplication
(see GitHub issue #6791). The only meaningful difference between the backend
and frontend copies was the file extension used in prompt markers and issue
payloads (``.py`` vs ``.tsx``); ``FailBadKeepGood`` is parameterized over that
extension, and the rest are extension-agnostic.

Not a test module itself -- its ``_``-prefixed name prevents pytest from
collecting it (same convention as ``_v2_config_fixtures.py``).

Relationship to ``test_code_review_coordinator.py``
---------------------------------------------------
``ScriptedClient`` here is conceptually equivalent to ``_ScriptedClient`` in
``test_code_review_coordinator.py`` (lines ~238-285), and ``FailBadKeepGood``
mirrors that file's ``_FailWhenBadPresent``. However, they are *not* fully
interchangeable: the coordinator-level tests detect reasoning-vs-formatting
passes via the ``_ANALYSIS_DELIMITER`` string (a local constant), whereas the
fallback e2e tests use the shared ``is_chunk_map_reasoning_prompt()`` helper
(which checks for ``CODE_TO_REVIEW_HEADER``). Both approaches correctly
discriminate the two passes but from opposite detection points. Unifying them
further would require either reconciling the detection strategy or abstracting
it behind a callback, which is a larger refactor beyond the scope of issue
#6791.
"""

from __future__ import annotations

import threading
from typing import Any

from tests.chunk_review_prompt_routing import (
    is_chunk_map_reasoning_prompt as _is_chunk_map_reasoning_prompt,
)

from llm_service import LLMSemanticExhaustionError
from llm_service.clients.dummy import DummyLLMClient


class ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each formatting-pass
    ``complete_json`` call.

    A real ``DummyLLMClient`` (not a mock of the coordinator), so it drives
    the coordinator's actual map-reduce pipeline. Adds ``call_count`` so a
    test can assert how many real formatting-pass LLM calls the pipeline made.

    Both the reasoning pass and the formatting pass land on
    ``complete_json`` now (see ``_is_chunk_map_reasoning_prompt``); only the
    formatting-pass call advances the scripted response cursor and
    ``call_count`` -- the reasoning-pass call gets the inherited dummy
    default, whose prose is discarded once wrapped for formatting.

    Exhaustion behavior: once all scripted responses have been served, the
    last response in the list is returned indefinitely (or ``{}`` if the list
    was empty). This keeps tests from crashing due to unexpected extra map
    chunks while still exercising the real coordinator pipeline.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0
        self.call_count = 0
        self._lock = threading.Lock()

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if _is_chunk_map_reasoning_prompt(prompt):
            return super().complete_json(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                tools=tools,
                think=think,
                **kwargs,
            )
        with self._lock:
            self.call_count += 1
            if self._idx < len(self._responses):
                resp = self._responses[self._idx]
                self._idx += 1
                return resp
            return self._responses[-1] if self._responses else {}


class PerFileScriptedClient(DummyLLMClient):
    """Returns a response keyed by which file marker appears in the prompt.

    Unlike ``ScriptedClient`` (which returns responses strictly by call
    order), this binds each canned response to the chunk it actually belongs
    to via the chunk's ``### path ###`` header. Real map-phase calls may run
    concurrently, so call order is not guaranteed to match file order; an
    order-based client would still pass an attribution assertion even if the
    coordinator mixed up which finding came from which file's content, since
    each canned response's ``file_path`` is a hard-coded value independent of
    the prompt it was served to. Selecting by marker closes that gap.

    Markers are matched on the reasoning-pass ``complete_json`` prompt
    (identified via ``_is_chunk_map_reasoning_prompt``); the formatting-pass
    ``complete_json`` wrap does not carry ``### path ###`` headers directly,
    but DOES carry the reasoning pass's own prose verbatim (wrapped in
    ``wrap_with_analysis_delimiters``'s "--- ANALYSIS" markers) -- real
    map-phase calls run the reasoning pass in a Strands ``Agent`` dispatched
    via ``asyncio.to_thread`` (a pooled worker thread) while the formatting
    pass runs synchronously back on the calling thread, so a
    ``threading.local`` cannot bridge state between the two calls for one
    chunk. Instead the reasoning-pass response echoes its own marker back
    (``{"summary": marker}``); ``chat()`` JSON-serializes that into the raw
    prose, which the formatting prompt then wraps verbatim, so the same
    marker is directly re-matchable there with no shared mutable state.

    ``call_count`` counts REASONING-pass calls only (the opposite of
    ``ScriptedClient.call_count`` above, which counts formatting-pass
    calls) -- callers comparing the two must not assume a shared meaning.
    """

    def __init__(self, responses_by_marker: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self._responses_by_marker = dict(responses_by_marker)
        self.call_count = 0
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if not _is_chunk_map_reasoning_prompt(prompt):
            for marker, response in self._responses_by_marker.items():
                if marker in prompt:
                    return response
            return super().complete_json(prompt, **kwargs)
        with self._lock:
            self.call_count += 1
        for marker in self._responses_by_marker:
            if marker in prompt:
                return {"summary": f"Structured prose review summary. {marker}"}
        raise AssertionError(f"no scripted response matches prompt: {prompt[:200]!r}")


class PromptCapturingClient(DummyLLMClient):
    """Records every chunk-review reasoning prompt; always returns a clean pass.

    Both the reasoning pass and the formatting pass land on ``complete_json``
    now (see ``_is_chunk_map_reasoning_prompt``), so recording happens there,
    gated to the reasoning-pass call only. The shared review-context prefix
    (spec excerpt / architecture overview / existing-codebase excerpt --
    see ``chunk_reviewer._build_shared_review_prefix``) is attached to the
    reasoning ``Agent``'s SYSTEM content, not the user-turn prompt, and
    ``DummyLLMClient.chat()`` forwards it to ``complete_json`` as the
    ``system_prompt`` kwarg rather than folding it into ``prompt`` -- so both
    are recorded together to keep this a faithful "what did the reasoning
    call actually see" capture.
    """

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete_json(
        self, prompt: str, *, system_prompt: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if _is_chunk_map_reasoning_prompt(prompt):
            with self._lock:
                self.prompts.append(f"{prompt}\n{system_prompt or ''}")
        return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}


class FailBadKeepGood(DummyLLMClient):
    """Fails any chunk touching a ``bad.<ext>`` file; returns a genuine issue for ``good.<ext>``.

    Parameterized over file extension so the same class serves both backend
    (``.py``) and frontend (``.tsx``) test suites.

    Mirrors ``test_code_review_coordinator.py``'s
    ``test_semantic_exhaustion_multi_file_still_separates_files`` fixture
    (``_FailWhenBadPresent``), scripted with a real finding for the surviving
    file so the translated output is non-empty.

    The reasoning pass (a Strands ``Agent`` dispatched via
    ``asyncio.to_thread``) and the formatting pass (a synchronous call back
    on the calling thread) do not share a thread, so a ``threading.local``
    handoff between them (as a prior version of this double used) silently
    breaks. Instead the reasoning pass's ``good.<ext>`` response echoes a
    marker back; ``chat()`` JSON-serializes it into the raw prose, which the
    formatting prompt wraps verbatim, so the marker is directly
    re-matchable in the formatting-pass prompt with no shared mutable state.

    ``call_count`` counts REASONING-pass calls only (the opposite of
    ``ScriptedClient.call_count`` above, which counts formatting-pass
    calls) -- callers comparing the two must not assume a shared meaning.
    """

    GOOD_FILE_REVIEWED_MARKER = "GOOD_FILE_REVIEWED_MARKER"

    def __init__(self, *, ext: str = "py") -> None:
        super().__init__()
        self._bad_file = f"bad.{ext}"
        self._good_file = f"good.{ext}"
        self.call_count = 0
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if not _is_chunk_map_reasoning_prompt(prompt):
            if self.GOOD_FILE_REVIEWED_MARKER in prompt:
                return {
                    "approved": False,
                    "issues": [
                        {
                            "severity": "high",
                            "category": "logic",
                            "file_path": self._good_file,
                            "description": "real issue",
                            "suggestion": "fix it",
                        }
                    ],
                    "summary": "found one",
                    "spec_compliance_notes": "",
                }
            return super().complete_json(prompt, **kwargs)
        with self._lock:
            self.call_count += 1
        if f"### {self._bad_file} ###" in prompt:
            raise LLMSemanticExhaustionError("no content", retry_thinking_level=False)
        if f"### {self._good_file} ###" in prompt:
            return {"summary": self.GOOD_FILE_REVIEWED_MARKER}
        return super().complete_json(prompt, **kwargs)


class AlwaysFail(DummyLLMClient):
    """Fails every chunk-review call, unconditionally."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self._lock = threading.Lock()

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.call_count += 1
        raise LLMSemanticExhaustionError("no content", retry_thinking_level=False)
