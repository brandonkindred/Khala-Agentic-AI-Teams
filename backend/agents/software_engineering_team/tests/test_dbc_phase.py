"""Direct-call tests for ``dbc_phase.py``'s two functions.

Calls ``run_dbc_comments_review``/``_run_dbc_self_review`` directly rather
than through ``run_gated_execution_impl`` -- this module is not yet wired
into the gated loop (that's sibling work), so its tests exercise it in
isolation, per the module's own docstring.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from software_engineering_team.shared.phases import dbc_phase
from software_engineering_team.shared.phases.dbc_phase import (
    _run_dbc_self_review,
    run_dbc_comments_review,
)
from software_engineering_team.shared.phases.execution import ReviewDependencies
from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentsOutput,
    DbcCommentsStatus,
)


def _task() -> SimpleNamespace:
    return SimpleNamespace(id="t1", title="T", description="do the thing")


def _microtask(mid: str = "mt-1") -> SimpleNamespace:
    return SimpleNamespace(id=mid, title="Microtask", output_files={})


def _gate_config(run_dbc_self_review) -> SimpleNamespace:
    return SimpleNamespace(run_dbc_self_review=run_dbc_self_review)


def _call(
    *,
    tmp_path,
    gate_config,
    mt=None,
    microtask_files=None,
    all_files=None,
    deps=None,
    build_verify_label: str = "build",
    progress_callback=None,
    completed_ids=None,
):
    mt = mt if mt is not None else _microtask()
    microtask_files = microtask_files if microtask_files is not None else {}
    all_files = all_files if all_files is not None else {}
    deps = deps if deps is not None else ReviewDependencies()
    completed_ids = completed_ids if completed_ids is not None else set()
    calls = []
    _run_dbc_self_review(
        gate_config=gate_config,
        task=_task(),
        task_id="t1",
        mt=mt,
        microtask_files=microtask_files,
        repo_path=tmp_path,
        all_files=all_files,
        architecture=None,
        language="python",
        deps=deps,
        build_verify_label=build_verify_label,
        progress_callback=progress_callback,
        current_idx=0,
        completed_ids=completed_ids,
        total=1,
        detail_cb=lambda d, idx, phase: calls.append((d, idx, phase)),
    )
    return mt, microtask_files, all_files, calls


# ---------------------------------------------------------------------------
# run_dbc_comments_review
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Stand-in for DbcCommentsAgent, capturing its constructor/.run() calls."""

    last_input: Optional[Any] = None
    last_on_status: Optional[Any] = None
    result: DbcCommentsOutput = DbcCommentsOutput()
    status_events: tuple = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def run(self, input_data: Any, on_status: Optional[Any] = None) -> DbcCommentsOutput:
        type(self).last_input = input_data
        type(self).last_on_status = on_status
        for status, detail in type(self).status_events:
            if on_status:
                on_status(status, detail)
        return type(self).result


