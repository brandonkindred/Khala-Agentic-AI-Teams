"""read_repo_code_budgeted is best-effort: mid-walk errors degrade, never raise."""

from __future__ import annotations

from pathlib import Path

from shared_repo_context import read_repo_code_budgeted


def test_unreadable_file_is_skipped_not_raised(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "good.py").write_text("print('ok')")
    (tmp_path / "bad.py").write_text("print('secret')")

    real_read_text = Path.read_text

    def _read(self: Path, *a, **k):
        if self.name == "bad.py":
            raise PermissionError("denied")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _read)
    out = read_repo_code_budgeted(
        tmp_path, extensions={".py"}, exclude_dirs={".git"}, max_chars=10_000
    )
    assert "good.py" in out
    assert "secret" not in out


def test_mid_walk_error_degrades_instead_of_raising(tmp_path: Path, monkeypatch) -> None:
    """A filesystem race mid-scan (e.g. a dir deleted by a parallel build) must not
    escape the scanner and fail the calling workflow; it degrades to whatever was
    gathered (here: nothing, since the walk is materialized up front)."""
    (tmp_path / "a.py").write_text("A = 1")

    def _boom(self: Path, pattern: str):
        yield tmp_path / "a.py"
        raise FileNotFoundError("directory vanished mid-walk")

    monkeypatch.setattr(Path, "rglob", _boom)
    out = read_repo_code_budgeted(
        tmp_path, extensions={".py"}, exclude_dirs={".git"}, max_chars=10_000
    )
    assert out == "# No code files found"  # degraded, no exception escaped
