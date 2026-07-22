"""Unit tests for shared.phases.deliver.make_run_deliver."""

from __future__ import annotations

import logging
import types
from pathlib import Path
from typing import Any, Dict

from software_engineering_team.shared.deliver_utils import DeliverGitOps


def test_make_run_deliver_reads_git_ns_at_call_time(monkeypatch, tmp_path: Path) -> None:
    """Patches applied to git_ns after bind must appear in ops passed to impl."""
    import software_engineering_team.shared.phases.deliver as deliver_mod

    def _noop(*_a, **_k):
        return True

    git_ns = types.SimpleNamespace(
        abort_merge=_noop,
        checkout_branch=_noop,
        commit_working_tree=_noop,
        create_feature_branch=_noop,
        delete_branch=_noop,
        merge_branch=_noop,
        write_agent_output=_noop,
    )

    captured: Dict[str, Any] = {}

    def fake_impl(**kwargs):
        captured["ops"] = kwargs["ops"]
        return "ok"

    monkeypatch.setattr(deliver_mod, "run_deliver_impl", fake_impl)

    models = types.SimpleNamespace()  # unused by stubbed impl
    run_deliver = deliver_mod.make_run_deliver(
        git_ns=git_ns,
        models=models,
        commit_msg_template="[{scope}] {summary}",
        logger=logging.getLogger("test_make_run_deliver"),
    )

    def patched_create(*_a, **_k):
        return (True, "feature/patched")

    git_ns.create_feature_branch = patched_create

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.py": "x"},
        summary="s",
    )

    assert result == "ok"
    ops = captured["ops"]
    assert isinstance(ops, DeliverGitOps)
    assert ops.create_feature_branch is patched_create