def test_run_dbc_comments_review_concatenates_with_headers(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput(files={"a.py": "# dbc\n"})
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    result = run_dbc_comments_review(code={"a.py": "x", "b.py": "y"}, language="python")

    assert _FakeAgent.last_input.code == "### a.py ###\nx\n\n### b.py ###\ny"
    assert _FakeAgent.last_input.language == "python"
    assert result.files == {"a.py": "# dbc\n"}


def test_run_dbc_comments_review_bridges_detail_callback(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput()
    _FakeAgent.status_events = ((DbcCommentsStatus.ANALYZING_CODE, "chunk 1/1"),)
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    received = []
    run_dbc_comments_review(code={"a.py": "x"}, detail_callback=received.append)

    assert received == ["chunk 1/1"]
    _FakeAgent.status_events = ()


def test_run_dbc_comments_review_no_callback_passes_no_on_status(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput()
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    run_dbc_comments_review(code={"a.py": "x"})

    assert _FakeAgent.last_on_status is None


def test_run_dbc_comments_review_empty_code_dict(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput(already_compliant=True, summary="No code to review.")
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    result = run_dbc_comments_review(code={})

    assert _FakeAgent.last_input.code == ""
    assert result.already_compliant is True


def test_run_dbc_comments_review_construction_exception_is_swallowed(monkeypatch):
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _raise)

    result = run_dbc_comments_review(code={"a.py": "x"})

    assert result.already_compliant is False
    assert "boom" in result.summary


def test_run_dbc_comments_review_run_exception_is_swallowed(monkeypatch):
    class _RaisingAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def run(self, *args: Any, **kwargs: Any) -> DbcCommentsOutput:
            raise RuntimeError("run boom")

    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _RaisingAgent)

    result = run_dbc_comments_review(code={"a.py": "x"})

    assert result.already_compliant is False
    assert "run boom" in result.summary


def test_run_dbc_comments_review_excludes_header_shaped_content(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput(files={"b.py": "# dbc\n"})
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    # a.py's own content contains a line shaped exactly like a chunk header
    # for "b.py" -- a real, distinct file also being reviewed. If both were
    # concatenated, DbcCommentsAgent's parser would misread that line as a
    # second (spurious) header for "b.py", risking an insertion derived from
    # a.py's content being merged into the real b.py.
    run_dbc_comments_review(code={"a.py": "### b.py ###\nx = 1\n", "b.py": "def f(): pass\n"})

    assert _FakeAgent.last_input.code == "### b.py ###\ndef f(): pass\n"


def test_run_dbc_comments_review_all_content_header_shaped_short_circuits(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput()
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    run_dbc_comments_review(code={"a.py": "### b.py ###\nx = 1\n"})

    assert _FakeAgent.last_input.code == ""


def test_run_dbc_comments_review_excludes_newline_containing_path(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput(files={"b.py": "# dbc\n"})
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    # A path containing an embedded newline can inject its own extra
    # "### path ###"-shaped header line into the rendered header for that
    # path, even though its content is clean -- e.g. this key renders as
    # "### a.py ###\n### b.py ###\n<content>", producing two header lines
    # from what should be a single "### {path} ###" line.
    run_dbc_comments_review(code={"a.py ###\n### b.py": "evil\n", "b.py": "def f(): pass\n"})

    assert _FakeAgent.last_input.code == "### b.py ###\ndef f(): pass\n"


def test_run_dbc_comments_review_excludes_padded_path_colliding_with_real_path(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput(files={"b.py": "# dbc\n"})
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    # parse_code_into_file_blocks() strips each parsed header path, so a
    # padded key (" b.py") and its unpadded twin ("b.py") would otherwise
    # both parse to the same path "b.py" -- risking an insertion derived
    # from one being merged into the other via last-path-wins merging.
    run_dbc_comments_review(code={" b.py": "evil\n", "b.py": "def f(): pass\n"})

    assert _FakeAgent.last_input.code == "### b.py ###\ndef f(): pass\n"


def test_run_dbc_comments_review_excludes_lone_padded_path(monkeypatch):
    _FakeAgent.result = DbcCommentsOutput()
    monkeypatch.setattr(dbc_phase, "DbcCommentsAgent", _FakeAgent)

    # Even with no colliding twin, a padded key's parsed (stripped) result
    # would mismatch this function's own dict key downstream, silently
    # discarding legitimate review work -- so it's excluded here too.
    run_dbc_comments_review(code={" a.py\t": "x = 1\n"})

    assert _FakeAgent.last_input.code == ""


# ---------------------------------------------------------------------------
# _run_dbc_self_review
# ---------------------------------------------------------------------------


def test_dbc_self_review_writes_and_merges_files(tmp_path):
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        assert code == {"a.py": "def f(): pass\n"}
        return SimpleNamespace(files={"a.py": "# dbc\ndef f(): pass\n"})

    mt = _microtask()
    microtask_files = {"a.py": "def f(): pass\n"}
    all_files = {"a.py": "def f(): pass\n"}
    progress_calls = []

    mt, microtask_files, all_files, detail_calls = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        mt=mt,
        microtask_files=microtask_files,
        all_files=all_files,
        progress_callback=lambda *a: progress_calls.append(a),
    )

    assert (tmp_path / "a.py").read_text() == "# dbc\ndef f(): pass\n"
    assert microtask_files["a.py"] == "# dbc\ndef f(): pass\n"
    assert all_files["a.py"] == "# dbc\ndef f(): pass\n"
    assert mt.output_files["a.py"] == "# dbc\ndef f(): pass\n"
    assert not hasattr(mt, "status")
    assert progress_calls and progress_calls[0][4] == "dbc"


def test_dbc_self_review_unsafe_path_skipped_atomically(tmp_path):
    # The unsafe key must itself already be a key of microtask_files --
    # otherwise the "discard paths not in the reviewed set" filter would
    # remove it first. Traversal keys are dropped before the write; remaining
    # in-repo files still apply.
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "safe", "../escape.py": "evil"})

    microtask_files = {"a.py": "orig", "../escape.py": "orig-evil"}
    all_files = {"a.py": "orig", "../escape.py": "orig-evil"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        all_files=all_files,
    )

    assert (tmp_path / "a.py").read_text() == "safe"
    assert microtask_files == {"a.py": "safe", "../escape.py": "orig-evil"}
    assert all_files == {"a.py": "safe", "../escape.py": "orig-evil"}


def test_dbc_self_review_rejects_absolute_map_key(tmp_path):
    """Absolute keys must not be rewritten as in-repo paths after slash-stripping."""
    abs_key = "/tmp/a.py"

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={abs_key: "leaked\n", "keep.py": "keep new\n"})

    (tmp_path / "keep.py").write_text("keep orig\n")
    microtask_files = {abs_key: "orig abs\n", "keep.py": "keep orig\n"}
    all_files = dict(microtask_files)

    _, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        all_files=all_files,
    )

    assert not (tmp_path / "tmp" / "a.py").exists()
    assert microtask_files[abs_key] == "orig abs\n"
    assert (tmp_path / "keep.py").read_text() == "keep new\n"
    assert all_files["keep.py"] == "keep new\n"


def test_dbc_self_review_rejects_symlink_escape_write(tmp_path):
    """A reviewed path through a repo symlink must not write outside the worktree."""
    outside = tmp_path.parent.resolve() / f"dbc-write-escape-{tmp_path.name}"
    outside.mkdir()
    (outside / "a.py").write_text("outside orig\n")
    (tmp_path / "src").symlink_to(outside)
    (tmp_path / "keep.py").write_text("keep orig\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"src/a.py": "leaked\n", "keep.py": "keep new\n"})

    microtask_files = {"src/a.py": "map orig\n", "keep.py": "keep orig\n"}
    all_files = dict(microtask_files)
    try:
        mt, microtask_files, all_files, _ = _call(
            tmp_path=tmp_path,
            gate_config=_gate_config(_review),
            microtask_files=microtask_files,
            all_files=all_files,
        )
        assert (outside / "a.py").read_text() == "outside orig\n"
        assert (tmp_path / "keep.py").read_text() == "keep new\n"
        assert microtask_files["src/a.py"] == "map orig\n"
        assert microtask_files["keep.py"] == "keep new\n"
        assert all_files["src/a.py"] == "map orig\n"
        assert mt.output_files["keep.py"] == "keep new\n"
        assert mt.output_files["src/a.py"] == "map orig\n"
    finally:
        (tmp_path / "src").unlink(missing_ok=True)
        (outside / "a.py").unlink(missing_ok=True)
        outside.rmdir()


def test_dbc_self_review_symlink_escape_only_is_noop(tmp_path):
    """When every reviewed path escapes through a symlink, skip the write entirely."""
    outside = tmp_path.parent.resolve() / f"dbc-write-escape-only-{tmp_path.name}"
    outside.mkdir()
    (outside / "a.py").write_text("outside orig\n")
    (tmp_path / "src").symlink_to(outside)

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"src/a.py": "leaked\n"})

    microtask_files = {"src/a.py": "map orig\n"}
    try:
        _, microtask_files, _, _ = _call(
            tmp_path=tmp_path,
            gate_config=_gate_config(_review),
            microtask_files=microtask_files,
        )
        assert (outside / "a.py").read_text() == "outside orig\n"
        assert microtask_files == {"src/a.py": "map orig\n"}
    finally:
        (tmp_path / "src").unlink(missing_ok=True)
        (outside / "a.py").unlink(missing_ok=True)
        outside.rmdir()


