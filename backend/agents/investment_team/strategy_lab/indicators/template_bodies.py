"""Canonical, parameterized method-body TEXT for indicators whose emitted
template was hand-duplicated between ``factors/compiler.py`` and
``synthesis/compiler.py`` (MACD's ~200-line streaming cache, ADX's
directional-movement loop).

Host-side only. The returned bodies are Python SOURCE TEXT destined to be
inlined into a *compiled strategy module* that runs inside the sandbox — but
this module itself is never flattened into the sandbox and is never imported
by the emitted code; it only generates the text at compile time. That
distinction is what lets both DSL compilers share one string source despite
the sandbox's import whitelist (``quality_gates.code_safety.ALLOWED_IMPORTS``)
forbidding the emitted code from importing a shared host module directly (see
``factors/compiler.py``'s module docstring).

Two placeholder families, resolved in two separate passes:

* ``%BARS%`` / ``%MISSING%`` / ``%CACHE_KEY%`` / ``%SELECT%`` /
  ``%CLOSE(expr)%`` — resolved ONCE per compiler, via plain text
  substitution, picking that compiler's own naming convention (``bars``/
  ``NAN`` for factors, ``history``/``None`` for synthesis) and read style
  (``factors`` is not source-aware — MACD always reads ``.close``;
  ``synthesis`` is source-aware — MACD reads ``self._src(bar, source)``).
* ``{fast}`` / ``{slow}`` / ``{signal}`` / ``{period}`` — left as
  ``str.format()`` placeholders. ``factors/compiler.py`` fills these later,
  once per compiled node, with int literals (its existing
  ``_format_primitive`` call is unchanged). ``synthesis/compiler.py`` fills
  them ONCE at import time with the *string* ``"fast"``/``"period"`` (e.g.
  ``.format(period="period")``), which turns ``{period}`` into the literal
  text ``period`` so the emitted method reads its own function argument
  instead of a baked-in constant.

Both compilers standardise on the underscore-prefixed local-variable
convention below for the shared portions (regardless of which compiler
emits them) — no test asserts on exact variable names inside a compiled
module (only on its behavior), so this is a safe, one-time naming
normalisation rather than a second naming convention to maintain.
"""

from __future__ import annotations

import re

_CLOSE_MACRO_RE = re.compile(r"%CLOSE\(([^()]*)\)%")


def _resolve_close_reads(text: str, close_expr_template: str) -> str:
    """Replace every ``%CLOSE(expr)%`` with ``close_expr_template`` applied to ``expr``.

    Preconditions: ``close_expr_template`` contains exactly one ``{obj}``
    placeholder (e.g. ``"{obj}.close"`` or ``"self._src({obj}, source)"``).
    Postconditions: every ``%CLOSE(...)%`` occurrence is replaced; any
    ``{fast}``/``{slow}``/``{signal}`` text nested inside the captured
    ``expr`` survives untouched for the caller's later ``.format()`` pass.
    """
    return _CLOSE_MACRO_RE.sub(lambda m: close_expr_template.format(obj=m.group(1)), text)


# ---------------------------------------------------------------------------
# MACD — streaming-cache classification (expand/slide/cold-rebuild), close
# normalisation, and iterator-based signal-EMA walk. Computes all three
# outputs (macd/signal/histogram) unconditionally and dispatches on
# ``%SELECT%`` — factors' MACDSignal node always selects ``'signal'`` (a
# literal), synthesis's ``macd`` helper selects via its runtime ``select``
# argument. The extra histogram/macd_val bookkeeping factors' compiled output
# didn't previously carry is a single subtraction; the signal-line numeric
# result (the only one factors ever returns) is unchanged.
# ---------------------------------------------------------------------------

