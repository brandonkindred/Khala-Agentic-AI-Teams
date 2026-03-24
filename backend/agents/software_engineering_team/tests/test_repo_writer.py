"""Unit tests for repo_writer, including NO_FILES_TO_WRITE handling."""

import subprocess
from pathlib import Path

from software_engineering_team.shared.repo_writer import (
    NO_FILES_TO_WRITE_MSG,
    write_agent_output,
)


def test_no_files_to_write_constant() -> None:
    """NO_FILES_TO_WRITE_MSG is the expected string."""
    assert NO_FILES_TO_WRITE_MSG == "No files to write"


def test_write_agent_output_returns_no_files_to_write_for_empty_files(tmp_path: Path) -> None:
    """write_agent_output returns (False, NO_FILES_TO_WRITE_MSG) when output has no files."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "x").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "x"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    ok, msg = write_agent_output(tmp_path, {"files": {}}, subdir="")
    assert ok is False
    assert msg == NO_FILES_TO_WRITE_MSG


def test_write_agent_output_returns_no_files_to_write_for_dict_with_empty_files(
    tmp_path: Path,
) -> None:
    """write_agent_output returns NO_FILES_TO_WRITE when output dict has empty files."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "x").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "x"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    ok, msg = write_agent_output(
        tmp_path, {"files": {}, "suggested_commit_message": "x"}, subdir=""
    )
    assert ok is False
    assert msg == NO_FILES_TO_WRITE_MSG
