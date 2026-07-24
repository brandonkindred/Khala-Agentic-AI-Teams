# Design: `_write_draft_to_path` precondition enforcement

Date: 2026-07-24

## Goal

Make `_write_draft_to_path` enforce Design-by-Contract preconditions for `draft` and `path` with explicit validation and docstring documentation, so invalid callers get a clear `TypeError` before any filesystem I/O.

## Context

`backend/agents/blogging/blog_writer_agent/agent.py` defines:

```python
def _write_draft_to_path(draft: str, path: Union[str, Path]) -> None:
    """Write draft content to path; create parent dirs if needed. Log the saved path."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(draft, encoding="utf-8")
    logger.info("Draft written to %s", p)
```

The type hints claim `draft: str` and `path: Union[str, Path]`, but nothing checks them. A non-string `draft` (e.g. `None`) fails inside `Path.write_text` with an opaque runtime `TypeError`. Invalid `path` values similarly fail later inside `Path(...)`. Call sites already pass strings/`Path` objects; this change hardens the helper boundary.

Similar blogging helpers raise `TypeError` on `isinstance` failures (e.g. `blog_planning_agent`, `pipeline/_common.py`).

## Decisions

| Topic | Choice |
|---|---|
| Validation style | Explicit `isinstance` + `TypeError` (not `assert`) |
| `draft` | Must be `str` (empty string allowed) |
| `path` | Must be `str` or `Path` |
| Docstring | Add Preconditions / Postconditions sections |
| Call sites | Unchanged |
| Scope | Helper + unit tests only |

## Contract

### Preconditions

- `draft` is a `str` (may be empty).
- `path` is a `str` or `pathlib.Path`.

### Postconditions

- Parent directories of `path` exist (`mkdir(parents=True, exist_ok=True)`).
- The resolved path’s file contents equal `draft` encoded as UTF-8.
- A success log line records the resolved path.

### Error behavior

- Non-`str` `draft` → `TypeError(f"draft must be a string, got {type(draft).__name__}")` before I/O.
- `path` not `str`/`Path` → `TypeError(f"path must be a str or Path, got {type(path).__name__}")` before I/O.
- No file or directory creation occurs when a precondition fails.

## Implementation sketch

```python
def _write_draft_to_path(draft: str, path: Union[str, Path]) -> None:
    """Write draft content to path; create parent dirs if needed. Log the saved path.

    Preconditions:
        - ``draft`` must be a string (may be empty).
        - ``path`` must be a ``str`` or ``pathlib.Path``.
    Postconditions:
        - Parent directories of ``path`` exist.
        - The resolved path contains ``draft`` as UTF-8 text.
        - A success log records the resolved path.
    """
    if not isinstance(draft, str):
        raise TypeError(f"draft must be a string, got {type(draft).__name__}")
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path must be a str or Path, got {type(path).__name__}")
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(draft, encoding="utf-8")
    logger.info("Draft written to %s", p)
```

## Tests

Extend `backend/agents/blogging/tests/test_writer_and_v2_helpers.py`:

- Keep `test_write_draft_to_path_creates_parents`.
- Add rejection cases for non-string `draft` (`None`, `123`) with message match `draft must be a string`.
- Add rejection cases for invalid `path` (`None`, `123`) with message match `path must be a str or Path`.
- On rejection, assert the intended target file does not exist.

## Out of scope

- Call-site refactors
- Validating that `path` is writable or that parent creation succeeds
- Rejecting empty-string drafts
- Broader blogging helper DbC audit