_MACD_BODY = """\
# Precondition defense-in-depth: matches IndicatorRegistry._macd_value,
# but returns %MISSING% (the sandbox can't propagate ValueError cleanly).
# ``not (x >= y)`` rather than ``x < y`` so NaN parameters trip the gate
# (NaN is unordered with everything; ``NaN < 2`` is False under IEEE 754).
if not ({fast} >= 2) or not ({slow} > {fast}) or not ({signal} >= 2):
    return %MISSING%
# True warm-up: need at least ``slow`` bars to seed the first macd_line
# entry. The signal-EMA fills once ``len(macd_line) >= signal``, i.e. at
# ``len(bars) >= slow + signal - 1``.
if len(%BARS%) < {slow}:
    return %MISSING%
# Streaming-cache the macd_line, keyed (in part) on the bar's symbol so a
# strategy whose on_bar fires across multiple tickers never mutates one
# symbol's macd_line with another's bars. Classify each call as ``expand``
# (history grew by one bar), ``slide`` (history length unchanged, oldest bar
# dropped), or cold-rebuild — the first two are O(fast + slow); the third
# rebuilds the whole macd_line from scratch.
#
# Bar-attribute reads route through a narrow try/except so a raising
# @property/computed_field on close/timestamp/symbol degrades to None
# instead of crashing the helper (mirrors indicators/streaming.py's
# ``_safe_getattr`` — only descriptor/resolution errors are caught;
# programmer/runtime sentinels propagate).
_safe_exc = (AttributeError, TypeError, ValueError, RuntimeError, LookupError)
try:
    _symbol = getattr(%BARS%[-1], 'symbol', None)
except NotImplementedError:
    raise
except _safe_exc:
    _symbol = None
_macd_key = %CACHE_KEY%
_state = self._ind_state.get(_macd_key)
_last = %BARS%[-1]
try:
    _raw_close = getattr(_last, 'close', None)
except NotImplementedError:
    raise
except _safe_exc:
    _raw_close = None
# Close normalisation mirrors indicators/streaming.py::_normalise_close.
# Detects third-party bool scalars by top-level module + exact-name
# allowlist (covers numpy.ma submodules, pyarrow, polars); guards
# ``__module__`` against None (type()-built classes); catches OverflowError
# for astronomical-magnitude ints.
if _raw_close is None or isinstance(_raw_close, bool):
    _new_close = None
else:
    _cls = type(_raw_close)
    _mod = getattr(_cls, '__module__', None)
    _nm = getattr(_cls, '__name__', '')
    if (
        isinstance(_mod, str)
        and isinstance(_nm, str)
        and _mod.split('.', 1)[0] in ('numpy', 'pandas', 'pyarrow', 'polars')
        and _nm.lower() in ('bool', 'bool_', 'boolean', 'booleanscalar', 'boolscalar', 'bool8')
    ):
        _new_close = None
    else:
        try:
            _new_close = float(_raw_close)
        except (TypeError, ValueError, OverflowError):
            _new_close = None
        else:
            if math.isnan(_new_close) or math.isinf(_new_close):
                _new_close = None
try:
    _new_ts = getattr(_last, 'timestamp', None)
except NotImplementedError:
    raise
except _safe_exc:
    _new_ts = None
_new_fp = (id(_last), len(%BARS%), _new_ts, _new_close)
if _state is not None and _state['fp'] == _new_fp:
    return _state['value'].get(%SELECT%)
_kind = 'none'
if _state is not None and len(%BARS%) >= 2:
    _prev = %BARS%[-2]
    try:
        _prev_ts = getattr(_prev, 'timestamp', None)
    except NotImplementedError:
        raise
    except _safe_exc:
        _prev_ts = None
    _prev_fp = _state['fp']
    _both_have_ts = _prev_fp[2] is not None and _prev_ts is not None
    _both_ts_absent = _prev_fp[2] is None and _prev_ts is None
    if _prev_fp[0] == id(_prev):
        _prev_matches = True
    elif _both_have_ts and _prev_fp[2] == _prev_ts:
        _prev_matches = True
    elif _both_ts_absent:
        # Defer close compute to this leg; avoids a wasted float() in the
        # common id/ts-match path and prevents crashes on non-numeric prev
        # closes.
        try:
            _prev_raw_close = getattr(_prev, 'close', None)
        except NotImplementedError:
            raise
        except _safe_exc:
            _prev_raw_close = None
        if _prev_raw_close is None or isinstance(_prev_raw_close, bool):
            _prev_close = None
        else:
            _prev_cls = type(_prev_raw_close)
            _prev_mod = getattr(_prev_cls, '__module__', None)
            _prev_nm = getattr(_prev_cls, '__name__', '')
            if (
                isinstance(_prev_mod, str)
                and isinstance(_prev_nm, str)
                and _prev_mod.split('.', 1)[0] in ('numpy', 'pandas', 'pyarrow', 'polars')
                and _prev_nm.lower() in ('bool', 'bool_', 'boolean', 'booleanscalar', 'boolscalar', 'bool8')
            ):
                _prev_close = None
            else:
                try:
                    _prev_close = float(_prev_raw_close)
                except (TypeError, ValueError, OverflowError):
                    _prev_close = None
                else:
                    if math.isnan(_prev_close) or math.isinf(_prev_close):
                        _prev_close = None
        _prev_matches = _prev_close is not None and _prev_fp[3] == _prev_close
    else:
        _prev_matches = False
    if _prev_matches:
        if len(%BARS%) == _prev_fp[1] + 1:
            _kind = 'expand'
        elif len(%BARS%) == _prev_fp[1]:
            _kind = 'slide'
_alpha_f = 2.0 / ({fast} + 1)
_alpha_s = 2.0 / ({slow} + 1)
if _kind == 'expand' or _kind == 'slide':
    macd_line = _state['macd_line']
    # Compute-then-mutate: finish the EMA loops BEFORE touching the cached
    # deque, so any raise leaves the cache untouched.
    _ef = %CLOSE(%BARS%[-{fast}])%
    for _b in %BARS%[-{fast} + 1:]:
        _ef = _alpha_f * %CLOSE(_b)% + (1 - _alpha_f) * _ef
    _es = %CLOSE(%BARS%[-{slow}])%
    for _b in %BARS%[-{slow} + 1:]:
        _es = _alpha_s * %CLOSE(_b)% + (1 - _alpha_s) * _es
    _new_macd = _ef - _es
    if _kind == 'slide':
        macd_line.popleft()
    macd_line.append(_new_macd)
else:
    macd_line = deque()
    for _end in range({slow}, len(%BARS%) + 1):
        _sub = %BARS%[:_end]
        _ef = %CLOSE(_sub[-{fast}])%
        for _b in _sub[-{fast} + 1:]:
            _ef = _alpha_f * %CLOSE(_b)% + (1 - _alpha_f) * _ef
        _es = %CLOSE(_sub[-{slow}])%
        for _b in _sub[-{slow} + 1:]:
            _es = _alpha_s * %CLOSE(_b)% + (1 - _alpha_s) * _es
        macd_line.append(_ef - _es)
_macd_val = macd_line[-1]
_sig_val = %MISSING%
_hist_val = %MISSING%
if len(macd_line) >= {signal}:
    _alpha_g = 2.0 / ({signal} + 1)
    # Iterator walk avoids deque.__getitem__ O(min(i, n-i)) indexing cost —
    # random-access on a deque would make signal-EMA O(n^2).
    _it = iter(macd_line)
    _sig = next(_it)
    for _x in _it:
        _sig = _alpha_g * _x + (1 - _alpha_g) * _sig
    _sig_val = _sig
    _hist_val = _macd_val - _sig_val
self._ind_state[_macd_key] = {{
    'fp': _new_fp,
    'macd_line': macd_line,
    'value': {{'macd': _macd_val, 'signal': _sig_val, 'histogram': _hist_val}},
}}
if %SELECT% == 'macd':
    return _macd_val
if %SELECT% == 'signal':
    return _sig_val
if %SELECT% == 'histogram':
    return _hist_val
return %MISSING%
"""


