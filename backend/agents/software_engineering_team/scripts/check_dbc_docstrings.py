"""Lint check: public functions/methods must document a Design by Contract docstring.

Per CLAUDE.md's DbC mandate, scoped to ``tech_lead_agent/``,
``coding_team_orchestrator.py``, and ``shared/cache/`` (see the sibling audit
tracked under the "Enforce Design by Contract docstrings in tech_lead_agent and
coding_team_orchestrator" issue) rather than repo-wide, to avoid a large
unrelated failure surface elsewhere in the codebase. Directory targets skip any
``tests/`` subdirectory — test functions were never in scope for this checker.

Run directly: ``python -m software_engineering_team.scripts.check_dbc_docstrings``
(defaults to the locations above) or pass explicit paths on the command line.
Exits 1 and prints one line per violation when any public function/method is
missing a docstring or is missing a ``Preconditions:`` or ``Postconditions:``
section header; exits 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Iterable, List, Sequence

_TEAM_DIR = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _TEAM_DIR.parent.parent
_DEFAULT_TARGETS = [
    _TEAM_DIR / "tech_lead_agent",
    _TEAM_DIR / "coding_team_orchestrator.py",
    _BACKEND_DIR / "shared" / "cache",
]
_REQUIRED_SECTIONS = ("Preconditions:", "Postconditions:")
_SKIPPED_DIR_NAMES = {"tests"}


def _is_public(name: str) -> bool:
    """Whether a function/method name is in scope for the DbC docstring check.

    Preconditions:
        - ``name`` is a function or method identifier from the AST (never empty).
    Postconditions:
        - Returns False for private names (a leading underscore) and dunder methods
          (e.g. ``__init__``) — their contract is either an implementation detail or
          self-evident from the language, so neither is required to spell it out in
          prose. Returns True for every other name.
    """
    return not name.startswith("_")


def _missing_sections(node: ast.AST) -> List[str]:
    """The required DbC section headers absent from a function's docstring.

    Preconditions:
        - ``node`` is an ``ast.FunctionDef`` or ``ast.AsyncFunctionDef``.
    Postconditions:
        - Returns a list containing "docstring" when the function has no docstring
          at all, else the subset of ``_REQUIRED_SECTIONS`` that does not appear as
          its own line (ignoring surrounding whitespace) in the docstring. A section
          name occurring only in prose (not on its own line) does not count — this
          requires an actual header, not an incidental mention. Returns ``[]`` when
          the docstring contains every required header (a fully-compliant function).
    """
    doc = ast.get_docstring(node)
    if not doc:
        return ["docstring"]
    header_lines = {line.strip() for line in doc.splitlines()}
    return [section for section in _REQUIRED_SECTIONS if section not in header_lines]


def _iter_function_defs(node: ast.AST) -> Iterable[ast.AST]:
    """Every function/method definition reachable from ``node`` without crossing a def boundary.

    Preconditions:
        - ``node`` is a parsed module (``ast.parse`` result) or, for the recursive calls this
          function makes on itself, any node found while descending through one.
    Postconditions:
        - Yields each ``FunctionDef``/``AsyncFunctionDef`` reached by descending through every
          other node type (module, class bodies including nested classes, and every structural
          suite — ``if``/``for``/``while``/``with``/``try``/``except``/``match``/``case`` and any
          future statement kind — at any depth), so a method hidden behind a class-level ``if``,
          an ``except`` handler, a ``match`` case, or defined on a nested class is still found.
          Functions nested inside another function (closures/local helpers) are never yielded —
          this walk does not recurse into a function's own body — since they are implementation
          details of their enclosing function, not part of the module's public contract surface.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child
        else:
            yield from _iter_function_defs(child)


def check_file(path: Path) -> List[str]:
    """Check one Python file for public functions/methods missing a DbC docstring.

    Preconditions:
        - ``path`` is a readable ``.py`` file.
    Postconditions:
        - Returns one formatted ``"path:lineno: name missing {sections}"`` message
          per public function/method whose docstring is missing or lacks a required
          section; ``[]`` when the file fully complies.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in _iter_function_defs(tree):
        if not _is_public(node.name):
            continue
        missing = _missing_sections(node)
        if missing:
            violations.append(f"{path}:{node.lineno}: {node.name} missing {', '.join(missing)}")
    return violations


def check_paths(paths: Sequence[Path]) -> List[str]:
    """Check every ``.py`` file reachable from ``paths`` (files and/or directories).

    Preconditions:
        - Each entry in ``paths`` exists on disk (a file or a directory).
    Postconditions:
        - Returns the concatenation of ``check_file``'s results across every ``.py``
          file found — files given directly, plus every ``.py`` file discovered
          recursively under any directory entry, excluding files under a ``tests/``
          subdirectory at any depth (test functions are never in scope for this
          checker) — in a deterministic (sorted) order.
    """
    files: List[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(
                    p
                    for p in path.rglob("*.py")
                    if not _SKIPPED_DIR_NAMES.intersection(p.relative_to(path).parts[:-1])
                )
            )
        else:
            files.append(path)
    violations: List[str] = []
    for file in files:
        violations.extend(check_file(file))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: check the given (or default) paths and report violations.

    Preconditions:
        - ``argv`` is None (use ``sys.argv``) or an explicit argument list of paths.
    Postconditions:
        - Prints one line per violation to stdout and returns 1 when any are found;
          prints nothing and returns 0 when the checked paths fully comply.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=_DEFAULT_TARGETS,
        help="Files or directories to check (default: tech_lead_agent/, coding_team_orchestrator.py, shared/cache/)",
    )
    args = parser.parse_args(argv)
    violations = check_paths(args.paths)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