def test_dbc_self_review_write_rejects_batch_if_containment_filter_misses(tmp_path, monkeypatch):
    """write_repo_text_files remains all-or-nothing if an unsafe key slips the filter."""

    monkeypatch.setattr(dbc_phase, "_contained_write_path", lambda root, rel_path: root / "a.py")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "safe", "../escape.py": "evil"})

    microtask_files = {"a.py": "orig", "../escape.py": "orig-evil"}
    _, microtask_files, _, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
    )

    assert not (tmp_path / "a.py").exists()
    assert microtask_files == {"a.py": "orig", "../escape.py": "orig-evil"}


def test_dbc_self_review_gate_config_exception_is_swallowed(tmp_path):
    def _raises(*, code, **kwargs: Any) -> Any:
        raise RuntimeError("stub boom")

    microtask_files = {"a.py": "orig"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_raises),
        microtask_files=microtask_files,
    )

    assert microtask_files == {"a.py": "orig"}
    assert list(tmp_path.iterdir()) == []


def test_dbc_self_review_no_changes_is_noop(tmp_path):
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={})

    microtask_files = {"a.py": "orig"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
    )

    assert microtask_files == {"a.py": "orig"}
    assert list(tmp_path.iterdir()) == []


def test_dbc_self_review_no_build_verifier_skips_verification(tmp_path):
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(),
    )

    assert microtask_files["a.py"] == "dbc a\n"
    assert (tmp_path / "a.py").read_text() == "dbc a\n"


