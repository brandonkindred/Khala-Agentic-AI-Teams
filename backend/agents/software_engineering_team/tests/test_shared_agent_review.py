"""Tests for software_engineering_team.shared.agent_review.

These exercise the shared QA/security review orchestration directly (the two V2
team wrappers delegate to it). The team-specific ``ReviewIssue`` type is injected,
so the helper is tested in isolation from any one team's models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from unittest.mock import MagicMock

from software_engineering_team.shared.agent_review import (
    AgentReviewCache,
    _piece_cache_key,
    run_chunked_agent_review,
    run_qa_agent,
    run_security_agent,
)

MAX = 60_000


@dataclass
class _Issue:
    source: str = ""
    severity: str = "medium"
    description: str = ""
    file_path: str = ""
    recommendation: str = ""


class _Bug:
    severity = "low"
    description = "real bug"
    location = "big.py"
    recommendation = ""


def _big_source() -> str:
    """A multi-function file larger than one prompt budget.

    Grows whole ``def fn_*`` functions until the source exceeds MAX, so cuts at
    function boundaries are exercised; begins with fn_0000 and ends with fn_tail.
    """
    lines: list[str] = []
    total = 0
    i = 0
    while total <= MAX:
        fn = f"def fn_{i:04d}():\n    return {i}"
        lines.append(fn)
        total += len(fn) + 1
        i += 1
    lines.append("def fn_tail():\n    return -1")
    big = "\n".join(lines)
    assert len(big) > MAX
    return big


def _is_raw(piece: str) -> bool:
    """True when a piece carries neither a ### path ### header nor N: prefixes."""
    return "### " not in piece and re.search(r"(?m)^\d+: ", piece) is None


def test_function_aware_split_feeds_raw_source():
    """A too-large multi-function file is split at function boundaries and each
    piece is RAW source (no ### header, no N: line prefixes), head and tail kept."""
    codes: list[str] = []

    def run_chunk(code: str):
        codes.append(code)
        return [_Bug()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"big.py": _big_source()},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    assert len(codes) > 1  # split into multiple function-aware pieces
    assert all(len(c) <= MAX for c in codes)  # every piece within budget
    assert all(_is_raw(c) for c in codes)  # raw source, not reviewer-rendered
    assert all(c.lstrip().startswith("def ") for c in codes)  # cuts at function boundaries
    joined = "\n".join(codes)
    assert "fn_0000" in joined and "fn_tail" in joined  # head and tail both reviewed
    assert len(issues) == len(codes)
    assert all(i.source == "qa" for i in issues)


def test_oversized_single_line_hard_splits_raw():
    """A single line longer than the cap (a minified bundle) has no function
    boundary, so it falls back to a character-bounded hard-split — still raw."""
    codes: list[str] = []

    def run_chunk(code: str):
        codes.append(code)
        return []

    line = "DATA = '" + ("a" * (MAX + 5_000)) + "'"
    assert "\n" not in line and len(line) > MAX

    run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"bundle.py": line},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    assert len(codes) > 1
    assert all(len(c) <= MAX for c in codes)
    assert all(_is_raw(c) for c in codes)
    assert "".join(codes) == line  # whole line reconstructs, nothing dropped


def test_small_file_single_call():
    """A file that fits is reviewed in one call (no regression)."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        assert code == "def f():\n    return 1"  # raw, whole file
        return []

    run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"x.py": "def f():\n    return 1"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert calls["n"] == 1


def test_blank_files_trigger_no_call():
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        return []

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"empty.py": "   \n\t"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert issues == [] and calls["n"] == 0


def test_failing_piece_skipped_others_survive():
    """A piece whose run_chunk raises is skipped; the others' issues survive."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("agent unavailable")
        return [_Bug()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"big.py": _big_source()},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=0,  # exercise the many-pieces warning path
    )
    assert calls["n"] > 1
    assert len(issues) >= 1
    assert all(i.description == "real bug" for i in issues)


