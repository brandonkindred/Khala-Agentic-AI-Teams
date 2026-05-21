"""Extra tests for ``software_engineering_team.shared.repo_writer`` covering
the validation helpers, file-conversion helper, and the agent-output write
flow with the git layer mocked out.
"""

from __future__ import annotations

from types import SimpleNamespace

# ---------------------------------------------------------------------------
# _validate_paths
# ---------------------------------------------------------------------------


def test_validate_paths_well_known_dirs_allowed() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    validated, warnings = _validate_paths({"src/foo.py": "code"})
    assert "src/foo.py" in validated
    assert warnings == []


def test_validate_paths_long_segment_rejected() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    long_name = "a" * 40
    validated, warnings = _validate_paths({f"{long_name}.py": "code"})
    assert validated == {}
    assert any("REJECTED" in w for w in warnings)


def test_validate_paths_sentence_dash_rejected() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    bad = "implement-the-feature-quickly.py"
    validated, _ = _validate_paths({bad: "code"})
    assert bad not in validated


def test_validate_paths_sentence_underscore_rejected() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    bad = "impl_a_b_c_d_e.py"
    validated, _ = _validate_paths({bad: "code"})
    assert validated == {}


def test_validate_paths_verb_prefix_rejected() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    bad = "implement_thing.py"
    validated, _ = _validate_paths({bad: "code"})
    assert validated == {}


def test_validate_paths_verb_prefix_allowed_in_migrations() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    path = "alembic/versions/add_index.py"
    validated, _ = _validate_paths({path: "x"})
    assert path in validated


def test_validate_paths_filler_word_rejected() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    bad = "foo_with_bar.py"
    validated, _ = _validate_paths({bad: "code"})
    assert validated == {}


def test_validate_paths_test_file_name_lengths() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    # Long test names are allowed up to 60 chars
    name = "test_" + ("x" * 40) + ".py"
    validated, _ = _validate_paths({name: "x"})
    assert name in validated


def test_validate_paths_gitignore_allowed() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    validated, _ = _validate_paths({".gitignore": "node_modules/\n"})
    assert ".gitignore" in validated


def test_validate_paths_empty_gitignore_rejected() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    validated, warnings = _validate_paths({".gitignore": "   "})
    assert validated == {}
    assert any(".gitignore" in w for w in warnings)


def test_validate_paths_empty_content_rejected() -> None:
    from software_engineering_team.shared.repo_writer import _validate_paths

    validated, warnings = _validate_paths({"src/foo.py": "   "})
    assert validated == {}


# ---------------------------------------------------------------------------
# _output_to_files_dict
# ---------------------------------------------------------------------------


def test_output_to_files_dict_files_attr() -> None:
    from software_engineering_team.shared.repo_writer import _output_to_files_dict

    output = SimpleNamespace(files={"app.py": "code"})
    out = _output_to_files_dict(output, subdir="backend")
    assert out == {"backend/app.py": "code"}


def test_output_to_files_dict_code_attr_python() -> None:
    from software_engineering_team.shared.repo_writer import _output_to_files_dict

    output = SimpleNamespace(code="print('x')", language="python", files={})
    out = _output_to_files_dict(output)
    assert "main.py" in out


def test_output_to_files_dict_code_attr_java() -> None:
    from software_engineering_team.shared.repo_writer import _output_to_files_dict

    output = SimpleNamespace(code="class X{}", language="java", files={})
    out = _output_to_files_dict(output)
    assert "main.java" in out


def test_output_to_files_dict_tests_attr() -> None:
    from software_engineering_team.shared.repo_writer import _output_to_files_dict

    output = SimpleNamespace(tests="def test(): pass")
    out = _output_to_files_dict(output, subdir="")
    assert "tests/test_main.py" in out


def test_output_to_files_dict_devops_artifacts() -> None:
    from software_engineering_team.shared.repo_writer import _output_to_files_dict

    output = SimpleNamespace(
        pipeline_yaml="name: ci",
        dockerfile="FROM python",
        docker_compose="version: '3'",
        iac_content="resource {}",
        artifacts={"extra/file.tf": "more"},
    )
    out = _output_to_files_dict(output)
    assert ".github/workflows/ci.yml" in out
    assert "Dockerfile" in out
    assert "docker-compose.yml" in out
    assert "infrastructure/main.tf" in out
    assert "extra/file.tf" in out


def test_output_to_files_dict_fixed_code() -> None:
    from software_engineering_team.shared.repo_writer import _output_to_files_dict

    output = SimpleNamespace(fixed_code="patched = 1")
    out = _output_to_files_dict(output, subdir="bin")
    assert "bin/fixes.py" in out


