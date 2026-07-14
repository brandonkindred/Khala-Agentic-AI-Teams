"""Canonical, parameterized method-body TEXT for the indicators whose
emitted template was hand-duplicated between ``factors/compiler.py`` and
``synthesis/compiler.py`` (MACD's streaming cache, ADX's directional-
movement loop).

Two placeholder families, resolved in two passes — see each ``render_*``
function's docstring for the exact contract. ``%BARS%``/``%MISSING%``/
``%CACHE_KEY%``/``%SELECT%``/``%CLOSE(expr)%`` are resolved once per
compiler (text substitution); ``{fast}``/``{slow}``/``{signal}``/
``{period}`` are left as ``str.format()`` placeholders for the caller.
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
# Close normalisation mirrors indicators/streaming.py::_normalise_close.
# Detects third-party bool scalars by top-level module + exact-name
# allowlist (covers numpy.ma submodules, pyarrow, polars); guards
# ``__module__`` against None (type()-built classes); catches OverflowError
# for astronomical-magnitude ints. Shared by the current-bar and prev-bar
# reads below so the two legs cannot drift from each other.
def _norm_close(_bar):
    try:
        _raw = getattr(_bar, 'close', None)
    except NotImplementedError:
        raise
    except _safe_exc:
        return None
    if _raw is None or isinstance(_raw, bool):
        return None
    _cls = type(_raw)
    _mod = getattr(_cls, '__module__', None)
    _nm = getattr(_cls, '__name__', '')
    if (
        isinstance(_mod, str)
        and isinstance(_nm, str)
        and _mod.split('.', 1)[0] in ('numpy', 'pandas', 'pyarrow', 'polars')
        and _nm.lower() in ('bool', 'bool_', 'boolean', 'booleanscalar', 'boolscalar', 'bool8')
    ):
        return None
    try:
        _val = float(_raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(_val) or math.isinf(_val):
        return None
    return _val
try:
    _symbol = getattr(%BARS%[-1], 'symbol', None)
except NotImplementedError:
    raise
except _safe_exc:
    _symbol = None
_macd_key = %CACHE_KEY%
_state = self._ind_state.get(_macd_key)
_last = %BARS%[-1]
_new_close = _norm_close(_last)
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
        _prev_close = _norm_close(_prev)
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

    Preconditions: ``bars_var``/``missing`` match the caller's own naming
    convention (factors: ``"bars"``/``"NAN"``; synthesis: ``"history"``/
    ``"None"``, source-aware — synthesis passes ``close_expr_template=
    "self._src({obj}, source)"`` where factors passes ``"{obj}.close"``).
    ``close_expr_template`` has exactly one ``{obj}`` placeholder;
    ``cache_key_expr`` and ``select_expr`` are complete Python expression
    text (may still contain literal ``{fast}``/``{slow}``/``{signal}`` for
    the caller's own later ``.format()`` pass — factors defers that to
    per-node compile time with int literals; synthesis does it once at
    import time with the *string* ``"fast"``/etc., turning each placeholder
    into its own bound parameter name).
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
