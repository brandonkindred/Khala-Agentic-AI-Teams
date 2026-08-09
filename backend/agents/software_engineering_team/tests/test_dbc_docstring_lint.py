"""Unit tests for the DbC docstring lint checker."""

from __future__ import annotations

import textwrap
from pathlib import Path

from software_engineering_team.scripts.check_dbc_docstrings import (
    _DEFAULT_TARGETS,
    check_file,
    check_paths,
)


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def test_compliant_function_has_no_violations(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '''
        def do_thing(x):
            """Do a thing.

            Preconditions:
                - x is an int.
            Postconditions:
                - Returns x doubled.
            """
            return x * 2
        ''',
    )
    assert check_file(path) == []


def test_missing_docstring_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        def do_thing(x):
            return x * 2
        """,
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "do_thing missing docstring" in violations[0]


def test_missing_preconditions_only_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '''
        def do_thing(x):
            """Do a thing.

            Postconditions:
                - Returns x doubled.
            """
            return x * 2
        ''',
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "missing Preconditions:" in violations[0]
    assert "Postconditions:" not in violations[0].split("missing", 1)[1]


def test_missing_both_sections_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '''
        def do_thing(x):
            """Do a thing."""
            return x * 2
        ''',
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "missing Preconditions:, Postconditions:" in violations[0]


def test_prose_only_mention_is_not_accepted_as_a_header(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '''
        def do_thing(x):
            """Do a thing. No Preconditions: or Postconditions: apply here, just prose."""
            return x
        ''',
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "missing Preconditions:, Postconditions:" in violations[0]


def test_private_function_without_sections_is_not_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '''
        def _helper(x):
            """No contract needed for a private helper."""
            return x
        ''',
    )
    assert check_file(path) == []


def test_dunder_method_is_not_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        class Foo:
            def __init__(self, x):
                self.x = x
        """,
    )
    assert check_file(path) == []


def test_class_method_is_checked(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        class Foo:
            def do_thing(self, x):
                return x
        """,
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "do_thing" in violations[0]


def test_method_in_nested_class_is_checked(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        class Outer:
            class Inner:
                def do_thing(self, x):
                    return x
        """,
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "do_thing" in violations[0]


def test_method_behind_class_level_if_is_checked(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        import sys

        class Foo:
            if sys.version_info >= (3, 0):
                def do_thing(self, x):
                    return x
        """,
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "do_thing" in violations[0]


def test_function_in_except_handler_is_checked(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        try:
            pass
        except ValueError:
            def do_thing(x):
                return x
        """,
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "do_thing" in violations[0]


def test_function_in_match_case_is_checked(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        match 1:
            case 1:
                def do_thing(x):
                    return x
        """,
    )
    violations = check_file(path)
    assert len(violations) == 1
    assert "do_thing" in violations[0]


def test_nested_function_is_not_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '''
        def outer():
            """Outer.

            Postconditions:
                - Returns nothing meaningful.
            Preconditions:
                - None.
            """

            def inner():
                return 1

            return inner()
        ''',
    )
    assert check_file(path) == []


def test_check_paths_walks_directories(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text(
        textwrap.dedent(
            """
            def do_thing():
                return 1
            """
        ),
        encoding="utf-8",
    )
    violations = check_paths([pkg])
    assert len(violations) == 1
    assert "do_thing" in violations[0]


def test_check_paths_skips_tests_subdirectory(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "tests" / "test_a.py").write_text(
        textwrap.dedent(
            """
            def test_do_thing():
                return 1
            """
        ),
        encoding="utf-8",
    )
    assert check_paths([pkg]) == []


def test_default_targets_have_no_violations() -> None:
    """Regression guard: tech_lead_agent/, coding_team_orchestrator.py, and
    shared/cache/ stay compliant."""
    assert check_paths(_DEFAULT_TARGETS) == []


def test_default_targets_include_shared_cache() -> None:
    """shared/cache/ (added in the shared.cache module) must be in scan scope
    so new public functions there are enforced in CI, not just the SE team's
    own tech_lead_agent/coding_team_orchestrator files."""
    assert any(target.parts[-2:] == ("shared", "cache") for target in _DEFAULT_TARGETS)
