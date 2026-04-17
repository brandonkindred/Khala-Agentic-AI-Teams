"""Static check: Markdown / prose prompts must not be routed into JSON methods.

Prevents recurrence of the Phase 1 failure — a prompt that asks for Markdown
being passed into a JSON-only LLM method (``complete_json`` /
``chat_json_round`` / ``generate_structured``). See
:doc:`/backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md`.

How it works
------------

1. Walks ``backend/agents/**/*.py`` and collects every module-level assignment
   of the form ``FOO_PROMPT = "..."`` (including multi-part string
   concatenation such as ``"...".join(...)`` of literal parts).
2. Flags a prompt as **text-intent** if its body contains one of the trigger
   phrases (``markdown``, ``prose``, ``as a document``) **and** does not also
   contain a JSON-mode signal (``json object``, ``respond with a json``,
   ``json schema``). The JSON-signal carve-out prevents false positives like
   "respond with a JSON object (no markdown fencing)" in
   :data:`user_agent_founder.agent.QUESTION_ANSWERING_PROMPT`.
3. Scans every call site in the same module to ``complete_json`` /
   ``chat_json_round`` / ``generate_structured`` and asserts its first
   positional argument is not a text-intent prompt name.
4. Violations that predate this check live in :data:`ALLOW_LIST` — each entry
   must name a follow-up plan or issue. If ``ALLOW_LIST`` grows beyond a
   handful, treat that as a signal that more migrations belong in the
   structured-output contract plan.

This is a best-effort AST heuristic, not a soundness proof. It catches the
common footgun (named ``*_PROMPT`` constants) while staying CI-fast.
"""

from __future__ import annotations

import ast
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENTS_ROOT = Path(__file__).resolve().parents[3]  # backend/agents/

TRIGGER_PHRASES = (
    "markdown",
    "prose",
    "as a document",
    "as a markdown",
    "markdown document",
    "write the spec",
)

# If any of these substrings appear, the prompt is asking for JSON and the
# ``markdown`` token is almost certainly a negation (e.g. "no markdown fencing").
JSON_INTENT_MARKERS = (
    "json object",
    "json schema",
    "respond with a json",
    "return a json",
    "emit only a json",
    "a json object",
    "json-decoded",
)

JSON_METHODS = {"complete_json", "chat_json_round", "generate_structured"}

# Allow-list: (relative_path_from_agents_root, prompt_name). Each entry must
# carry a trailing comment naming the follow-up. Keep this list short — more
# than a handful is a signal that migration work belongs in the plan, not
# the allow-list.
ALLOW_LIST: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _literal_string(node: ast.AST) -> str | None:
    """Return the string body of ``node`` if it is a (concatenated) string literal.

    Handles bare ``Constant(str)`` and ``BinOp`` / ``JoinedStr`` concatenations
    of string literals. Returns ``None`` for anything dynamic.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            else:
                # f-string with dynamic interpolation — still mostly a literal
                # for our heuristic, but we can't know the interpolated value.
                # Treat the literal scaffolding as the body.
                continue
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _is_text_intent(body: str) -> bool:
    lower = body.lower()
    has_trigger = any(phrase in lower for phrase in TRIGGER_PHRASES)
    if not has_trigger:
        return False
    has_json_intent = any(marker in lower for marker in JSON_INTENT_MARKERS)
    return not has_json_intent


def _collect_text_intent_prompts(tree: ast.AST) -> set[str]:
    """Return the set of ``*_PROMPT`` names whose literal body is text-intent."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        body = _literal_string(node.value)
        if body is None or not _is_text_intent(body):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_PROMPT"):
                names.add(target.id)
    return names


def _collect_json_call_violations(
    tree: ast.AST,
    text_intent_names: set[str],
) -> list[tuple[str, int]]:
    """Return a list of (prompt_name, lineno) for offending call sites."""
    violations: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name: str | None = None
        if isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        elif isinstance(node.func, ast.Name):
            func_name = node.func.id
        if func_name not in JSON_METHODS:
            continue
        # First positional arg OR ``prompt=`` kwarg.
        candidate: ast.AST | None = None
        if node.args:
            candidate = node.args[0]
        for kw in node.keywords:
            if kw.arg == "prompt":
                candidate = kw.value
                break
        if candidate is None:
            continue
        if isinstance(candidate, ast.Name) and candidate.id in text_intent_names:
            violations.append((candidate.id, node.lineno))
        elif isinstance(candidate, ast.Attribute) and candidate.attr in text_intent_names:
            violations.append((candidate.attr, node.lineno))
    return violations


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def _iter_agent_py_files():
    for path in AGENTS_ROOT.rglob("*.py"):
        # Skip this test file and any obvious non-source locations.
        if path == Path(__file__).resolve():
            continue
        parts = path.parts
        if any(p in {"__pycache__", ".venv", "venv", "node_modules"} for p in parts):
            continue
        yield path


def test_detector_catches_synthetic_violation():
    """Sanity check: the AST walker flags a crafted text-intent-into-JSON call."""
    source = '''
MY_PROMPT = """
Write a markdown document with three sections.
No strict schema — just prose.
"""

def do_thing(client):
    return client.complete_json(MY_PROMPT)
'''
    tree = ast.parse(source)
    text_intent = _collect_text_intent_prompts(tree)
    assert "MY_PROMPT" in text_intent
    violations = _collect_json_call_violations(tree, text_intent)
    assert violations == [("MY_PROMPT", 8)]


def test_detector_respects_json_intent_carveout():
    """A prompt that says 'no markdown fencing' while asking for JSON is safe."""
    source = '''
MY_PROMPT = """
Respond with a JSON object (no markdown fencing):
{ "ok": true }
"""

def do_thing(client):
    return client.complete_json(MY_PROMPT)
'''
    tree = ast.parse(source)
    text_intent = _collect_text_intent_prompts(tree)
    assert "MY_PROMPT" not in text_intent


def test_no_text_intent_prompt_passed_to_json_method():
    offenders: list[str] = []
    for path in _iter_agent_py_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        text_intent_names = _collect_text_intent_prompts(tree)
        if not text_intent_names:
            continue
        rel = path.relative_to(AGENTS_ROOT).as_posix()
        violations = _collect_json_call_violations(tree, text_intent_names)
        for name, lineno in violations:
            if (rel, name) in ALLOW_LIST:
                continue
            offenders.append(f"{rel}:{lineno}: {name} (text-intent prompt) routed into JSON method")

    assert not offenders, (
        "Text-intent prompts ('markdown', 'prose', etc. bodies) must not be passed to "
        "complete_json / chat_json_round / generate_structured. "
        "Use generate_text or client.complete() for free-form text, or rewrite the prompt "
        "to demand strict JSON output. "
        "If the flag is intentional, add an entry to ALLOW_LIST with a follow-up reference.\n"
        + "\n".join(offenders)
    )
