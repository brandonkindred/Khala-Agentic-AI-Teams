"""Insertion-merge logic for the DbC Comments agent.

Deterministically applies the LLM's anchored comment insertions
(:class:`DbcCommentInsertion`) onto the ORIGINAL source content. The LLM never
re-emits a whole file (see ``prompts.DBC_COMMENTS_PROMPT``); this module is
what actually produces merged file content, replacing the old whole-file-
rewrite path. Every function here is pure and LLM-free.

Python files (``.py``) are anchored via ``ast``: an insertion's ``symbol`` is
matched to a module/function/class by name, disambiguated by ``line`` when
the name is not unique. Whatever the model's ``action`` says, the actual
merge always replaces an existing docstring if one is present and inserts a
new one otherwise -- the structural check (does the target's first statement
already look like a docstring?) is more reliable than trusting the model's
self-reported label.

Non-Python files (typescript, java, ...) have no parser available here, so
insertions are anchored by line number only: the comment is inserted
immediately above the given line, matching that line's leading whitespace.
There is no reliable way to detect an existing comment block without a
parser, so accepted insertions are always a fresh addition, never a
structural replace (unlike the Python path). As a conservative guard against
duplicating a comment the model meant to replace, an insertion is rejected
outright when a `*/`-terminated block already ends on the line immediately
above the target -- this can't distinguish "meant to replace this" from "an
unrelated comment happens to be adjacent," so it always rejects rather than
guesses. It's still possible to duplicate a comment that isn't immediately
adjacent to the target line -- a known limitation, not a bug.

Both merge paths reject (skip, without touching the file) any insertion
that cannot be safely anchored, without ever corrupting the file. Rejection
reasons differ by path since only the Python path has symbols to resolve:
the Python path rejects an ambiguous or missing symbol; the generic path
rejects an out-of-range or missing line, or a line immediately preceded by
an existing comment block. Both paths reject an unknown file and a
duplicate target already claimed by an earlier insertion in the same batch.
"""

from __future__ import annotations

import ast
import logging
from typing import Dict, List, Optional, Set, Tuple

from software_engineering_team.shared.code_completeness import reject_invalid_python

from .chunking import parse_code_into_file_blocks
from .models import DbcCommentInsertion

logger = logging.getLogger(__name__)

_MODULE_SYMBOL_ALIASES = frozenset({"", "module docstring", "module", "<module>", "file"})


def apply_dbc_insertions(
    code: str, insertions: List[DbcCommentInsertion]
) -> Tuple[Dict[str, str], int, int, List[str]]:
    """Merge ``insertions`` onto the original files parsed out of ``code``.

    Preconditions:
        - ``code`` is the same concatenated, ``### path ###``-headered (or
          single headerless) source the insertions were generated against.

    Postconditions:
        - Returns ``(files, comments_added, comments_updated, rejected)``.
        - ``files`` maps path -> merged content, containing ONLY files where
          at least one insertion was safely applied; every ``.py`` entry has
          already passed :func:`reject_invalid_python`. A file with no
          successfully applied insertion is omitted entirely -- its pre-DbC
          content is simply not returned, never corrupted.
        - ``comments_added``/``comments_updated`` count only insertions that
          were actually applied (added = no prior docstring found at the
          anchor, updated = an existing one was replaced); non-Python
          insertions always count as ``added`` (see module docstring).
        - ``rejected`` holds one human-readable reason per insertion that
          could not be safely applied. Pure; raises nothing.
    """
    original_files = _resolve_original_files(code, insertions)

    by_file: Dict[str, List[DbcCommentInsertion]] = {}
    for ins in insertions:
        by_file.setdefault(ins.file, []).append(ins)

    files: Dict[str, str] = {}
    comments_added = 0
    comments_updated = 0
    rejected: List[str] = []

    for file, file_insertions in by_file.items():
        original = original_files.get(file)
        if original is None:
            rejected.extend(
                f"{file}: unknown file (not present in the reviewed code)" for _ in file_insertions
            )
            continue

        if file.endswith(".py"):
            merged, added, updated, file_rejected = _merge_python_file(original, file_insertions)
        else:
            merged, added, updated, file_rejected = _merge_generic_file(original, file_insertions)
        rejected.extend(f"{file}: {reason}" for reason in file_rejected)

        if merged is None:
            continue

        if file.endswith(".py"):
            valid, invalid = reject_invalid_python({file: merged})
            if file not in valid:
                rejected.append(
                    f"{file}: merged output failed the post-merge syntax check ({invalid[file]})"
                )
                continue

        files[file] = merged
        comments_added += added
        comments_updated += updated

    return files, comments_added, comments_updated, rejected