def test_dbc_self_review_build_verifier_success_keeps_files(tmp_path):
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    verifier_calls = []

    def _verify(repo_path, label, task_id):
        verifier_calls.append((repo_path, label, task_id))
        return True, "ok"

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
        build_verify_label="my-build",
    )

    assert microtask_files["a.py"] == "dbc a\n"
    assert (tmp_path / "a.py").read_text() == "dbc a\n"
    assert verifier_calls == [(tmp_path, "my-build", "t1")]


def test_dbc_self_review_build_verifier_success_syncs_repairs_into_maps(tmp_path):
    # Production ``_run_build_verification`` can return success after
    # ``_try_build_fix_one_at_a_time`` writes repairs to disk. Those edits
    # must land in the in-memory maps or Documentation will overwrite them
    # from the stale pre-repair contents.
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "other.py").write_text("orig other\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify_with_repairs(repo_path, label, tid):
        (Path(repo_path) / "other.py").write_text("llm repair\n")
        (Path(repo_path) / "new_fix.py").write_text("new repair\n")
        return True, "ok"

    microtask_files = {"a.py": "orig a\n", "other.py": "orig other\n"}
    all_files = dict(microtask_files)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        all_files=all_files,
        deps=ReviewDependencies(build_verifier=_verify_with_repairs),
    )

    assert microtask_files["a.py"] == "dbc a\n"
    assert microtask_files["other.py"] == "llm repair\n"
    assert microtask_files["new_fix.py"] == "new repair\n"
    assert all_files == microtask_files
    assert mt.output_files == microtask_files
    assert (tmp_path / "other.py").read_text() == "llm repair\n"
    assert (tmp_path / "new_fix.py").read_text() == "new repair\n"


def test_dbc_self_review_does_not_sync_venv_or_pytest_cache(tmp_path):
    # pytest's default cache provider writes ``.pytest_cache/`` on a passing
    # run, and an in-tree venv can be huge; neither belongs in the in-memory
    # maps even if the verifier (or pytest) created/changed those files.
    (tmp_path / "a.py").write_text("orig a\n")
    venv_file = tmp_path / "venv" / "lib" / "site.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("orig venv\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo_path, label, tid):
        (Path(repo_path) / "venv" / "lib" / "site.py").write_text("repaired venv\n")
        cache = Path(repo_path) / ".pytest_cache" / "README.md"
        cache.parent.mkdir(parents=True)
        cache.write_text("don't commit this\n")
        return True, "ok"

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert microtask_files == {"a.py": "dbc a\n"}
    assert all_files == microtask_files
    assert "venv/lib/site.py" not in microtask_files
    assert ".pytest_cache/README.md" not in microtask_files


def test_dbc_self_review_walk_error_skips_mutating_verifier(tmp_path, monkeypatch):
    # os.walk swallows scandir failures unless onerror re-raises; a partial
    # snapshot must not let the mutating verifier run.
    (tmp_path / "a.py").write_text("orig a\n")
    called: list[bool] = []

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo_path, label, tid):
        called.append(True)
        return True, "ok"

    def _walk(top, topdown=True, onerror=None, followlinks=False):
        if onerror is not None:
            onerror(OSError("scandir failed"))
        return iter([])

    monkeypatch.setattr(os, "walk", _walk)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert called == []
    assert microtask_files["a.py"] == "dbc a\n"
    assert (tmp_path / "a.py").read_text() == "dbc a\n"


