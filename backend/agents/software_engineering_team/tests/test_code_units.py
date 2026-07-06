"""Tests for class/method unit extraction (``code_review_agent.code_units``).

Extraction is pure and must never raise: Python classes are parsed via ``ast``,
and every non-Python, blank, or unparseable input degrades to ``[]`` so the
class-cohesion pass simply does not run for it.
"""

from __future__ import annotations

from code_review_agent.code_units import (
    ClassUnit,
    MethodSummary,
    extract_classes,
)

_SAMPLE = '''\
import os


@decorator
class Cache:
    """A small in-memory cache.

    Second paragraph is dropped from the first-paragraph view.
    """

    def get(self, key: str) -> int:
        """Return the cached value."""
        return self._store[key]

    async def refresh(self, *, force: bool = False) -> None:
        # no docstring here
        self._store.clear()

    class Inner:
        def nested(self):
            pass


def free_function():
    return 1
'''


def test_extracts_top_level_class_with_methods() -> None:
    classes = extract_classes("cache.py", _SAMPLE)
    assert len(classes) == 1
    cache = classes[0]
    assert isinstance(cache, ClassUnit)
    assert cache.name == "Cache"
    assert cache.docstring.startswith("A small in-memory cache.")
    # start_line is the earliest decorator line: in _SAMPLE, `@decorator` is on
    # line 4 and `class Cache:` on line 5, so the class unit starts at line 4.
    assert cache.start_line == 4
    assert cache.end_line > cache.start_line
    # Only directly-defined methods; the inner class's ``nested`` is not hoisted.
    assert [m.name for m in cache.methods] == ["get", "refresh"]


def test_method_signature_and_docstring() -> None:
    cache = extract_classes("cache.py", _SAMPLE)[0]
    get, refresh = cache.methods
    assert isinstance(get, MethodSummary)
    assert get.signature == "def get(self, key: str) -> int:"
    assert get.docstring == "Return the cached value."
    # async methods render with the async prefix; no docstring -> "".
    assert refresh.signature == "async def refresh(self, *, force: bool=False) -> None:"
    assert refresh.docstring == ""


def test_method_line_range_within_class() -> None:
    cache = extract_classes("cache.py", _SAMPLE)[0]
    for m in cache.methods:
        assert cache.start_line <= m.start_line <= m.end_line <= cache.end_line


def test_class_without_docstring_or_methods() -> None:
    classes = extract_classes("m.py", "class Empty:\n    pass\n")
    assert len(classes) == 1
    assert classes[0].name == "Empty"
    assert classes[0].docstring == ""
    assert classes[0].methods == ()


def test_multiple_classes_in_source_order() -> None:
    src = "class A:\n    def a(self): pass\n\nclass B:\n    def b(self): pass\n"
    names = [c.name for c in extract_classes("m.py", src)]
    assert names == ["A", "B"]


def test_non_python_returns_empty() -> None:
    ts = "export class Foo {\n  bar() { return 1; }\n}\n"
    assert extract_classes("foo.ts", ts) == []
    # No extension is treated as non-Python.
    assert extract_classes("", "class X:\n    pass\n") == []


def test_parse_failure_returns_empty() -> None:
    # Syntactically invalid Python must degrade to [] rather than raise.
    assert extract_classes("bad.py", "class Broken(:\n    def x(self)\n") == []


def test_blank_and_classless_return_empty() -> None:
    assert extract_classes("m.py", "") == []
    assert extract_classes("m.py", "   \n\t\n") == []
    assert extract_classes("m.py", "x = 1\n\ndef f():\n    return x\n") == []


def test_pyi_extension_is_parsed() -> None:
    classes = extract_classes("stub.pyi", "class S:\n    def m(self) -> int: ...\n")
    assert [c.name for c in classes] == ["S"]
    assert classes[0].methods[0].signature == "def m(self) -> int:"