def _resolve_original_files(code: str, insertions: List[DbcCommentInsertion]) -> Dict[str, str]:
    """Reconstruct ``{path: content}`` from the concatenated review input.

    Postconditions:
        - When ``code`` carries ``### path ###`` headers, returns one entry
          per header, last occurrence wins on a repeated path.
        - When ``code`` is a single headerless blob and every insertion
          targets the same file path, that single blob is attributed to that
          path -- the common single-file-review case (and what the unit
          tests exercise). Otherwise returns {} (nothing to safely anchor to).
    """
    blocks = parse_code_into_file_blocks(code)
    if len(blocks) == 1 and blocks[0][0] == "":
        target_paths = {ins.file for ins in insertions if ins.file}
        if len(target_paths) == 1:
            return {next(iter(target_paths)): blocks[0][1]}
        return {}
    return {path: content for path, content in blocks if path}


def _find_python_target(tree: ast.AST, symbol: str, line: Optional[int]) -> Optional[ast.AST]:
    """Resolve ``symbol`` to the ast node it names, or None if unsafe to anchor.

    Postconditions:
        - A module-level alias (blank, "module docstring", ...) resolves to
          ``tree`` itself.
        - A uniquely-named function/class resolves directly.
        - A name shared by multiple nodes resolves only when ``line`` picks
          out exactly one of them by its ``def``/``class`` line; otherwise
          None (ambiguous -- never guessed at).
    """
    normalized = (symbol or "").strip().lower()
    if normalized in _MODULE_SYMBOL_ALIASES:
        return tree
    target_name = symbol.strip()
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == target_name
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1 and line is not None:
        exact = [n for n in candidates if n.lineno == line]
        if len(exact) == 1:
            return exact[0]
    return None


def _is_existing_docstring(stmt: ast.stmt) -> bool:
    """True when ``stmt`` (a body's first statement) is a docstring literal."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _choose_quote(text: str) -> Optional[str]:
    """Pick a triple-quote delimiter that does not collide with ``text``.

    Postconditions:
        - Returns a triple-double-quote delimiter when absent from ``text``,
          else a triple-single-quote delimiter when that is absent, else
          None when both appear (unsafe to render).
    """
    if '"""' not in text:
        return '"""'
    if "'''" not in text:
        return "'''"
    return None


def _strip_outer_quotes(text: str, quotes: Tuple[str, ...]) -> str:
    """Strip a matching outer wrapper (e.g. the model's own ``\"\"\"``) so
    callers control the exact re-quoting themselves instead of double-wrapping."""
    for q in quotes:
        if text.startswith(q) and text.endswith(q) and len(text) >= 2 * len(q):
            return text[len(q) : -len(q)].strip("\n")
    return text


def _split_lines(original: str) -> List[str]:
    """Split ``original`` into lines, each guaranteed to end with ``\\n``.

    Postconditions:
        - Every returned line ends with ``\\n``, including the last, so a
          caller can always safely slice-and-splice without checking.
    """
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def _render_python_docstring(comment: str, indent: str) -> Optional[List[str]]:
    """Render ``comment`` as an indented Python docstring block, or None if unsafe."""
    text = _strip_outer_quotes(comment.strip(), ('"""', "'''"))
    quote = _choose_quote(text)
    if quote is None:
        return None
    body_lines = text.splitlines() if text else [""]
    rendered = [f"{indent}{quote}"]
    rendered.extend(f"{indent}{line}" if line.strip() else "" for line in body_lines)
    rendered.append(f"{indent}{quote}")
    return rendered


