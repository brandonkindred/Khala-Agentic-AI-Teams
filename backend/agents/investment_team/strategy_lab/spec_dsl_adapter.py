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

# Token: either a price field, a number (int / decimal / leading-dot decimal /
# scientific), or ``name(arg1[,arg2,...])`` / bare ``name`` for the indicators
# above.  Longest names come first so ``macd_signal`` matches before ``macd``.
# Each underscore in the indicator name is allowed to appear as ``-`` or ``_``
# so hyphenated aliases (``macd-signal``) parse identically to the
# underscored form.
_INDICATOR_NAME_PAT = "|".join(name.replace("_", "[-_]") for name in _INDICATOR_NAMES)
_NUMBER_PAT = r"-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
_TOKEN_PAT = (
    r"(?:" + _INDICATOR_NAME_PAT + r")\s*\([^)]*\)"
    r"|(?:close|high|low|open|volume|hl2|ohlc4)\b"
    r"|(?:" + _INDICATOR_NAME_PAT + r")\b"
    r"|" + _NUMBER_PAT
)
# ``cross(?:es)?`` matches singular `cross` and plural `crosses` (the previous
# ``crosses?`` matched only `crosse`/`crosses` — never bare `cross`).
_OP_PAT = r"(>=|<=|==|>|<|cross(?:es)?\s+above|cross(?:es)?\s+below)"

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

# Positive numeric pattern (no leading sign).  Includes scientific notation
# so tiny fractions like ``1e-12`` round-trip through the formatter; Pydantic
# `gt=0` constraints reject negative parses, so we don't need to forbid the
# minus sign here.
_POS_NUMBER_PAT = r"(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"

_STOP_LOSS_PCT_RE = re.compile(
    r"\b(?:(trailing[-\s_]?(?:high|low))\s+)?stop[\s_-]?loss[:\s]+(" + _POS_NUMBER_PAT + r")\s*%",
    re.IGNORECASE,
)
_STOP_LOSS_DECIMAL_RE = re.compile(
    r"\b(?:(trailing[-\s_]?(?:high|low))\s+)?stop[\s_-]?loss[:\s]+(" + _POS_NUMBER_PAT + r")",
    re.IGNORECASE,
)

_TAKE_PROFIT_RE = re.compile(
    r"\b(?:take[\s_-]?profit|target)[:\s]+(" + _POS_NUMBER_PAT + r")\s*%",
    re.IGNORECASE,
)

_FIXED_FRACTION_RE = re.compile(
    r"\b(?:risk|allocate)\s+(" + _POS_NUMBER_PAT + r")\s*%\s+per\s+trade\b",
    re.IGNORECASE,
)

_VOL_TARGET_RE = re.compile(
    r"\bvol(?:atility)?[\s_-]?target\s+(" + _POS_NUMBER_PAT + r")\s*%",
    re.IGNORECASE,
)

_FIXED_NOTIONAL_RE = re.compile(
    r"\$(" + _POS_NUMBER_PAT + r")\s+(?:per\s+trade|notional)",
    re.IGNORECASE,
)


# Per-indicator argument parser.  ``_parse_call_args`` splits ``raw`` into
# positional values and a single optional ``source=X`` kwarg, rejecting:
#   - extra positional args beyond what the indicator accepts,
#   - any kwarg other than ``source`` (or any ``source=`` when the indicator
#     has no ``source`` field),
#   - positional args appearing after a kwarg,
#   - non-numeric positional tokens.
# Returning ``None`` makes the caller emit ``UnparsableRule``.
def _parse_call_args(
    raw: str,
    int_slots: int,
    float_slots: int = 0,
    allow_source: bool = True,
) -> tuple[list[int | float], str | None] | None:
    if not raw.strip():
        return [], None
    # Don't filter empty slots: ``sma(,20)`` / ``macd(12,,9)`` would silently
    # shift positional values to the wrong parameters.  Any empty slot is a
    # malformed call.
    parts = [p.strip() for p in raw.split(",")]
    if any(not p for p in parts):
        return None
    positional_tokens: list[str] = []
    source: str | None = None
    saw_kwarg = False
    for part in parts:
        if "=" in part:
            saw_kwarg = True
            k, v = part.split("=", 1)
            k = k.strip().lower()
            v = v.strip().lower()  # `SOURCE=OPEN` → `source=open` so the Literal accepts it
            if k != "source" or not allow_source or source is not None:
                return None
            source = v
        else:
            if saw_kwarg:
                return None
            positional_tokens.append(part)

    if len(positional_tokens) > int_slots + float_slots:
        return None

    parsed: list[int | float] = []
    for i, val in enumerate(positional_tokens):
        try:
            parsed.append(int(val) if i < int_slots else float(val))
        except ValueError:
            return None
    return parsed, source


