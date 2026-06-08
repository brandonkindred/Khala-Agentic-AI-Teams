"""``BarPredicate`` intermediate representation for the indicator-coverage probe.

This module is the stable contract between the *extractor* (the AST walker in
``indicator_probe.py`` that turns a strategy's ``on_bar`` predicates into a
tree) and the *aggregator* (the report builder that walks the tree to compute
hit rates, classify blockers, and assemble the coverage report).

The tree shape itself carries the structure that used to live as boolean flags
on the old ``_Group`` / ``_Subcond`` dataclasses: ``AndOp`` vs ``OrOp`` encodes
the combinator, nesting encodes AND-required ancestors of an OR, ``SymbolGate``
wrapping encodes per-symbol filters, and the ``unknown`` flags encode
un-modelled conjuncts/alternatives. A new combinator becomes a new IR variant
rather than a new flag threaded through every reader.

The IR is deliberately self-contained: it depends only on ``pandas`` (for leaf
evaluation) and the standard library — no ``ast`` and no coverage-report models
— so the extractor and aggregator can be exercised and fuzzed independently
through this layer.

Pure: no I/O, no LLM, no subprocess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple, Union

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BarPredicate IR nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaskLeaf:
    """A terminal: a single evaluable comparison or truthiness mask."""

    label: str
    evaluator: Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True)
class Static:
    """A terminal whose mask is a constant.

    ``Static(True)`` is used as the inner body of a pure symbol-gate leg
    (e.g. ``bar.symbol == "AAPL"`` as a standalone OR alternative — the
    enclosing :class:`SymbolGate` restricts the leg to AAPL bars where
    the predicate is unconditionally true).
    """

    value: bool


@dataclass(frozen=True)
class SymbolGate:
    """Wraps an inner predicate, restricting it to specific symbols.

    Replaces ``_Subcond.target_symbols`` / ``_Group.target_symbols``.
    During evaluation, when the current symbol is not in ``syms`` the
    inner mask is forced to all-False.
    """

    syms: frozenset
    inner: "BarPredicate"


@dataclass(frozen=True)
class AndOp:
    """Conjunction: all legs must hold.

    ``unknown=True`` when at least one original conjunct was un-modellable
    (e.g. ``self.custom_ok(bar)``). The recognised legs' mask is then
    only a SUPERSET of the real predicate, so the aggregator must not
    conclude ``COVERAGE_OK`` from them alone. Replaces
    ``_Group.has_unknown_and_conjunct``.
    """

    legs: Tuple["BarPredicate", ...]
    unknown: bool = False


@dataclass(frozen=True)
class OrOp:
    """Disjunction: at least one leg must hold.

    ``unknown=True`` when at least one original alternative was
    un-modellable. With an un-modelled alternative present we can't
    prove the OR is unreachable, so the aggregator must suppress
    ``or_group_never_fires`` / zero-hit blockers under this node.
    Replaces ``_Group.has_unknown_or_leg`` and ``_Subcond.has_unknown_leg``.
    """

    legs: Tuple["BarPredicate", ...]
    unknown: bool = False


@dataclass(frozen=True)
class Leg:
    """One reportable subcondition in :class:`CoverageReport.subconditions`.

    Wraps a sub-tree with the human-readable label used in coverage
    output. The wrapped sub-tree is what's evaluated for the leg's
    hits; the label is what's displayed. Leg boundaries also define
    the granularity of blocker classification: a leg in AND context
    can be flagged zero-hit, alternatives directly under an :class:`OrOp`
    are classified as an OR group, and deeper structure inside a leg
    is internal (not separately reported).
    """

    label: str
    inner: "BarPredicate"


BarPredicate = Union[AndOp, OrOp, SymbolGate, MaskLeaf, Static, Leg]


@dataclass(frozen=True)
class PredicateGroup:
    """One ``if``-predicate's worth of coverage-relevant content.

    ``tree`` is the :data:`BarPredicate` IR — its shape encodes the
    combinator (AND vs OR), AND-required ancestors of an OR (as
    ``AndOp(legs=(anc, OrOp(alts)))``), symbol filters (``SymbolGate``
    wrapping), and un-modelled conjuncts/alternatives (``AndOp.unknown``
    / ``OrOp.unknown``). Empty-symbol intersections (e.g. two
    ``bar.symbol == "X"`` and ``bar.symbol == "Y"`` conjoined) collapse
    to a ``SymbolGate(frozenset(), ...)`` and the group is dropped
    before emission.

    ``denied_symbols`` carries the exclude-shaped early-return denylist
    (``if bar.symbol == "AAPL": return``) — symbols dropped from
    evaluation regardless of any positive ``SymbolGate``. Independent
    of the tree's positive filters: a group can have an allowlist gate
    AND a denylist; effective scope is
    ``tree_symbols ∩ (universe - denied_symbols)``.
    """

    tree: BarPredicate
    denied_symbols: Optional[frozenset] = None


# ---------------------------------------------------------------------------
# Tree utilities
# ---------------------------------------------------------------------------


def _strip_symbol_gates(node: BarPredicate) -> Tuple[BarPredicate, Optional[frozenset]]:
    """Peel any outer :class:`SymbolGate` nodes off *node* and return the inner
    predicate plus the intersected gate symbols. ``None`` symbols means
    no gate at this level. Used to inspect the structural shape of a
    sub-tree without fragile dispatch on ``SymbolGate``.
    """
    syms: Optional[frozenset] = None
    while isinstance(node, SymbolGate):
        syms = node.syms if syms is None else (syms & node.syms)
        node = node.inner
    return node, syms


def _tree_and_unknown(node: BarPredicate) -> bool:
    """Return True if any :class:`AndOp` in the tree has ``unknown=True``."""
    if isinstance(node, AndOp):
        if node.unknown:
            return True
        return any(_tree_and_unknown(leg) for leg in node.legs)
    if isinstance(node, OrOp):
        return any(_tree_and_unknown(leg) for leg in node.legs)
    if isinstance(node, (SymbolGate, Leg)):
        return _tree_and_unknown(node.inner)
    return False


def _tree_or_unknown(node: BarPredicate) -> bool:
    """Return True if any :class:`OrOp` in the tree has ``unknown=True``."""
    if isinstance(node, OrOp):
        if node.unknown:
            return True
        return any(_tree_or_unknown(leg) for leg in node.legs)
    if isinstance(node, AndOp):
        return any(_tree_or_unknown(leg) for leg in node.legs)
    if isinstance(node, (SymbolGate, Leg)):
        return _tree_or_unknown(node.inner)
    return False


def _collect_legs(
    node: BarPredicate, accumulated_syms: Optional[frozenset] = None
) -> List[Tuple[Leg, Optional[frozenset], bool, Optional[int]]]:
    """Walk the tree, collecting each :class:`Leg` along with its
    effective symbol filter (intersection of all enclosing
    :class:`SymbolGate` syms), whether it sits directly under an
    :class:`OrOp` (i.e. is an OR alternative rather than an AND-required
    leg), and an integer identifier of the closest enclosing
    :class:`OrOp` (used to group OR alternatives for the "all-zero OR"
    blocker). Stops at each :class:`Leg` — does *not* descend into
    ``Leg.inner``.
    """
    return _collect_legs_walk(node, accumulated_syms, in_or=False, or_id=None, next_or_id=[0])


def _collect_legs_walk(
    node: BarPredicate,
    accumulated_syms: Optional[frozenset],
    in_or: bool,
    or_id: Optional[int],
    next_or_id: List[int],
) -> List[Tuple[Leg, Optional[frozenset], bool, Optional[int]]]:
    if isinstance(node, Leg):
        return [(node, accumulated_syms, in_or, or_id)]
    if isinstance(node, SymbolGate):
        new_syms = node.syms if accumulated_syms is None else (accumulated_syms & node.syms)
        return _collect_legs_walk(node.inner, new_syms, in_or, or_id, next_or_id)
    if isinstance(node, AndOp):
        results: List[Tuple[Leg, Optional[frozenset], bool, Optional[int]]] = []
        for leg in node.legs:
            results.extend(
                _collect_legs_walk(
                    leg, accumulated_syms, in_or=False, or_id=None, next_or_id=next_or_id
                )
            )
        return results
    if isinstance(node, OrOp):
        this_or_id = next_or_id[0]
        next_or_id[0] += 1
        results = []
        for leg in node.legs:
            results.extend(
                _collect_legs_walk(
                    leg, accumulated_syms, in_or=True, or_id=this_or_id, next_or_id=next_or_id
                )
            )
        return results
    return []


def _tree_effective_symbols(node: BarPredicate) -> Optional[frozenset]:
    """Return the union of symbols any leg in the tree could fire on, or
    ``None`` when at least one leg is symbol-unconstrained (universal).

    AND combinator: a single universal leg leaves the AND unconstrained
    only if no other AND leg gates the symbol space — but for warmup
    sizing we conservatively treat the AND as universal whenever any
    leg is universal (the predicate could fire on any symbol from a
    universal leg's perspective). OR combinator: a single universal
    alternative makes the disjunction universal.

    This function powers warmup sizing in :func:`_union_target_symbols`
    and group-level symbol resolution.
    """
    legs = _collect_legs(node)
    union: set = set()
    saw_universal = False
    for _leg, syms, _in_or, _or_id in legs:
        if syms is None:
            saw_universal = True
        else:
            union |= syms
    if saw_universal:
        return None
    return frozenset(union) if union else None


def _find_or_groups(node: BarPredicate) -> List[Tuple[int, OrOp]]:
    """Return a list of (or_id, OrOp) pairs in the same order that
    :func:`_collect_legs` assigns or_ids — used so the classifier can
    look up the :class:`OrOp` (for ``unknown`` flag) of each OR group
    it sees in a leg's metadata.
    """
    out: List[Tuple[int, OrOp]] = []
    _find_or_groups_walk(node, out, next_or_id=[0])
    return out


def _find_or_groups_walk(
    node: BarPredicate, out: List[Tuple[int, OrOp]], next_or_id: List[int]
) -> None:
    if isinstance(node, OrOp):
        this_id = next_or_id[0]
        next_or_id[0] += 1
        out.append((this_id, node))
        for leg in node.legs:
            _find_or_groups_walk(leg, out, next_or_id)
        return
    if isinstance(node, AndOp):
        for leg in node.legs:
            _find_or_groups_walk(leg, out, next_or_id)
        return
    if isinstance(node, (SymbolGate, Leg)):
        _find_or_groups_walk(node.inner, out, next_or_id)


def _eval_tree(node: BarPredicate, df: pd.DataFrame, symbol: str) -> pd.Series:
    """Recursively evaluate *node* against *df* for *symbol*, returning a
    boolean :class:`pandas.Series` indexed by ``df.index``.

    The :class:`SymbolGate` dispatch is the per-symbol filter: when
    ``symbol`` is not in the gate's ``syms``, the entire sub-tree below
    it evaluates to all-False without invoking the inner mask.
    """
    if isinstance(node, MaskLeaf):
        try:
            series = node.evaluator(df)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover — defensive
            logger.debug("subcondition %r failed on %s: %s", node.label, symbol, exc)
            return pd.Series(False, index=df.index, dtype=bool)
        return pd.Series(series, index=df.index).fillna(False).astype(bool)
    if isinstance(node, Static):
        return pd.Series(node.value, index=df.index, dtype=bool)
    if isinstance(node, SymbolGate):
        if symbol not in node.syms:
            return pd.Series(False, index=df.index, dtype=bool)
        return _eval_tree(node.inner, df, symbol)
    if isinstance(node, Leg):
        return _eval_tree(node.inner, df, symbol)
    if isinstance(node, AndOp):
        if not node.legs:
            return pd.Series(True, index=df.index, dtype=bool)
        result = _eval_tree(node.legs[0], df, symbol)
        for leg in node.legs[1:]:
            result = result & _eval_tree(leg, df, symbol)
        return result
    if isinstance(node, OrOp):
        if not node.legs:
            return pd.Series(False, index=df.index, dtype=bool)
        result = _eval_tree(node.legs[0], df, symbol)
        for leg in node.legs[1:]:
            result = result | _eval_tree(leg, df, symbol)
        return result
    raise AssertionError(f"unknown BarPredicate variant: {type(node)!r}")  # pragma: no cover


def _leg_gate_symbols(leg: Leg) -> Optional[frozenset]:
    """Return the outermost :class:`SymbolGate` ``syms`` of *leg*'s inner
    sub-tree, or ``None`` if the leg has no outer gate. Used by the
    visitor to propagate a compound leg's effective symbol scope to the
    group level so sibling AND-conjuncts inherit it.
    """
    inner = leg.inner
    if isinstance(inner, SymbolGate):
        return inner.syms
    return None


def _build_and_group(
    legs: List[Leg],
    effective_symbols: Optional[set],
    effective_unknown: bool,
    denied_symbols: Optional[frozenset],
) -> PredicateGroup:
    """Assemble a :class:`PredicateGroup` whose root predicate is an
    :class:`AndOp` over *legs*, optionally wrapped in a
    :class:`SymbolGate` when ``effective_symbols`` constrains the
    group.

    ``effective_unknown`` becomes ``AndOp.unknown`` — replaces the old
    ``_Group.has_unknown_and_conjunct`` flag.
    """
    tree: BarPredicate = AndOp(legs=tuple(legs), unknown=effective_unknown)
    if effective_symbols is not None:
        tree = SymbolGate(syms=frozenset(effective_symbols), inner=tree)
    return PredicateGroup(tree=tree, denied_symbols=denied_symbols)


def _build_or_group(
    ancestor_legs: List[Leg],
    or_alt_legs: List[Leg],
    or_unknown: bool,
    effective_symbols: Optional[set],
    ancestor_unknown: bool,
    denied_symbols: Optional[frozenset],
) -> PredicateGroup:
    """Assemble a :class:`PredicateGroup` for an ``if A or B or C:`` shape,
    optionally with AND-required ancestors from enclosing ``if``s.

    Tree shape:

    * No ancestors → root is :class:`OrOp` over *or_alt_legs*.
    * With ancestors → ``AndOp(legs=(*ancestor_legs, OrOp(legs=or_alt_legs)))``.

    ``or_unknown`` becomes the :class:`OrOp` ``unknown`` flag (replaces
    the old ``_Group.has_unknown_or_leg``); ``ancestor_unknown`` becomes
    the :class:`AndOp` ``unknown`` flag when ancestors are present
    (replaces ``_Group.has_unknown_and_conjunct``). Optional outer
    :class:`SymbolGate` wraps the root when *effective_symbols* is set.
    """
    or_node = OrOp(legs=tuple(or_alt_legs), unknown=or_unknown)
    if ancestor_legs:
        tree: BarPredicate = AndOp(
            legs=tuple(ancestor_legs) + (or_node,),
            unknown=ancestor_unknown,
        )
    else:
        tree = or_node
    if effective_symbols is not None:
        tree = SymbolGate(syms=frozenset(effective_symbols), inner=tree)
    return PredicateGroup(tree=tree, denied_symbols=denied_symbols)


# ---------------------------------------------------------------------------
# Canonical rendering
# ---------------------------------------------------------------------------


def render_bar_predicate(node: BarPredicate, indent: int = 0) -> str:
    """Render *node* as a stable, deterministic, indented text tree.

    This is the canonical textual form of the IR, used for snapshot /
    regression assertions and for debugging probe output. It is
    independent of object identity: a :class:`MaskLeaf` renders by its
    ``label`` only — the ``evaluator`` is a closure with an unstable
    ``repr`` and is deliberately omitted — and every symbol set is
    emitted in sorted order so the output is insensitive to set
    iteration order.

    Preconditions:
        ``node`` is a :data:`BarPredicate`; ``indent >= 0``.
    Postconditions:
        Returns a newline-joined string indented two spaces per level.
        IR trees equal by shape, labels, ``unknown`` flags and gate
        symbols render to byte-identical strings.
    """
    assert indent >= 0, "indent must be non-negative"
    pad = "  " * indent
    if isinstance(node, MaskLeaf):
        return f"{pad}Mask({node.label})"
    if isinstance(node, Static):
        return f"{pad}Static({node.value})"
    if isinstance(node, SymbolGate):
        syms = ", ".join(sorted(node.syms))
        return f"{pad}SymbolGate({{{syms}}})\n" + render_bar_predicate(node.inner, indent + 1)
    if isinstance(node, Leg):
        return f"{pad}Leg({node.label})\n" + render_bar_predicate(node.inner, indent + 1)
    if isinstance(node, AndOp):
        lines = [f"{pad}And(unknown={node.unknown})"]
        lines += [render_bar_predicate(leg, indent + 1) for leg in node.legs]
        return "\n".join(lines)
    if isinstance(node, OrOp):
        lines = [f"{pad}Or(unknown={node.unknown})"]
        lines += [render_bar_predicate(leg, indent + 1) for leg in node.legs]
        return "\n".join(lines)
    raise AssertionError(f"unknown BarPredicate variant: {type(node)!r}")  # pragma: no cover


def render_predicate_group(group: PredicateGroup) -> str:
    """Render *group* (denylist + tree) as a stable canonical string.

    Preconditions:
        ``group`` is a :class:`PredicateGroup`.
    Postconditions:
        Returns ``"denied: {<sorted syms>}|none\\n<tree render>"`` —
        deterministic for a given group.
    """
    if group.denied_symbols:
        denied = "{" + ", ".join(sorted(group.denied_symbols)) + "}"
    else:
        denied = "none"
    return f"denied: {denied}\n" + render_bar_predicate(group.tree)