def render_macd_body(
    *,
    bars_var: str,
    missing: str,
    cache_key_expr: str,
    select_expr: str,
    close_expr_template: str,
) -> str:
    """Render the canonical MACD helper body.

    Preconditions: ``close_expr_template`` has exactly one ``{obj}``
    placeholder; ``cache_key_expr`` and ``select_expr`` are complete Python
    expression text (may still contain literal ``{fast}``/``{slow}``/
    ``{signal}`` for the caller's own later ``.format()`` pass).
    Postconditions: returns left-aligned Python source computing all three
    MACD outputs and returning the one ``select_expr`` names; the ``{fast}``/
    ``{slow}``/``{signal}`` placeholders are NOT resolved here.
    """
    text = (
        _MACD_BODY.replace("%BARS%", bars_var)
        .replace("%MISSING%", missing)
        .replace("%CACHE_KEY%", cache_key_expr)
        .replace("%SELECT%", select_expr)
    )
    return _resolve_close_reads(text, close_expr_template)


# ---------------------------------------------------------------------------
# ADX — directional-movement / true-range accumulation.
# ---------------------------------------------------------------------------

_ADX_BODY = """\
if len(%BARS%) < 2 * {period} + 1:
    return %MISSING%
# Slice to the trailing window BEFORE the DM/TR loop — only the last
# {period} triples are ever read (via the ``[-{period}:]`` slices below), so
# scanning the full retrieved history first was pure waste: O(len(%BARS%))
# work for an O(period) result. The 2 * {period} + 1 margin (rather than the
# minimal {period} + 1) keeps this slice exactly as large as the warm-up
# gate above, so the change is a pure perf fix — byte-identical output.
_window = %BARS%[-(2 * {period} + 1):]
_plus = []
_minus = []
_trs = []
for _i in range(1, len(_window)):
    _up = _window[_i].high - _window[_i - 1].high
    _dn = _window[_i - 1].low - _window[_i].low
    _plus.append(_up if _up > _dn and _up > 0 else 0.0)
    _minus.append(_dn if _dn > _up and _dn > 0 else 0.0)
    _pc = _window[_i - 1].close
    _trs.append(max(
        _window[_i].high - _window[_i].low,
        abs(_window[_i].high - _pc),
        abs(_window[_i].low - _pc),
    ))
_tr_sum = sum(_trs[-{period}:])
if _tr_sum == 0:
    return 0.0
_plus_di = 100.0 * sum(_plus[-{period}:]) / _tr_sum
_minus_di = 100.0 * sum(_minus[-{period}:]) / _tr_sum
if _plus_di + _minus_di == 0:
    return 0.0
return 100.0 * abs(_plus_di - _minus_di) / (_plus_di + _minus_di)
"""


def render_adx_body(*, bars_var: str, missing: str) -> str:
    """Render the canonical ADX helper body (``{period}`` left as a placeholder)."""
    return _ADX_BODY.replace("%BARS%", bars_var).replace("%MISSING%", missing)


# ---------------------------------------------------------------------------
# VWAP — rolling window over the trailing ``period`` bars. Unified semantics:
# the factors DSL's VWAP has always been rolling; synthesis's was cumulative-
# over-all-history before the commit that added a ``period`` param to
# synthesis's VWAP (an explicit, intentional behavior change).
# ---------------------------------------------------------------------------

_VWAP_BODY = """\
if len(%BARS%) < {period}:
    return %MISSING%
_w = %BARS%[-{period}:]
_num = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in _w)
_den = sum(b.volume for b in _w)
if _den == 0:
    return sum(b.close for b in _w) / {period}
return _num / _den
"""


def render_vwap_body(*, bars_var: str, missing: str) -> str:
    """Render the canonical rolling-window VWAP helper body (``{period}`` left as a placeholder)."""
    return _VWAP_BODY.replace("%BARS%", bars_var).replace("%MISSING%", missing)