def _merge_python_file(
    original: str, insertions: List[DbcCommentInsertion]
) -> Tuple[Optional[str], int, int, List[str]]:
    """Apply ``insertions`` to one Python file's content via ast-anchored edits."""
    try:
        tree = ast.parse(original)
    except SyntaxError as exc:
        return None, 0, 0, [f"original file has invalid Python syntax, cannot anchor: {exc}"]

    lines = _split_lines(original)

    edits: List[Tuple[int, int, List[str]]] = []
    used_targets: Set[int] = set()
    added = 0
    updated = 0
    rejected: List[str] = []

    for ins in insertions:
        target = _find_python_target(tree, ins.symbol, ins.line)
        if target is None:
            rejected.append(
                f"could not anchor symbol '{ins.symbol}' (line={ins.line}): not found or ambiguous"
            )
            continue
        if id(target) in used_targets:
            rejected.append(
                f"duplicate insertion for symbol '{ins.symbol}': already handled by an earlier "
                "insertion in this batch"
            )
            continue
        body = getattr(target, "body", None)
        if not body and target is not tree:
            rejected.append(f"symbol '{ins.symbol}' has an empty body, cannot anchor")
            continue

        # An empty module body (a file with only whitespace/comments, or truly
        # empty) has no first statement to anchor against, but the module
        # itself still exists -- a docstring can always be inserted at the top.
        first_stmt = body[0] if body else None

        if first_stmt is not None and target is not tree:
            # A one-liner def/class (e.g. "def f(): pass", or a multi-line
            # signature closed by "): pass") puts the body's first statement on
            # the same physical line as something else from the header --
            # comparing line *numbers* can't detect this reliably (end_lineno
            # spans the whole node, so it equals first_stmt.lineno for ANY
            # single-statement body, one-liner or not); instead check whether
            # everything before the statement's own column on its source line
            # is pure whitespace. If not, inserting "before" that line would
            # land the comment outside the function/class entirely, so reject
            # explicitly rather than emitting a line the post-merge syntax
            # check would only reject downstream with a confusing error.
            header_prefix = lines[first_stmt.lineno - 1][: first_stmt.col_offset]
            if header_prefix.strip():
                rejected.append(
                    f"symbol '{ins.symbol}': its body starts on the same line as its "
                    "def/class header (a one-liner), cannot anchor a comment safely"
                )
                continue
        indent = "" if first_stmt is None else " " * first_stmt.col_offset
        rendered = _render_python_docstring(ins.comment, indent)
        if rendered is None:
            location = (
                f"'{ins.symbol}' at line {ins.line}" if ins.line is not None else f"'{ins.symbol}'"
            )
            rejected.append(
                f"comment for {location} contains both quote styles, cannot safely render"
            )
            continue

        used_targets.add(id(target))
        if first_stmt is not None and _is_existing_docstring(first_stmt):
            edits.append((first_stmt.lineno - 1, first_stmt.end_lineno, rendered))
            updated += 1
        elif first_stmt is None:
            # Fresh module docstring on an empty-bodied module: also consume any
            # purely-blank leading lines (never real content -- a blank line can't
            # contain a def/class, so this can't collide with another edit's range)
            # so the docstring lands directly at the top, with no stray blank line
            # left dangling between it and the file's first real content.
            end = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
            edits.append((0, end, rendered))
            added += 1
        else:
            start = first_stmt.lineno - 1
            edits.append((start, start, rendered))
            added += 1

    if not edits:
        return None, 0, 0, rejected

    # Distinct AST targets never share lines in valid Python, so two edits'
    # ranges should never overlap -- but if that invariant is ever wrong (a
    # bug here, or an assumption this doesn't yet account for), refuse to
    # guess which edit should win. Abort the whole merge for this file rather
    # than risk silently corrupting it by applying overlapping edits.
    ordered = sorted(edits, key=lambda e: e[0])
    for (_, end, _), (next_start, _, _) in zip(ordered, ordered[1:]):
        if end > next_start:
            rejected.append(
                "internal error: overlapping edits detected while merging this file; "
                "aborting its merge entirely rather than risk corrupting it"
            )
            return None, 0, 0, rejected

    # Apply from the bottom up so an earlier (lower-line) edit's start/end
    # offsets are never invalidated by a later (higher-line) edit shifting
    # the line count.
    for start, end, rendered in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:end] = [f"{line}\n" for line in rendered]

    return "".join(lines), added, updated, rejected