def test_all_pieces_failing_appends_one_synthetic_issue_not_a_false_clean_list():
    """When every piece of a multi-piece file fails (not just one), exactly one
    synthetic issue is appended -- not one per failed piece, and not the empty
    list that would be indistinguishable from a genuine clean pass. Also
    exercises ``failure_severity``'s default ("critical"), since this call
    doesn't override it."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        raise RuntimeError("agent unavailable")

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"big.py": _big_source()},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=0,  # exercise the many-pieces warning path too
    )
    assert calls["n"] > 1  # confirms this is genuinely a multi-piece file
    assert len(issues) == 1  # one synthetic issue, not one per failed piece
    assert issues[0].severity == "critical"  # failure_severity's default
    assert issues[0].source == "qa"
    assert issues[0].file_path == ""
    assert (
        issues[0].description
        == f"QA agent could not complete review: all {calls['n']} piece(s) failed"
    )


def test_file_path_defaults_to_sent_file_when_agent_gives_none():
    """When the agent reports location=None, the finding is attributed to the
    file actually sent rather than left blank."""

    class _NoLoc:
        severity = "high"
        description = "x"
        location = None
        recommendation = ""

    def run_chunk(code: str):
        return [_NoLoc()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"svc.py": "def f():\n    return 1"},
        source="security",
        default_severity="high",
        label="Security agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert len(issues) == 1
    assert issues[0].file_path == "svc.py"


def test_file_path_prefers_real_path_over_free_text_location():
    """A free-text `location` (e.g. a function name, not a path) must never
    override the file actually sent -- it's folded into the description
    instead, so a downstream exact-match lookup still resolves the real
    file rather than silently missing and falling back to arbitrary files."""

    class _BugWithFreeTextLocation:
        severity = "high"
        description = "SQL injection"
        location = "the login() function"
        file_path = "auth/login.py"
        recommendation = "use parameterized queries"

    def run_chunk(code: str):
        return [_BugWithFreeTextLocation()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"auth/login.py": "def login():\n    pass"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    assert len(issues) == 1
    assert issues[0].file_path == "auth/login.py"
    assert "the login() function" in issues[0].description


def test_file_path_ignores_agents_own_file_path_field_uses_sent_path():
    """`item.file_path` is itself LLM-authored text -- the agent never sees a
    file header (raw source only), so a value that disagrees with the file
    actually sent is presumed hallucinated/mis-formatted, not authoritative.
    Trusting it would reintroduce the same exact-match-lookup-miss bug this
    guards against, just via a different field name."""

    class _BugWithWrongFilePath:
        severity = "high"
        description = "SQL injection"
        location = ""
        file_path = "some/other/file.py"  # disagrees with the file actually sent
        recommendation = ""

    def run_chunk(code: str):
        return [_BugWithWrongFilePath()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"auth/login.py": "def login():\n    pass"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    assert len(issues) == 1
    assert issues[0].file_path == "auth/login.py"
    assert "some/other/file.py" in issues[0].description


def test_file_path_falls_back_to_sent_path_when_item_has_only_free_text_location():
    """SecurityVulnerability only ever carries `location`, never `file_path`.
    A free-text `location` there must resolve to the file actually sent
    (`path`), not the free text -- mirroring the security-agent shape where
    no `file_path` attribute exists on the item at all."""

    class _VulnWithFreeTextLocation:
        severity = "high"
        description = "XSS"
        location = "the render() function"
        recommendation = "sanitize"

    def run_chunk(code: str):
        return [_VulnWithFreeTextLocation()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"x.ts": "const f = () => 1;"},
        source="security",
        default_severity="high",
        label="Security agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    assert len(issues) == 1
    assert issues[0].file_path == "x.ts"
    assert "the render() function" in issues[0].description


def test_run_qa_agent_builds_qa_input_and_extracts_bugs():
    """run_qa_agent feeds raw code to the QA agent and maps bugs_found to issues."""
    codes: list[str] = []

    class _QAAgent:
        def run(self, inp):
            codes.append(inp.code)
            assert inp.language == "python"
            return MagicMock(bugs_found=[_Bug()])

    issues = run_qa_agent(
        qa_agent=_QAAgent(),
        files={"x.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert codes == ["def f():\n    return 1"]
    assert len(issues) == 1 and issues[0].source == "qa"


def test_run_security_agent_builds_security_input_and_extracts_vulns():
    """run_security_agent feeds raw code to the security agent and maps vulns."""
    codes: list[str] = []

    class _Vuln:
        severity = "high"
        description = "XSS"
        location = "x.ts"
        recommendation = "sanitize"

    class _SecAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(vulnerabilities=[_Vuln()])

    issues = run_security_agent(
        security_agent=_SecAgent(),
        files={"x.ts": "const f = () => 1;"},
        language="typescript",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert codes == ["const f = () => 1;"]
    assert len(issues) == 1 and issues[0].source == "security"


# ---------------------------------------------------------------------------
# AgentReviewCache: per-piece verdict cache for run_chunked_agent_review
# ---------------------------------------------------------------------------


def test_agent_review_cache_get_put_returns_independent_copies():
    """get/put never hand out the shared list — mutating a returned list is safe."""
    cache = AgentReviewCache()
    assert cache.get("k") is None

    original = [_Bug()]
    cache.put("k", original)
    original.append(_Bug())  # mutate the caller's list after storing
    assert len(cache.get("k")) == 1  # stored copy is unaffected

    fetched = cache.get("k")
    fetched.append(_Bug())  # mutate the returned list
    assert len(cache.get("k")) == 1  # a second get is unaffected


def test_agent_review_cache_deep_copies_items_so_mutating_a_field_is_safe():
    """get/put copy more than just the list container -- mutating a FIELD on a
    returned/stored item (cached items are mutable objects, e.g. Pydantic
    models) must not corrupt the cached entry."""
    cache = AgentReviewCache()
    bug = _Bug()
    cache.put("k", [bug])

    bug.severity = "mutated-after-put"  # mutate the caller's own object after storing
    assert cache.get("k")[0].severity == "low"  # stored copy is unaffected

    fetched = cache.get("k")
    fetched[0].severity = "mutated-after-get"  # mutate the returned item
    assert cache.get("k")[0].severity == "low"  # a second get is unaffected


def test_piece_cache_key_does_not_collide_across_separator_boundary():
    """A literal NUL byte inside cache_context or piece must not let two
    different (cache_context, piece) pairs hash identically (the flat
    NUL-joined body of "python\x00A"/"B" and "python"/"A\x00B" is otherwise
    the same string)."""
    key_a = _piece_cache_key("qa", "python\x00A", "B")
    key_b = _piece_cache_key("qa", "python", "A\x00B")
    assert key_a != key_b


def test_cache_hit_skips_run_chunk_for_identical_piece():
    """A second call with byte-identical content/context reuses the cached result."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        return [_Bug()]

    cache = AgentReviewCache()
    kwargs = dict(
        run_chunk=run_chunk,
        files={"x.py": "def f():\n    return 1"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
        cache_context="python\x00task",
    )

    first = run_chunked_agent_review(**kwargs)
    second = run_chunked_agent_review(**kwargs)

    assert calls["n"] == 1  # only the first call actually invoked the agent
    assert len(first) == len(second) == 1
    assert first[0].description == second[0].description
    assert first[0].description.startswith("real bug")


def test_cache_misses_on_changed_content():
    """Editing the file's content changes the piece, so the cache does not hit."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        return [_Bug()]

    cache = AgentReviewCache()
    common = dict(
        run_chunk=run_chunk,
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
        cache_context="python\x00task",
    )

    run_chunked_agent_review(files={"x.py": "def f():\n    return 1"}, **common)
    run_chunked_agent_review(files={"x.py": "def f():\n    return 2"}, **common)

    assert calls["n"] == 2  # different content -> both calls hit the agent


def test_cache_misses_on_changed_context():
    """Same content but a different language/task_description misses the cache."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        return [_Bug()]

    cache = AgentReviewCache()
    common = dict(
        run_chunk=run_chunk,
        files={"x.py": "def f():\n    return 1"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    run_chunked_agent_review(cache_context="python\x00task-a", **common)
    run_chunked_agent_review(cache_context="python\x00task-b", **common)

    assert calls["n"] == 2  # different cache_context -> both calls hit the agent


def test_cache_write_does_not_exhaust_a_generator_run_chunk_result():
    """run_chunk may return any iterable, not just a list. Caching its result
    must not consume a one-shot generator before the issue-conversion loop
    below gets a chance to see it."""

    def run_chunk(code: str):
        return (b for b in [_Bug()])  # one-shot: exhausted after a single pass

    cache = AgentReviewCache()
    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"x.py": "def f():\n    return 1"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
        cache_context="python\x00task",
    )

    assert len(issues) == 1  # the generator's item must still reach the issue list
    assert issues[0].description.startswith("real bug")


def test_unchanged_second_file_stays_cached_only_changed_one_is_rereviewed():
    """Two files reviewed together; only the edited one re-hits the agent next round."""
    calls: list[str] = []

    def run_chunk(code: str):
        calls.append(code)
        return [_Bug()]

    cache = AgentReviewCache()
    common = dict(
        run_chunk=run_chunk,
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
        cache_context="python\x00task",
    )

    run_chunked_agent_review(
        files={"a.py": "def a():\n    return 1", "b.py": "def b():\n    return 2"}, **common
    )
    assert len(calls) == 2

    # Simulate a batch-fix round that only rewrote b.py; a.py is byte-identical.
    run_chunked_agent_review(
        files={"a.py": "def a():\n    return 1", "b.py": "def b():\n    return 3"}, **common
    )
    assert len(calls) == 3  # only b.py's new content triggered another agent call


def test_failed_piece_is_not_cached_and_is_retried():
    """A piece whose run_chunk call raises is never cached, so the next call retries it for real."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("agent unavailable")
        return [_Bug()]

    cache = AgentReviewCache()
    kwargs = dict(
        run_chunk=run_chunk,
        files={"x.py": "def f():\n    return 1"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
        cache_context="python\x00task",
    )

    first = run_chunked_agent_review(**kwargs)
    second = run_chunked_agent_review(**kwargs)

    assert calls["n"] == 2  # the failed first call was not cached, so it retried
    # The only piece failed -> a synthetic "review incomplete" issue, not a false-clean [].
    assert len(first) == 1 and first[0].severity == "critical"
    assert len(second) == 1 and second[0].description.startswith("real bug")  # the retry succeeded


def test_failing_cache_miss_among_cache_hits_still_appends_synthetic_issue():
    """A cache hit must never outnumber a failed fresh attempt into looking like
    partial coverage: one file cached clean from a prior cycle plus one changed
    file whose only fresh attempt fails must still fail closed, even though
    ``len(pieces)`` (2) is greater than ``failed`` (1)."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        if calls["n"] <= 2:
            return [_Bug()]  # both round-1 pieces succeed and get cached
        raise RuntimeError("agent unavailable")  # b.py's only round-2 attempt fails

    cache = AgentReviewCache()
    common = dict(
        run_chunk=run_chunk,
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
        cache_context="python\x00task",
    )

    # Round 1: both files reviewed for real and cached.
    run_chunked_agent_review(
        files={"a.py": "def a():\n    return 1", "b.py": "def b():\n    return 2"}, **common
    )
    assert calls["n"] == 2

    # Round 2: a.py is unchanged (served from cache, no run_chunk call), b.py
    # changed and its only fresh attempt fails. len(pieces) == 2 but exactly
    # one piece (b.py) actually needed a fresh review this call, and it failed.
    second = run_chunked_agent_review(
        files={"a.py": "def a():\n    return 1", "b.py": "def b():\n    return 3"}, **common
    )
    assert calls["n"] == 3  # only b.py attempted a fresh call

    # a.py's cached issue plus the synthetic "review incomplete" issue for b.py --
    # not a bare cache replay that silently omits the fact that b.py was never
    # actually reviewed this round.
    assert len(second) == 2
    cached = [i for i in second if i.description.startswith("real bug")]
    synthetic = [
        i for i in second if i.severity == "critical" and not i.description.startswith("real bug")
    ]
    assert len(cached) == 1
    assert len(synthetic) == 1


def test_cache_none_default_is_unchanged_passthrough():
    """Omitting ``cache`` (the default) calls the agent every time, exactly as before caching existed."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        return [_Bug()]

    kwargs = dict(
        run_chunk=run_chunk,
        files={"x.py": "def f():\n    return 1"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    run_chunked_agent_review(**kwargs)
    run_chunked_agent_review(**kwargs)

    assert calls["n"] == 2  # no cache given -> both calls hit the agent


def test_run_qa_agent_cache_hit_skips_second_qa_call():
    """run_qa_agent's cache_context folds in language/task_description before hashing."""
    calls = {"n": 0}

    class _QAAgent:
        def run(self, inp):
            calls["n"] += 1
            return MagicMock(bugs_found=[_Bug()])

    cache = AgentReviewCache()
    kwargs = dict(
        qa_agent=_QAAgent(),
        files={"x.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    run_qa_agent(**kwargs)
    run_qa_agent(**kwargs)

    assert calls["n"] == 1


def test_run_security_agent_cache_hit_skips_second_security_call():
    """run_security_agent's cache_context folds in language/task_description before hashing."""
    calls = {"n": 0}

    class _SecAgent:
        def run(self, inp):
            calls["n"] += 1
            return MagicMock(vulnerabilities=[])

    cache = AgentReviewCache()
    kwargs = dict(
        security_agent=_SecAgent(),
        files={"x.ts": "const f = () => 1;"},
        language="typescript",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    run_security_agent(**kwargs)
    run_security_agent(**kwargs)

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Failure fallbacks (approved=False + no findings) must never be cached
# ---------------------------------------------------------------------------


def test_qa_agent_fallback_is_not_cached_and_is_retried():
    """QAExpertAgent's structured-output-failure fallback (approved=False, no
    bugs, returned normally rather than raised) must not be cached as a clean
    pass — the next call for the same content retries the agent for real."""
    calls = {"n": 0}

    class _FlakyQAAgent:
        def run(self, inp):
            calls["n"] += 1
            if calls["n"] == 1:
                return MagicMock(
                    bugs_found=[], approved=False, summary="QA could not parse model response: boom"
                )
            return MagicMock(bugs_found=[_Bug()], approved=False)

    cache = AgentReviewCache()
    kwargs = dict(
        qa_agent=_FlakyQAAgent(),
        files={"x.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    first = run_qa_agent(**kwargs)
    # The only piece hit the fallback -> a synthetic "review incomplete" issue,
    # not a false-clean [] (which a downstream gate would treat as "no findings").
    assert len(first) == 1 and first[0].severity == "high"
    assert calls["n"] == 1

    second = run_qa_agent(**kwargs)
    assert calls["n"] == 2  # not served from cache -- retried for real
    assert len(second) == 1 and second[0].description.startswith("real bug")


def test_qa_agent_genuine_rejection_with_findings_is_still_cached():
    """approved=False WITH populated bugs_found is a genuine result, not a
    fallback — it's cached normally."""
    calls = {"n": 0}

    class _QAAgent:
        def run(self, inp):
            calls["n"] += 1
            return MagicMock(bugs_found=[_Bug()], approved=False)

    cache = AgentReviewCache()
    kwargs = dict(
        qa_agent=_QAAgent(),
        files={"x.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    run_qa_agent(**kwargs)
    run_qa_agent(**kwargs)

    assert calls["n"] == 1


def test_qa_agent_clean_pass_is_still_cached():
    """approved=True with no bugs (a genuine clean pass) is not mistaken for a
    fallback — it's cached normally."""
    calls = {"n": 0}

    class _QAAgent:
        def run(self, inp):
            calls["n"] += 1
            return MagicMock(bugs_found=[], approved=True)

    cache = AgentReviewCache()
    kwargs = dict(
        qa_agent=_QAAgent(),
        files={"x.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    run_qa_agent(**kwargs)
    run_qa_agent(**kwargs)

    assert calls["n"] == 1


def test_security_agent_fallback_is_not_cached_and_is_retried():
    """CybersecurityExpertAgent's structured-output-failure fallback
    (approved=False, no vulnerabilities, returned normally rather than raised)
    must not be cached as a clean pass."""
    calls = {"n": 0}

    class _Vuln:
        severity = "high"
        description = "real vuln"
        location = "x.ts"
        recommendation = ""

    class _FlakySecAgent:
        def run(self, inp):
            calls["n"] += 1
            if calls["n"] == 1:
                return MagicMock(
                    vulnerabilities=[], approved=False, summary="Security analysis failed: boom"
                )
            return MagicMock(vulnerabilities=[_Vuln()], approved=False)

    cache = AgentReviewCache()
    kwargs = dict(
        security_agent=_FlakySecAgent(),
        files={"x.ts": "const f = () => 1;"},
        language="typescript",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    first = run_security_agent(**kwargs)
    # The only piece hit the fallback -> a synthetic "review incomplete" issue,
    # not a false-clean [] (which a downstream gate would treat as "no findings").
    assert len(first) == 1 and first[0].severity == "critical"
    assert calls["n"] == 1

    second = run_security_agent(**kwargs)
    assert calls["n"] == 2  # not served from cache -- retried for real
    assert len(second) == 1 and second[0].description == "real vuln"


def test_security_agent_genuine_rejection_with_findings_is_still_cached():
    """approved=False WITH populated vulnerabilities is a genuine result, not a
    fallback — it's cached normally."""
    calls = {"n": 0}

    class _Vuln:
        severity = "high"
        description = "real vuln"
        location = "x.ts"
        recommendation = ""

    class _SecAgent:
        def run(self, inp):
            calls["n"] += 1
            return MagicMock(vulnerabilities=[_Vuln()], approved=False)

    cache = AgentReviewCache()
    kwargs = dict(
        security_agent=_SecAgent(),
        files={"x.ts": "const f = () => 1;"},
        language="typescript",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
        cache=cache,
    )

    run_security_agent(**kwargs)
    run_security_agent(**kwargs)

    assert calls["n"] == 1
