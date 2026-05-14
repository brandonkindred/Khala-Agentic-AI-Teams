"""Regex-based prose → spec_dsl adapter (issue #550, step 1 of 8 from #537).

Pure regex; no LLM; no Pydantic-side-effects beyond constructing DSL nodes.
First-match-wins ordering. Unmatched prose returns the shared ``UnparsableRule``
variant (or ``UnparsableSizing`` in the sizing slot) so callers can route the
ungroomed text back to a redesign loop.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import ValidationError

from .spec_dsl import (
    ADXRef,
    ATRRef,
    BollingerRef,
    ConstRef,
    EMARef,
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    MACDRef,
    Predicate,
    PriceRef,
    RSIRef,
    SignalExitRule,
    SMARef,
    StochasticRef,
    StopLossRule,
    TakeProfitRule,
    TimeStopRule,
    UnparsableRule,
    UnparsableSizing,
    VolatilityTargetSizing,
    VWAPRef,
)

# ---------------------------------------------------------------------------
# Module-level compiled regex constants.
# ---------------------------------------------------------------------------


_PRICE_FIELDS = frozenset({"close", "high", "low", "open", "volume", "hl2", "ohlc4"})

_INDICATOR_NAMES = (
    "sma",
    "ema",
    "rsi",
    "macd_signal",
    "macd_histogram",
    "macd",
    "bollinger_upper",
    "bollinger_lower",
    "bollinger_middle",
    "bollinger",
    "atr",
    "adx",
    "stochastic_k",
    "stochastic_d",
    "stochastic",
    "vwap",
)

# Token: either a price field, a number (int or decimal), or
# ``name(arg1[,arg2,...])`` for the indicators above.  Longest indicator
# names come first so ``macd_signal`` matches before ``macd``.
_INDICATOR_NAME_PAT = "|".join(re.escape(name) for name in _INDICATOR_NAMES)
_TOKEN_PAT = (
    r"(?:"
    + _INDICATOR_NAME_PAT
    + r")\s*\([^)]*\)"
    + r"|(?:close|high|low|open|volume|hl2|ohlc4)\b"
    + r"|(?:vwap)\b"
    + r"|-?\d+(?:\.\d+)?"
    + r"|(?:rsi)\b"
)
_OP_PAT = r"(>=|<=|==|>|<|crosses?\s+above|crosses?\s+below)"

_PREDICATE_RE = re.compile(
    rf"^\s*({_TOKEN_PAT})\s*{_OP_PAT}\s*({_TOKEN_PAT})\s*$",
    re.IGNORECASE,
)

_SHORT_RE = re.compile(r"\bshort\b", re.IGNORECASE)

_EXIT_WHEN_RE = re.compile(r"^\s*exit\s+when\s+(.+)$", re.IGNORECASE)
_TIME_STOP_RE = re.compile(
    r"^\s*exit\s+after\s+(\d+)\s*(bars?|days?|periods?)\s*$",
    re.IGNORECASE,
)

_STOP_LOSS_PCT_RE = re.compile(
    r"\bstop[\s_-]?loss[:\s]+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_STOP_LOSS_DECIMAL_RE = re.compile(
    r"\bstop[\s_-]?loss[:\s]+(0?\.\d+)\b",
    re.IGNORECASE,
)

_TAKE_PROFIT_RE = re.compile(
    r"\b(?:take[\s_-]?profit|target)[:\s]+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

_FIXED_FRACTION_RE = re.compile(
    r"\b(?:risk|allocate)\s+(\d+(?:\.\d+)?)\s*%\s+per\s+trade\b",
    re.IGNORECASE,
)

_VOL_TARGET_RE = re.compile(
    r"\bvol(?:atility)?[\s_-]?target\s+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

_FIXED_NOTIONAL_RE = re.compile(
    r"\$(\d+(?:\.\d+)?)\s+(?:per\s+trade|notional)",
    re.IGNORECASE,
)


# Per-indicator argument parsers.  Each entry returns the constructed
# IndicatorRef instance (or raises ValidationError, which the caller
# catches to emit UnparsableRule).
def _parse_int_args(raw: str, names: tuple[str, ...]) -> dict:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return {name: int(part) for name, part in zip(names, parts)}


def _parse_indicator_call(name: str, raw_args: str):
    if name == "sma":
        kwargs = _parse_int_args(raw_args, ("period",)) if raw_args.strip() else {}
        return SMARef(**kwargs) if kwargs else None
    if name == "ema":
        kwargs = _parse_int_args(raw_args, ("period",)) if raw_args.strip() else {}
        return EMARef(**kwargs) if kwargs else None
    if name == "rsi":
        kwargs = _parse_int_args(raw_args, ("period",)) if raw_args.strip() else {}
        return RSIRef(**kwargs)
    if name == "atr":
        kwargs = _parse_int_args(raw_args, ("period",)) if raw_args.strip() else {}
        return ATRRef(**kwargs)
    if name == "adx":
        kwargs = _parse_int_args(raw_args, ("period",)) if raw_args.strip() else {}
        return ADXRef(**kwargs)
    if name in ("macd", "macd_signal", "macd_histogram"):
        output = name.split("_", 1)[1] if "_" in name else "macd"
        kwargs = _parse_int_args(raw_args, ("fast", "slow", "signal")) if raw_args.strip() else {}
        return MACDRef(output=output, **kwargs)  # type: ignore[arg-type]
    if name in ("bollinger", "bollinger_upper", "bollinger_lower", "bollinger_middle"):
        band = name.split("_", 1)[1] if "_" in name else "middle"
        parts = [p.strip() for p in raw_args.split(",") if p.strip()]
        kwargs: dict = {}
        if parts:
            kwargs["period"] = int(parts[0])
        if len(parts) >= 2:
            kwargs["num_std"] = float(parts[1])
        return BollingerRef(band=band, **kwargs)  # type: ignore[arg-type]
    if name in ("stochastic", "stochastic_k", "stochastic_d"):
        output = name.split("_", 1)[1] if "_" in name else "k"
        kwargs = _parse_int_args(raw_args, ("k_period", "d_period")) if raw_args.strip() else {}
        return StochasticRef(output=output, **kwargs)  # type: ignore[arg-type]
    if name == "vwap":
        return VWAPRef()
    return None


_INDICATOR_CALL_RE = re.compile(
    rf"^\s*({_INDICATOR_NAME_PAT})\s*\(([^)]*)\)\s*$",
    re.IGNORECASE,
)
_BARE_NAME_RE = re.compile(
    rf"^\s*({_INDICATOR_NAME_PAT}|vwap)\s*$",
    re.IGNORECASE,
)


def _parse_token(tok: str):
    """Return an IndicatorRef-compatible node, or ``None`` if unparseable."""
    tok = tok.strip()
    if not tok:
        return None
    low = tok.lower()
    if low in _PRICE_FIELDS:
        return PriceRef(field=low)  # type: ignore[arg-type]
    # bare number
    try:
        return ConstRef(value=float(tok))
    except ValueError:
        pass
    m = _INDICATOR_CALL_RE.match(tok)
    if m:
        try:
            return _parse_indicator_call(m.group(1).lower(), m.group(2))
        except (ValueError, ValidationError):
            return None
    m = _BARE_NAME_RE.match(tok)
    if m:
        try:
            return _parse_indicator_call(m.group(1).lower(), "")
        except (ValueError, ValidationError):
            return None
    return None


_OP_MAP = {
    ">": "gt",
    "<": "lt",
    ">=": "ge",
    "<=": "le",
    "==": "eq",
}


def _normalise_op(op_text: str) -> str | None:
    op_text = op_text.strip().lower()
    if op_text in _OP_MAP:
        return _OP_MAP[op_text]
    # ``crosses above`` / ``cross above`` (and below)
    if re.match(r"crosses?\s+above", op_text):
        return "cross_above"
    if re.match(r"crosses?\s+below", op_text):
        return "cross_below"
    return None


def _parse_predicate(prose: str) -> Predicate | None:
    m = _PREDICATE_RE.match(prose)
    if not m:
        return None
    lhs = _parse_token(m.group(1))
    op = _normalise_op(m.group(2))
    rhs = _parse_token(m.group(3))
    if lhs is None or op is None or rhs is None:
        return None
    try:
        return Predicate(lhs=lhs, op=op, rhs=rhs)  # type: ignore[arg-type]
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def parse_entry_rule(prose: str) -> EntryRule | UnparsableRule:
    """Parse a single entry-rule prose string."""
    text = prose.strip()
    side: Literal["long", "short"] = "short" if _SHORT_RE.search(text) else "long"
    # Strip a leading "long when " / "short when " marker if present so the
    # remaining text is a pure predicate.
    body = re.sub(r"^\s*(?:long|short)\s+when\s+", "", text, count=1, flags=re.IGNORECASE)
    predicate = _parse_predicate(body)
    if predicate is None:
        return UnparsableRule(prose=text, reason="no pattern matched")
    return EntryRule(side=side, when=predicate)


def parse_exit_rule(prose: str):
    """Parse a single exit-rule prose string; returns one of the ExitRule union members."""
    text = prose.strip()

    m = _TIME_STOP_RE.match(text)
    if m:
        try:
            return TimeStopRule(n_bars=int(m.group(1)))
        except ValidationError:
            return UnparsableRule(prose=text, reason="time_stop out of bounds")

    m = _EXIT_WHEN_RE.match(text)
    if m:
        predicate = _parse_predicate(m.group(1))
        if predicate is not None:
            return SignalExitRule(when=predicate)
        return UnparsableRule(prose=text, reason="exit predicate not recognised")

    m = _STOP_LOSS_PCT_RE.search(text)
    if m:
        try:
            return StopLossRule(pct=float(m.group(1)) / 100.0)
        except ValidationError:
            return UnparsableRule(prose=text, reason="stop_loss out of bounds")

    m = _STOP_LOSS_DECIMAL_RE.search(text)
    if m:
        try:
            return StopLossRule(pct=float(m.group(1)))
        except ValidationError:
            return UnparsableRule(prose=text, reason="stop_loss out of bounds")

    m = _TAKE_PROFIT_RE.search(text)
    if m:
        try:
            return TakeProfitRule(pct=float(m.group(1)) / 100.0)
        except ValidationError:
            return UnparsableRule(prose=text, reason="take_profit out of bounds")

    # Bare predicate in the exit slot is treated as a SignalExitRule.
    predicate = _parse_predicate(text)
    if predicate is not None:
        return SignalExitRule(when=predicate)

    return UnparsableRule(prose=text, reason="no pattern matched")


def parse_sizing_rule(prose: str):
    """Parse a single sizing-rule prose string; returns one of the SizingRule union members."""
    text = prose.strip()

    m = _FIXED_FRACTION_RE.search(text)
    if m:
        try:
            return FixedFractionSizing(fraction=float(m.group(1)) / 100.0)
        except ValidationError:
            return UnparsableSizing(prose=text, reason="fixed_fraction out of bounds")

    m = _VOL_TARGET_RE.search(text)
    if m:
        try:
            return VolatilityTargetSizing(target_annual_vol=float(m.group(1)) / 100.0)
        except ValidationError:
            return UnparsableSizing(prose=text, reason="vol_target out of bounds")

    m = _FIXED_NOTIONAL_RE.search(text)
    if m:
        try:
            return FixedNotionalSizing(notional_usd=float(m.group(1)))
        except ValidationError:
            return UnparsableSizing(prose=text, reason="fixed_notional out of bounds")

    return UnparsableSizing(prose=text, reason="no pattern matched")


def parse_rule_list(prose_list: list[str], kind: Literal["entry", "exit"]) -> list:
    """Map a legacy list[str] of rules to a list of structured rules."""
    parser = parse_entry_rule if kind == "entry" else parse_exit_rule
    return [parser(p) for p in prose_list]


def parse_sizing_list(prose_list: list[str]):
    """Collapse a legacy list[str] of sizing rules to a single structured rule.

    Multiple entries: first parsable wins; remaining entries are concatenated
    into the chosen variant's ``note`` field.  All-unparsable returns
    ``UnparsableSizing`` covering every input joined by ``"; "``.
    Empty list returns ``UnparsableSizing(prose="", reason="empty")``.
    """
    if not prose_list:
        return UnparsableSizing(prose="", reason="empty")
    if len(prose_list) == 1:
        return parse_sizing_rule(prose_list[0])

    chosen = None
    leftovers: list[str] = []
    for entry in prose_list:
        parsed = parse_sizing_rule(entry)
        if chosen is None and not isinstance(parsed, UnparsableSizing):
            chosen = parsed
        else:
            leftovers.append(entry)

    if chosen is None:
        return UnparsableSizing(
            prose="; ".join(p.strip() for p in prose_list),
            reason="no pattern matched",
        )

    note = "; ".join(p.strip() for p in leftovers)
    return chosen.model_copy(update={"note": note})
