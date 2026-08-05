"""Tests for DbcCommentsAgent's chunking/context-size bounding.

Covers the acceptance criteria for context-size bounding: large-file
chunking (a big multi-file ``code`` input is split into multiple bounded LLM
calls and the results merged), a malformed response on a later chunk (still
fails the whole run loud, not just that chunk), and insertion conflicts
arising across chunks (the existing merge-step duplicate-target rejection
still applies when insertions come from more than one LLM call). Reuses the
real ``code_review_agent`` chunking utilities directly (not mocked) so the
expected chunk count is derived the same way ``DbcCommentsAgent.run`` derives
it -- these tests do not hard-code a char budget.
"""

from __future__ import annotations

from typing import Any, Dict, List, Union

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.code_review_agent.chunking import build_review_chunks
from software_engineering_team.shared.chunking import parse_code_into_file_blocks
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars
from software_engineering_team.technical_writers.dbc_comments_agent import agent as dbc_mod
from software_engineering_team.technical_writers.dbc_comments_agent.agent import (
    DbcCommentsAgent,
)
from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentsInput,
    DbcCommentsStatus,
)

_FILE_COUNT = 10
_PADDING_LINES = 200


class _SequencedClient(DummyLLMClient):
    """Returns/raises a different scripted response per call, in order."""

    def __init__(self, responses: List[Union[BaseException, Dict[str, Any]]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append(prompt)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _RepeatingClient(DummyLLMClient):
    """Returns the same canned payload on every call."""

    def __init__(self, canned: Dict[str, Any]) -> None:
        super().__init__()
        self._canned = canned
        self.calls: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append(prompt)
        return self._canned


def _large_multi_file_code() -> str:
    """A multi-file ``code`` string large enough to force multiple chunks.

    Postconditions:
        - Each file parses as valid Python with a single top-level ``foo``
          function, so a DbC insertion targeting (path, "foo") always
          resolves via merge.apply_dbc_insertions's AST-based anchoring.
        - Total size comfortably exceeds compute_code_review_map_chunk_chars's
          budget for a DummyLLMClient, so build_review_chunks always returns
          more than one chunk regardless of minor constant tuning.
    """
    padding = "\n".join(f"# padding line {i}" for i in range(_PADDING_LINES))
    files = []
    for i in range(_FILE_COUNT):
        files.append(f"### file{i}.py ###\ndef foo():\n    pass\n\n\n{padding}\n")
    return "".join(files)


def _expected_chunks():
    """The real chunks DbcCommentsAgent.run will build for _large_multi_file_code().

    Uses a DummyLLMClient's context budget (get_max_context_tokens) directly,
    exactly matching what DbcCommentsAgent.run itself computes.
    """
    code = _large_multi_file_code()
    blocks = parse_code_into_file_blocks(code)
    max_chars = compute_code_review_map_chunk_chars(DummyLLMClient())
    return code, build_review_chunks(blocks, max_chars)


def test_dbc_run_large_input_is_chunked_into_multiple_bounded_llm_calls() -> None:
    """A large multi-file input must be split into more than one chunk -- no
    single unbounded prompt carrying the whole input is ever sent."""
    code, chunks = _expected_chunks()
    assert len(chunks) > 1, "test fixture must be large enough to force chunking"

    responses = [
        {
            "insertions": [
                {"file": chunk.segments[0].path, "symbol": "foo", "comment": '"""Does nothing."""'}
            ],
            "already_compliant": False,
            "summary": f"chunk {i + 1} reviewed",
        }
        for i, chunk in enumerate(chunks)
    ]
    client = _SequencedClient(responses)
    out = DbcCommentsAgent(llm_client=client).run(DbcCommentsInput(code=code))

    assert len(client.calls) == len(chunks)
    # No single call carried the whole input -- each call's prompt is smaller
    # than the full concatenated code.
    assert all(len(call) < len(code) for call in client.calls)
    assert out.already_compliant is False
    assert len(out.insertions) == len(chunks)
    # Every insertion targeted a distinct (file, symbol), so all merge cleanly.
    assert out.comments_added == len(chunks)
    assert out.rejected_insertions == []
    assert len(out.files) == len(chunks)


def test_dbc_run_merges_non_conflicting_insertions_across_chunks() -> None:
    """Insertions returned by different chunks for different files are all
    preserved in the merged output (not overwritten by later chunks)."""
    code, chunks = _expected_chunks()
    assert len(chunks) > 1

    responses = [
        {
            "insertions": [
                {"file": chunk.segments[0].path, "symbol": "foo", "comment": '"""Does nothing."""'}
            ],
            "already_compliant": False,
        }
        for chunk in chunks
    ]
    client = _SequencedClient(responses)
    out = DbcCommentsAgent(llm_client=client).run(DbcCommentsInput(code=code))

    merged_paths = set(out.files.keys())
    expected_paths = {chunk.segments[0].path for chunk in chunks}
    assert merged_paths == expected_paths


def test_dbc_run_insertion_conflict_across_chunks_is_rejected() -> None:
    """Two different chunks proposing an insertion for the SAME (file, symbol)
    is a conflict merge.apply_dbc_insertions already detects for a single
    batch -- confirm it still applies when the insertions arrive from
    multiple LLM calls, not silently double-applying or corrupting anything."""
    code, chunks = _expected_chunks()
    assert len(chunks) > 1
    target_path = chunks[0].segments[0].path

    canned = {
        "insertions": [{"file": target_path, "symbol": "foo", "comment": '"""Does nothing."""'}],
        "already_compliant": False,
    }
    client = _RepeatingClient(canned)
    out = DbcCommentsAgent(llm_client=client).run(DbcCommentsInput(code=code))

    assert len(client.calls) == len(chunks)
    # Raw insertions are all kept for observability...
    assert len(out.insertions) == len(chunks)
    # ...but only the first application to a given target is accepted; the
    # rest are rejected as duplicates, never silently double-applied.
    assert out.comments_added == 1
    assert len(out.rejected_insertions) == len(chunks) - 1


def test_dbc_run_insertion_conflict_forces_non_compliant_even_if_chunks_said_compliant() -> None:
    """A rejected insertion (here, a duplicate target across chunks) must
    force already_compliant=False even when every chunk itself reported
    already_compliant=True -- a fix the model identified but that could not
    actually be applied means the code is not yet fully compliant,
    regardless of what any individual chunk claimed."""
    code, chunks = _expected_chunks()
    assert len(chunks) > 1
    target_path = chunks[0].segments[0].path

    canned = {
        "insertions": [{"file": target_path, "symbol": "foo", "comment": '"""Does nothing."""'}],
        "already_compliant": True,
    }
    client = _RepeatingClient(canned)
    out = DbcCommentsAgent(llm_client=client).run(DbcCommentsInput(code=code))

    assert len(out.rejected_insertions) == len(chunks) - 1
    assert out.already_compliant is False


def test_dbc_run_malformed_response_on_a_later_chunk_fails_the_whole_run() -> None:
    """A persistently malformed reply on chunk 2 must fail the WHOLE run
    loud, even though chunk 1 already succeeded -- an earlier chunk's
    success can never let a later chunk's persistent failure slip through as
    silently compliant, and a later chunk (3+) must never be reached.

    complete_validated applies its own internal schema-correction retry on a
    malformed-but-parseable payload (missing a required field), so a single
    outer attempt can itself consume more than one complete_json call --
    this supplies generously more malformed entries than the worst case
    needs rather than pinning an exact internal call count, which is
    complete_validated's own implementation detail."""
    code, chunks = _expected_chunks()
    assert len(chunks) > 1

    good_payload = {"insertions": [], "already_compliant": True, "summary": "chunk 1 ok"}
    malformed_payload = {"insertions": []}  # missing required "already_compliant"
    responses = [good_payload] + [malformed_payload] * 8
    client = _SequencedClient(responses)

    statuses: List[DbcCommentsStatus] = []
    out = DbcCommentsAgent(llm_client=client).run(
        DbcCommentsInput(code=code), on_status=lambda s, d: statuses.append(s)
    )

    # The acceptance criterion: a persistent failure fails the whole run
    # loud, surfaced via NEEDS_RETRY-then-FAILED status callbacks. Not
    # asserted against the summary text or an exact call count -- both are
    # complete_validated's own implementation detail, not part of the
    # contract this test is verifying.
    assert out.already_compliant is False
    assert statuses.count(DbcCommentsStatus.NEEDS_RETRY) >= 1
    assert DbcCommentsStatus.FAILED in statuses
    assert statuses.index(DbcCommentsStatus.NEEDS_RETRY) < statuses.index(DbcCommentsStatus.FAILED)
    # A later chunk (3+) is never reached: only `responses` were queued (8
    # malformed entries -- generously more than any retry budget needs), and
    # no chunk-3-shaped response exists beyond them, so exhausting the run
    # without an IndexError from _SequencedClient proves chunk 3 was never
    # called.
    assert len(client.calls) < len(responses)


def test_dbc_run_small_input_still_yields_exactly_one_chunk() -> None:
    """A small input must not be chunked at all -- the common case stays a
    single LLM call, matching pre-#4373 behavior exactly."""
    code = "def f():\n    pass\n"
    blocks = parse_code_into_file_blocks(code)
    max_chars = compute_code_review_map_chunk_chars(DummyLLMClient())
    chunks = build_review_chunks(blocks, max_chars)
    assert len(chunks) == 1

    canned = {"insertions": [], "already_compliant": True, "summary": "ok"}
    client = _RepeatingClient(canned)
    out = DbcCommentsAgent(llm_client=client).run(DbcCommentsInput(code=code))

    assert len(client.calls) == 1
    assert out.already_compliant is True


def test_dbc_run_chunk_preparation_failure_fails_loud(monkeypatch) -> None:
    """A failure while preparing chunks (e.g. compute_code_review_map_chunk_chars
    hitting the network via self.llm.get_max_context_tokens()) must fail the
    whole run loud, the same as an exhausted per-chunk LLM call -- never a
    silent fail-open, and the LLM is never called at all since there's no
    chunk yet to review."""

    def _boom(llm):
        raise RuntimeError("context lookup failed")

    monkeypatch.setattr(dbc_mod, "compute_code_review_map_chunk_chars", _boom)

    client = _RepeatingClient({"insertions": [], "already_compliant": True})
    statuses: List[DbcCommentsStatus] = []
    out = DbcCommentsAgent(llm_client=client).run(
        DbcCommentsInput(code="def f(): pass"), on_status=lambda s, d: statuses.append(s)
    )

    assert out.already_compliant is False
    assert "preparing chunks" in out.summary
    assert DbcCommentsStatus.FAILED in statuses
    assert client.calls == []


def test_dbc_run_dedupes_identical_summaries_across_chunks() -> None:
    """Multiple chunks reporting the identical summary text collapse to one
    occurrence in the joined output, instead of repeating verbatim."""
    code, chunks = _expected_chunks()
    assert len(chunks) > 1

    responses = [
        {"insertions": [], "already_compliant": True, "summary": "No changes needed."}
        for _ in chunks
    ]
    client = _SequencedClient(responses)
    out = DbcCommentsAgent(llm_client=client).run(DbcCommentsInput(code=code))

    assert out.summary == "No changes needed."
    assert out.summary.count("No changes needed.") == 1