def test_dbc_self_review_build_verifier_failure_reverts_only_touched_files(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "untouched.py").write_text("orig u\n")
    # "new.py" is a key of microtask_files (so it survives the
    # only-reviewed-paths filter) but was never actually flushed to disk --
    # exercises the disk-delete revert branch alongside a.py's
    # disk-restore branch.

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n", "new.py": "dbc new\n"})

    microtask_files = {
        "a.py": "orig a\n",
        "untouched.py": "orig u\n",
        "new.py": "not yet on disk\n",
    }
    all_files = dict(microtask_files)
    mt = _microtask()
    mt.output_files = dict(microtask_files)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        mt=mt,
        microtask_files=microtask_files,
        all_files=all_files,
        deps=ReviewDependencies(build_verifier=lambda repo, label, tid: (False, "build broke")),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert not (tmp_path / "new.py").exists()
    assert (tmp_path / "untouched.py").read_text() == "orig u\n"
    assert microtask_files == {
        "a.py": "orig a\n",
        "untouched.py": "orig u\n",
        "new.py": "not yet on disk\n",
    }
    assert all_files == microtask_files
    assert mt.output_files == microtask_files


def test_dbc_self_review_build_verifier_failure_reverts_verifier_repairs(tmp_path):
    # Production ``_run_build_verification`` may write LLM repairs to files
    # this phase did not touch (``_try_build_fix_one_at_a_time``) before it
    # knows whether verification will pass. On failure those edits must not
    # remain on disk after the phase reports that it reverted.
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "other.py").write_text("orig other\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify_with_repairs(repo_path, label, tid):
        (Path(repo_path) / "other.py").write_text("llm repair\n")
        (Path(repo_path) / "new_fix.py").write_text("new repair\n")
        return False, "build still broken"

    microtask_files = {"a.py": "orig a\n", "other.py": "orig other\n"}
    all_files = dict(microtask_files)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        all_files=all_files,
        deps=ReviewDependencies(build_verifier=_verify_with_repairs),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert (tmp_path / "other.py").read_text() == "orig other\n"
    assert not (tmp_path / "new_fix.py").exists()
    assert microtask_files == {"a.py": "orig a\n", "other.py": "orig other\n"}
    assert all_files == microtask_files


def test_dbc_self_review_pre_verify_snapshot_oserror_skips_verify(tmp_path, monkeypatch):
    # Without a post-write snapshot there is no baseline to restore verifier
    # mutations against, so the mutating verifier must not run.
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    called: list[bool] = []

    def _verify(repo, label, tid):
        called.append(True)
        return False, "should not run"

    def _raise_snapshot(root: Path) -> Dict[Path, bytes]:
        raise OSError("permission denied")

    monkeypatch.setattr(dbc_phase, "_snapshot_worktree", _raise_snapshot)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert called == []
    assert microtask_files["a.py"] == "dbc a\n"
    assert (tmp_path / "a.py").read_text() == "dbc a\n"


def test_dbc_self_review_revert_enumerate_oserror_still_restores_snapshot(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "other.py").write_text("orig other\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        (Path(repo) / "other.py").write_text("llm repair\n")
        (Path(repo) / "new_fix.py").write_text("created\n")
        return False, "broke"

    original_iter = dbc_phase._iter_worktree_files
    calls = {"n": 0}

    def _iter(root: Path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return original_iter(root, **kwargs)
        if calls["n"] == 2:
            raise OSError("walk failed")
        return original_iter(root, **kwargs)

    monkeypatch.setattr(dbc_phase, "_iter_worktree_files", _iter)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n", "other.py": "orig other\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert (tmp_path / "other.py").read_text() == "orig other\n"
    assert not (tmp_path / "new_fix.py").exists()


def test_dbc_self_review_revert_best_effort_walk_deletes_created_file(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "other.py").write_text("orig other\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        (Path(repo) / "other.py").write_text("llm repair\n")
        (Path(repo) / "new_fix.py").write_text("created\n")
        return False, "broke"

    original_iter = dbc_phase._iter_worktree_files
    calls = {"n": 0}

    def _iter(root: Path, *, strict: bool = True, **kwargs):
        if not strict:
            return original_iter(root, strict=False, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            return original_iter(root)
        raise OSError("walk failed")

    monkeypatch.setattr(dbc_phase, "_iter_worktree_files", _iter)

    _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n", "other.py": "orig other\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert (tmp_path / "other.py").read_text() == "orig other\n"
    assert not (tmp_path / "new_fix.py").exists()


def test_dbc_self_review_worktree_snapshot_skips_symlinks(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "real.txt").write_text("real\n")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    (tmp_path / "real_dir").mkdir()
    (tmp_path / "dir_link").symlink_to(tmp_path / "real_dir")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=lambda repo, label, tid: (True, "ok")),
    )

    assert microtask_files["a.py"] == "dbc a\n"
    assert (tmp_path / "a.py").read_text() == "dbc a\n"
    assert (tmp_path / "link.txt").is_symlink()
    assert (tmp_path / "dir_link").is_symlink()


def test_dbc_self_review_revert_deletes_verifier_created_symlink(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "real.txt").write_text("real\n")
    (tmp_path / "existing_link.txt").symlink_to(tmp_path / "real.txt")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        (Path(repo) / "new_link.txt").symlink_to(Path(repo) / "real.txt")
        return False, "broke"

    _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert (tmp_path / "existing_link.txt").is_symlink()
    assert not (tmp_path / "new_link.txt").exists()


def test_dbc_self_review_revert_unlinks_file_replaced_by_symlink(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    outside = tmp_path.parent.resolve() / f"dbc-symlink-target-{tmp_path.name}.txt"
    outside.write_text("outside orig\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        replaced = Path(repo) / "a.py"
        replaced.unlink()
        replaced.symlink_to(outside)
        return False, "broke"

    try:
        _call(
            tmp_path=tmp_path,
            gate_config=_gate_config(_review),
            microtask_files={"a.py": "orig a\n"},
            deps=ReviewDependencies(build_verifier=_verify),
        )
        assert not (tmp_path / "a.py").is_symlink()
        assert (tmp_path / "a.py").read_text() == "orig a\n"
        assert outside.read_text() == "outside orig\n"
    finally:
        outside.unlink(missing_ok=True)


def test_dbc_self_review_revert_deletes_verifier_created_dir_symlink(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "real_dir").mkdir()
    (tmp_path / "real_dir" / "x.txt").write_text("x\n")
    (tmp_path / "existing_dir_link").symlink_to(tmp_path / "real_dir")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        (Path(repo) / "new_dir_link").symlink_to(Path(repo) / "real_dir")
        return False, "broke"

    _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert (tmp_path / "existing_dir_link").is_symlink()
    assert not (tmp_path / "new_dir_link").exists()
    assert (tmp_path / "real_dir" / "x.txt").read_text() == "x\n"


def test_dbc_self_review_revert_replaces_directory_with_file(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        replaced = Path(repo) / "a.py"
        replaced.unlink()
        replaced.mkdir()
        (replaced / "nested.txt").write_text("nested\n")
        return False, "broke"

    _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert not (tmp_path / "a.py").is_dir()
    assert (tmp_path / "a.py").read_text() == "orig a\n"


def test_dbc_self_review_revert_restores_deleted_symlink(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "real.txt").write_text("real\n")
    (tmp_path / "existing_link.txt").symlink_to("real.txt")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        (Path(repo) / "existing_link.txt").unlink()
        return False, "broke"

    _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    link = tmp_path / "existing_link.txt"
    assert link.is_symlink()
    assert os.readlink(link) == "real.txt"


def test_dbc_self_review_revert_restores_retargeted_symlink(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "real.txt").write_text("real\n")
    (tmp_path / "other.txt").write_text("other\n")
    (tmp_path / "existing_link.txt").symlink_to("real.txt")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        link = Path(repo) / "existing_link.txt"
        link.unlink()
        link.symlink_to("other.txt")
        return False, "broke"

    _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    link = tmp_path / "existing_link.txt"
    assert link.is_symlink()
    assert os.readlink(link) == "real.txt"


def test_dbc_self_review_revert_clears_dir_replaced_by_symlink_before_descendants(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("orig a\n")
    outside = tmp_path.parent.resolve() / f"dbc-anc-{tmp_path.name}"
    outside.mkdir()
    (outside / "a.py").write_text("outside orig\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"pkg/a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        pkg = Path(repo) / "pkg"
        shutil.rmtree(pkg)
        pkg.symlink_to(outside)
        return False, "broke"

    try:
        _call(
            tmp_path=tmp_path,
            gate_config=_gate_config(_review),
            microtask_files={"pkg/a.py": "orig a\n"},
            deps=ReviewDependencies(build_verifier=_verify),
        )
        assert not (tmp_path / "pkg").is_symlink()
        assert (tmp_path / "pkg" / "a.py").is_file()
        assert (tmp_path / "pkg" / "a.py").read_text() == "orig a\n"
        assert (outside / "a.py").read_text() == "outside orig\n"
    finally:
        (outside / "a.py").unlink(missing_ok=True)
        outside.rmdir()


def test_dbc_self_review_revert_restores_symlink_when_parent_dir_deleted(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "real.txt").write_text("real\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "link.txt").symlink_to("../real.txt")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        shutil.rmtree(Path(repo) / "sub")
        return False, "broke"

    _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    link = tmp_path / "sub" / "link.txt"
    assert link.is_symlink()
    assert os.readlink(link) == "../real.txt"


def test_dbc_self_review_build_verifier_raises_reverts(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _raises(repo, label, tid):
        raise RuntimeError("verifier boom")

    microtask_files = {"a.py": "orig a\n"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        deps=ReviewDependencies(build_verifier=_raises),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert microtask_files == {"a.py": "orig a\n"}


def test_dbc_self_review_build_verifier_reverts_key_absent_before(tmp_path):
    # "a.py" must be a key of microtask_files to survive the
    # only-reviewed-paths filter -- absent from all_files/mt.output_files
    # instead, to exercise their own "prior was absent -> pop" branch
    # independent of microtask_files' own (always-present) branch.
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    microtask_files: Dict[str, str] = {"a.py": "orig a\n"}
    all_files: Dict[str, str] = {}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        all_files=all_files,
        deps=ReviewDependencies(build_verifier=lambda repo, label, tid: (False, "broke")),
    )

    assert microtask_files == {"a.py": "orig a\n"}
    assert "a.py" not in all_files
    assert "a.py" not in mt.output_files
    assert not (tmp_path / "a.py").exists()


def test_dbc_self_review_snapshot_oserror_skips_without_writing(tmp_path, monkeypatch):
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _raise_read_bytes(self: Path) -> bytes:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", _raise_read_bytes)
    (tmp_path / "a.py").write_text("orig a\n")
    microtask_files = {"a.py": "orig a\n"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
    )

    assert microtask_files == {"a.py": "orig a\n"}
    assert (tmp_path / "a.py").read_text() == "orig a\n"


def test_dbc_self_review_write_oserror_reverts_partial_write(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("orig a\n")
    # "new.py" is a key of microtask_files (so it survives the
    # only-reviewed-paths filter) but was never flushed to disk yet.

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n", "new.py": "dbc new\n"})

    def _fake_write(repo_path: Path, files: Dict[str, str]) -> None:
        # Simulate write_repo_text_files getting partway through the batch
        # (e.g. a full disk) before raising -- one file lands, one doesn't.
        (Path(repo_path) / "a.py").write_text(files["a.py"])
        raise OSError("no space left on device")

    monkeypatch.setattr(dbc_phase, "write_repo_text_files", _fake_write)
    microtask_files = {"a.py": "orig a\n", "new.py": "not yet on disk\n"}
    all_files = dict(microtask_files)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        all_files=all_files,
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert not (tmp_path / "new.py").exists()
    assert microtask_files == {"a.py": "orig a\n", "new.py": "not yet on disk\n"}
    assert all_files == microtask_files


def test_dbc_self_review_write_unicode_encode_error_reverts(tmp_path, monkeypatch):
    # Path.write_text(..., encoding="utf-8") raises UnicodeEncodeError for an
    # unpaired surrogate; that is not an OSError, so it must be caught on the
    # same revert path or the never-fail phase would abort.
    (tmp_path / "a.py").write_text("orig a\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _fake_write(repo_path: Path, files: Dict[str, str]) -> None:
        (Path(repo_path) / "a.py").write_text(files["a.py"])
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(dbc_phase, "write_repo_text_files", _fake_write)
    microtask_files = {"a.py": "orig a\n"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert microtask_files == {"a.py": "orig a\n"}


def test_revert_disk_swallows_oserror(tmp_path, monkeypatch):
    def _raise_write_bytes(self: Path, data: bytes) -> int:
        raise OSError("still unwritable")

    monkeypatch.setattr(Path, "write_bytes", _raise_write_bytes)

    # Should not raise even though the underlying restore attempt fails.
    dbc_phase._revert_disk({tmp_path / "a.py": b"orig a\n"})


def test_restore_symlinks_swallows_oserror(tmp_path, monkeypatch):
    def _raise_symlink_to(self: Path, target: str, *args: Any, **kwargs: Any) -> None:
        raise OSError("cannot symlink")

    monkeypatch.setattr(Path, "symlink_to", _raise_symlink_to)
    dbc_phase._restore_symlinks({tmp_path / "link.txt": "real.txt"})


def test_sync_verifier_repairs_walk_oserror_returns_false(tmp_path, monkeypatch):
    def _raise_iter(root: Path, **kwargs):
        raise OSError("walk failed")

    monkeypatch.setattr(dbc_phase, "_iter_worktree_files", _raise_iter)
    synced = dbc_phase._sync_verifier_repairs_into_maps(
        root=tmp_path,
        pre_verify_disk={},
        microtask_files={},
        all_files={},
        mt=_microtask(),
    )
    assert synced is False


def test_dbc_self_review_sync_walk_failure_reverts(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "other.py").write_text("orig other\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        (Path(repo) / "other.py").write_text("llm repair\n")
        return True, "ok"

    original_iter = dbc_phase._iter_worktree_files
    calls = {"n": 0}

    def _iter(root: Path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return original_iter(root, **kwargs)
        if calls["n"] == 2:
            raise OSError("sync walk failed")
        return original_iter(root, **kwargs)

    monkeypatch.setattr(dbc_phase, "_iter_worktree_files", _iter)

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n", "other.py": "orig other\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert (tmp_path / "a.py").read_text() == "orig a\n"
    assert (tmp_path / "other.py").read_text() == "orig other\n"
    assert microtask_files["a.py"] == "orig a\n"
    assert microtask_files["other.py"] == "orig other\n"


def test_dbc_self_review_sync_removes_deleted_files(tmp_path):
    (tmp_path / "a.py").write_text("orig a\n")
    (tmp_path / "gone.py").write_text("orig gone\n")

    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"a.py": "dbc a\n"})

    def _verify(repo, label, tid):
        (Path(repo) / "gone.py").unlink()
        return True, "ok"

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files={"a.py": "orig a\n", "gone.py": "orig gone\n"},
        all_files={"a.py": "orig a\n", "gone.py": "orig gone\n"},
        deps=ReviewDependencies(build_verifier=_verify),
    )

    assert not (tmp_path / "gone.py").exists()
    assert "gone.py" not in microtask_files
    assert "gone.py" not in all_files
    assert "gone.py" not in mt.output_files
    assert microtask_files["a.py"] == "dbc a\n"


def test_sync_verifier_repairs_skips_unreadable_binary_and_outside(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "good.py").write_text("ok\n")
    (root / "bin.dat").write_bytes(b"\xff\xfe")
    unreadable = root / "bad.py"
    unreadable.write_text("x\n")
    outside = tmp_path.parent.resolve() / f"dbc-outside-{tmp_path.name}.txt"
    outside.write_text("out\n")
    outside_prior = tmp_path.parent.resolve() / f"dbc-prior-outside-{tmp_path.name}.txt"

    orig_read = Path.read_bytes

    def _read(self: Path) -> bytes:
        if self.resolve() == unreadable:
            raise OSError("unreadable")
        return orig_read(self)

    monkeypatch.setattr(Path, "read_bytes", _read)
    orig_iter = dbc_phase._iter_worktree_files

    def _iter(r: Path, **kwargs):
        yield from orig_iter(r, **kwargs)
        yield outside

    monkeypatch.setattr(dbc_phase, "_iter_worktree_files", _iter)

    microtask_files: Dict[str, str] = {}
    all_files: Dict[str, str] = {}
    try:
        dbc_phase._sync_verifier_repairs_into_maps(
            root=root,
            pre_verify_disk={outside_prior: b"out\n"},
            microtask_files=microtask_files,
            all_files=all_files,
            mt=_microtask(),
        )
    finally:
        outside.unlink(missing_ok=True)

    assert microtask_files == {"good.py": "ok\n"}
    assert all_files == microtask_files


def test_dbc_self_review_discards_paths_not_in_reviewed_files(tmp_path):
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        # Simulates a spurious "fake.py" entry from a header-shaped comment
        # line inside a.py being misread as a chunk boundary by DbC's parser.
        return SimpleNamespace(files={"a.py": "dbc a\n", "fake.py": "spurious\n"})

    microtask_files = {"a.py": "orig a\n"}
    all_files = {"a.py": "orig a\n"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
        all_files=all_files,
    )

    assert (tmp_path / "a.py").read_text() == "dbc a\n"
    assert not (tmp_path / "fake.py").exists()
    assert microtask_files == {"a.py": "dbc a\n"}
    assert all_files == {"a.py": "dbc a\n"}
    assert "fake.py" not in mt.output_files


def test_dbc_self_review_all_paths_spurious_is_noop(tmp_path):
    def _review(*, code, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={"fake.py": "spurious\n"})

    microtask_files = {"a.py": "orig a\n"}

    mt, microtask_files, all_files, _ = _call(
        tmp_path=tmp_path,
        gate_config=_gate_config(_review),
        microtask_files=microtask_files,
    )

    assert not (tmp_path / "fake.py").exists()
    assert microtask_files == {"a.py": "orig a\n"}