# ---------------------------------------------------------------------------
# merge_gitignore_entries
# ---------------------------------------------------------------------------


def test_merge_gitignore_entries_creates_new(tmp_path) -> None:
    from software_engineering_team.shared.repo_writer import merge_gitignore_entries

    out, changed = merge_gitignore_entries(tmp_path, ["node_modules/", "dist/"])
    assert changed is True
    assert "node_modules/" in out
    assert "dist/" in out


def test_merge_gitignore_entries_no_change_when_all_present(tmp_path) -> None:
    from software_engineering_team.shared.repo_writer import merge_gitignore_entries

    (tmp_path / ".gitignore").write_text("node_modules/\ndist/\n")
    out, changed = merge_gitignore_entries(tmp_path, ["node_modules/"])
    assert changed is False


def test_merge_gitignore_entries_appends(tmp_path) -> None:
    from software_engineering_team.shared.repo_writer import merge_gitignore_entries

    (tmp_path / ".gitignore").write_text("node_modules/")
    out, changed = merge_gitignore_entries(tmp_path, ["dist/"])
    assert changed is True
    assert "node_modules/" in out
    assert "dist/" in out


# ---------------------------------------------------------------------------
# write_agent_output (mocking write_files_and_commit)
# ---------------------------------------------------------------------------


def test_write_agent_output_dict_with_files(tmp_path, monkeypatch) -> None:
    from software_engineering_team.shared import repo_writer

    captured = {}

    def fake_commit(repo_path, files, msg):
        captured["files"] = files
        captured["msg"] = msg
        return True, "committed"

    monkeypatch.setattr(repo_writer, "write_files_and_commit", fake_commit)
    ok, msg = repo_writer.write_agent_output(
        tmp_path,
        {"files": {"src/main.py": "print('x')"}, "commit_message": "feat: add main"},
    )
    assert ok is True
    assert "src/main.py" in captured["files"]
    assert captured["msg"] == "feat: add main"


def test_write_agent_output_dict_with_fixed_code(tmp_path, monkeypatch) -> None:
    from software_engineering_team.shared import repo_writer

    monkeypatch.setattr(
        repo_writer, "write_files_and_commit", lambda *a, **kw: (True, "ok")
    )
    ok, msg = repo_writer.write_agent_output(
        tmp_path, {"fixed_code": "patched=1", "fix_path": "src/util.py"}
    )
    assert ok is True


def test_write_agent_output_dict_no_files_returns_failure(tmp_path) -> None:
    from software_engineering_team.shared.repo_writer import (
        NO_FILES_TO_WRITE_MSG,
        write_agent_output,
    )

    ok, msg = write_agent_output(tmp_path, {})
    assert ok is False
    assert msg == NO_FILES_TO_WRITE_MSG


def test_write_agent_output_all_paths_invalid(tmp_path, monkeypatch) -> None:
    from software_engineering_team.shared import repo_writer

    monkeypatch.setattr(repo_writer, "write_files_and_commit", lambda *a, **kw: (True, "ok"))
    bad = {"implement-the-thing.py": "code"}
    ok, msg = repo_writer.write_agent_output(tmp_path, {"files": bad})
    assert ok is False
    assert "rejected by path validation" in msg


def test_write_agent_output_object_input(tmp_path, monkeypatch) -> None:
    from software_engineering_team.shared import repo_writer

    captured = {}

    def fake_commit(repo_path, files, msg):
        captured["files"] = files
        captured["msg"] = msg
        return True, "ok"

    monkeypatch.setattr(repo_writer, "write_files_and_commit", fake_commit)

    output = SimpleNamespace(
        files={"app/main.py": "print('hi')"},
        suggested_commit_message="feat: app",
        gitignore_entries=["__pycache__/"],
    )
    ok, _ = repo_writer.write_agent_output(tmp_path, output, subdir="backend")
    assert ok is True
    assert "backend/app/main.py" in captured["files"]


def test_write_agent_output_gitignore_merged_into_files(tmp_path, monkeypatch) -> None:
    from software_engineering_team.shared import repo_writer

    captured = {}

    def fake_commit(repo_path, files, msg):
        captured["files"] = files
        return True, "ok"

    monkeypatch.setattr(repo_writer, "write_files_and_commit", fake_commit)
    output = SimpleNamespace(files={"app/main.py": "x"}, gitignore_entries=["dist/"])
    ok, _ = repo_writer.write_agent_output(tmp_path, output)
    assert ".gitignore" in captured["files"]
    assert "dist/" in captured["files"][".gitignore"]