def _parse_indicator_call(name: str, raw_args: str):
    def _single_period(cls, require_positional: bool):
        res = _parse_call_args(raw_args, int_slots=1, allow_source=True)
        if res is None:
            return None
        pos, source = res
        if require_positional and not pos:
            return None
        kwargs: dict = {}
        if pos:
            kwargs["period"] = pos[0]
        if source is not None:
            kwargs["source"] = source
        return cls(**kwargs)

    if name == "sma":
        return _single_period(SMARef, require_positional=True)
    if name == "ema":
        return _single_period(EMARef, require_positional=True)
    if name == "rsi":
        return _single_period(RSIRef, require_positional=False)
    if name == "atr":
        res = _parse_call_args(raw_args, int_slots=1, allow_source=False)
        if res is None:
            return None
        pos, _ = res
        return ATRRef(**({"period": pos[0]} if pos else {}))
    if name == "adx":
        res = _parse_call_args(raw_args, int_slots=1, allow_source=False)
        if res is None:
            return None
        pos, _ = res
        return ADXRef(**({"period": pos[0]} if pos else {}))
    if name in ("macd", "macd_signal", "macd_histogram"):
        output = name.split("_", 1)[1] if "_" in name else "macd"
        res = _parse_call_args(raw_args, int_slots=3, allow_source=True)
        if res is None:
            return None
        pos, source = res
        kwargs = dict(zip(("fast", "slow", "signal"), pos))
        if source is not None:
            kwargs["source"] = source
        return MACDRef(output=output, **kwargs)  # type: ignore[arg-type]
    if name in ("bollinger", "bollinger_upper", "bollinger_lower", "bollinger_middle"):
        band = name.split("_", 1)[1] if "_" in name else "middle"
        res = _parse_call_args(raw_args, int_slots=1, float_slots=1, allow_source=True)
        if res is None:
            return None
        pos, source = res
        kwargs: dict = {}
        if pos:
            kwargs["period"] = pos[0]
        if len(pos) >= 2:
            kwargs["num_std"] = pos[1]
        if source is not None:
            kwargs["source"] = source
        return BollingerRef(band=band, **kwargs)  # type: ignore[arg-type]
    if name in ("stochastic", "stochastic_k", "stochastic_d"):
        output = name.split("_", 1)[1] if "_" in name else "k"
        res = _parse_call_args(raw_args, int_slots=2, allow_source=False)
        if res is None:
            return None
        pos, _ = res
        kwargs = dict(zip(("k_period", "d_period"), pos))
        return StochasticRef(output=output, **kwargs)  # type: ignore[arg-type]
    if name == "vwap":
        res = _parse_call_args(raw_args, int_slots=0, allow_source=False)
        if res is None:
            return None
        return VWAPRef()
    return None


_INDICATOR_CALL_RE = re.compile(
    rf"^\s*({_INDICATOR_NAME_PAT})\s*\(([^)]*)\)\s*$",
    re.IGNORECASE,
)
_BARE_NAME_RE = re.compile(
    rf"^\s*({_INDICATOR_NAME_PAT})\s*$",
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
            return _parse_indicator_call(_canonical_name(m.group(1)), m.group(2))
        except (ValueError, ValidationError):
            return None
    m = _BARE_NAME_RE.match(tok)
    if m:
        try:
            return _parse_indicator_call(_canonical_name(m.group(1)), "")
        except (ValueError, ValidationError):
            return None
    return None


def _canonical_name(raw: str) -> str:
    """Lowercase and convert hyphenated indicator aliases to the underscored form."""
    return raw.lower().replace("-", "_")


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
    # ``crosses above`` / ``cross above`` (and below) — note ``crosses?`` only
    # matches ``crosse``/``crosses``, so we use ``cross(?:es)?`` for both forms.
    if re.match(r"cross(?:es)?\s+above", op_text):
        return "cross_above"
    if re.match(r"cross(?:es)?\s+below", op_text):
        return "cross_below"
    return None


def _basis_from_trail(trail: str | None) -> str:
    """Map a captured ``trailing-high`` / ``trailing-low`` prefix to a StopLossRule basis."""
    if trail is None:
        return "entry_price"
    return "trailing_high" if "high" in trail.lower() else "trailing_low"


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
    """Parse a single entry-rule prose string.

    Accepts a bare predicate (``"close > sma(20)"``), the formatter's own
    output (``"long when …"`` / ``"short when …"``), or the legacy repo
    convention (``"enter when …"``, optionally ``"enter long when …"`` /
    ``"enter short when …"``).
    """
    text = prose.strip()
    side: Literal["long", "short"] = "short" if _SHORT_RE.search(text) else "long"
    body = re.sub(
        r"^\s*(?:enter\s+)?(?:(?:long|short)\s+)?when\s+",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
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
            return StopLossRule(pct=float(m.group(2)) / 100.0, basis=_basis_from_trail(m.group(1)))
        except ValidationError:
            return UnparsableRule(prose=text, reason="stop_loss out of bounds")

    m = _STOP_LOSS_DECIMAL_RE.search(text)
    if m:
        try:
            return StopLossRule(pct=float(m.group(2)), basis=_basis_from_trail(m.group(1)))
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