def _render_block_comment(comment: str, indent: str) -> List[str]:
    """Render ``comment`` as an indented ``/** ... */`` block (JSDoc/Javadoc style).

    If ``comment`` is already a fully-formed ``/** ... */`` block (a common,
    reasonable model output), each inner line's own leading ``*``/``* ``
    marker is stripped before re-wrapping -- otherwise re-wrapping would
    double it (``/**\\n * existing\\n */`` becoming `` *  * existing``).
    """
    text = comment.strip()
    if text.startswith("/**") and text.endswith("*/"):
        raw_lines = text.splitlines()
        if len(raw_lines) == 1:
            # Single physical line, e.g. "/** short comment */": extract the
            # inner text directly, there are no delimiter lines to drop.
            body_lines = [text[3:-2].strip()]
        else:
            # The model's own opening "/**" and closing "*/" each occupy their
            # own line (the exact style shown to it in DBC_COMMENTS_PROMPT), so
            # drop those two delimiter lines and strip each remaining line's
            # own "*"/"* " marker -- this preserves interior content, including
            # any intentional blank lines, instead of losing or doubling it.
            inner_lines = raw_lines[1:-1] or [text[3:-2]]
            body_lines = [line.strip().removeprefix("*").strip() for line in inner_lines]
    else:
        body_lines = text.splitlines() if text else [""]
    rendered = [f"{indent}/**"]
    for line in body_lines:
        stripped = line.strip()
        rendered.append(f"{indent} * {stripped}" if stripped else f"{indent} *")
    rendered.append(f"{indent} */")
    return rendered


def _merge_generic_file(
    original: str, insertions: List[DbcCommentInsertion]
) -> Tuple[Optional[str], int, int, List[str]]:
    """Apply ``insertions`` to one non-Python file via line-anchored inserts."""
    lines = _split_lines(original)
    total_lines = len(lines)

    edits: List[Tuple[int, List[str]]] = []
    used_lines: Set[int] = set()
    rejected: List[str] = []
    added = 0

    for ins in insertions:
        if ins.line is None or not (1 <= ins.line <= total_lines):
            rejected.append(
                f"symbol '{ins.symbol}': no valid line anchor for a non-Python file "
                f"(line={ins.line}, file has {total_lines} line(s))"
            )
            continue
        if ins.line in used_lines:
            rejected.append(f"symbol '{ins.symbol}': duplicate insertion at line {ins.line}")
            continue
        preceding_idx = ins.line - 2
        if preceding_idx >= 0 and lines[preceding_idx].strip().endswith("*/"):
            # No parser is available for non-Python files (see module docstring),
            # so there's no reliable way to tell whether this insertion is meant
            # to replace that existing block or is simply misanchored -- reject
            # rather than risk duplicating (or corrupting the intent of) it.
            rejected.append(
                f"symbol '{ins.symbol}': an existing comment block already ends immediately "
                f"above line {ins.line}, cannot safely insert without risking a duplicate"
            )
            continue
        target_line = lines[ins.line - 1]
        indent = target_line[: len(target_line) - len(target_line.lstrip())]
        rendered = _render_block_comment(ins.comment, indent)
        used_lines.add(ins.line)
        edits.append((ins.line - 1, rendered))
        added += 1

    if not edits:
        return None, 0, 0, rejected

    for start, rendered in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:start] = [f"{line}\n" for line in rendered]

    return "".join(lines), added, 0, rejected
