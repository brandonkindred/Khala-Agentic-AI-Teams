"""Shared AST-walk constants for the indicator-coverage probe (#448).

Defines the AND/OR combinator strategy objects and block-field/subcondition
caps shared by :mod:`subcondition_visitor`, :mod:`predicate_resolution`,
and :mod:`subcond_builder`. The reporting pipeline (result dataclasses and
``CoverageAggregator``) that used to live here now lives in
:mod:`aggregator_report`; the public entry point ``run_indicator_probe`` is
re-exported from there via ``coverage_probe/__init__.py``.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Callable, Literal

import pandas as pd

_MAX_SUBCONDITIONS = 16
_MAX_LIKELY_BLOCKERS = 6


@dataclass(frozen=True)
class _CombinatorOps:
    """Strategy object parameterising AND vs OR compound-subcond building."""

    reduce: Callable[[pd.Series, pd.Series], pd.Series]
    identity: bool
    combine_symbols: Callable[[frozenset, frozenset], frozenset]
    on_unknown_term: Literal["abort", "track"]
    expose_or_legs: bool


_AND_OPS = _CombinatorOps(
    reduce=operator.and_,
    identity=True,
    combine_symbols=frozenset.__and__,
    on_unknown_term="abort",
    expose_or_legs=False,
)

_OR_OPS = _CombinatorOps(
    reduce=operator.or_,
    identity=False,
    combine_symbols=frozenset.__or__,
    on_unknown_term="track",
    expose_or_legs=True,
)


# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


_BLOCK_FIELDS = ("body", "orelse", "finalbody")
