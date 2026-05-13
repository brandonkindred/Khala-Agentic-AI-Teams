"""Unit tests for the strategy-protocol versioning seam (issue #391).

The harness now treats every strategy as v1: strategies that omit
``protocol_version`` are accepted (treated as v1), strategies that
declare ``protocol_version = 1`` are accepted, and any other declaration
is rejected at child startup with a clean error.

There is intentionally no v0 legacy mode — these tests assert the
single-version surface and the rejection path for future-version
declarations.
"""

from __future__ import annotations

import textwrap

import pytest

from investment_team.trading_service.strategy.streaming_harness import (
    StrategyRuntimeError,
    StreamingHarness,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_V1_EXPLICIT_CODE = textwrap.dedent('''\
    """v1 strategy: declares protocol_version=1 and exercises a v1 feature
    (bracket attachment + REQUEUE_NEXT_BAR unfilled policy) so we prove
    these are reachable without runtime gating."""
    from contract import (
        OrderSide,
        OrderType,
        Strategy,
        StopAttachment,
        UnfilledPolicy,
    )

    protocol_version = 1


    class V1Strategy(Strategy):
        def on_bar(self, ctx, bar):
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1.0,
                order_type=OrderType.MARKET,
                reason="v1-feature-mix",
                unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
                attached_stop_loss=StopAttachment(stop_price=bar.close * 0.95),
            )
''')


_V1_IMPLICIT_CODE = textwrap.dedent('''\
    """No protocol_version declared — must default to v1 and reach the
    same v1 feature surface as the explicit fixture."""
    from contract import (
        OrderSide,
        OrderType,
        Strategy,
        StopAttachment,
        UnfilledPolicy,
    )


    class V1ImplicitStrategy(Strategy):
        def on_bar(self, ctx, bar):
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1.0,
                order_type=OrderType.MARKET,
                reason="v1-implicit",
                unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
                attached_stop_loss=StopAttachment(stop_price=bar.close * 0.95),
            )
''')


def _wrong_pv_code(declaration: str) -> str:
    """Build a fixture that declares ``protocol_version = <declaration>``
    at module scope. ``declaration`` is the literal Python source for the
    value to bind (e.g. ``"2"``, ``'"1"'``, ``"None"``).
    """
    return textwrap.dedent(
        f"""\
        from contract import Strategy

        protocol_version = {declaration}


        class WrongVersionStrategy(Strategy):
            def on_bar(self, ctx, bar):
                pass
        """
    )


def _bar(ts: str = "2024-01-02", symbol: str = "AAA") -> dict:
    return {
        "symbol": symbol,
        "timestamp": ts,
        "timeframe": "1d",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000.0,
    }


def _state() -> dict:
    return {"capital": 100_000.0, "equity": 100_000.0, "positions": []}


# ---------------------------------------------------------------------------
# Happy path: explicit v1
# ---------------------------------------------------------------------------


def test_strategy_explicit_v1_round_trip() -> None:
    """A strategy declaring ``protocol_version = 1`` runs end-to-end,
    advertises the expanded v1 capability flags, and may use v1-only
    features (bracket attachment + REQUEUE_NEXT_BAR) without any
    runtime gate firing.
    """
    with StreamingHarness(_V1_EXPLICIT_CODE) as harness:
        resp = harness.send_start(config={"initial_capital": 100_000.0})

        # Protocol version is latched on first ready.
        assert resp.protocol_version == 1
        assert harness.protocol_version == 1

        # Chunked-bars capability is orthogonal but still advertised.
        assert harness.supports_chunked_bars is True

        # v1 capability flags enumerated by _CAPABILITIES.
        caps = resp.capabilities
        assert caps.get("partial_fills") is True
        assert caps.get("bracket") is True
        assert caps.get("trailing_stop") is True
        assert caps.get("ioc_fok") is True

        # v1 features reach submit_order without raising.
        bar_resp = harness.send_bar(bar=_bar(), state=_state(), is_warmup=False)
        assert len(bar_resp.orders) == 1
        order = bar_resp.orders[0]
        assert order["reason"] == "v1-feature-mix"
        assert order["unfilled_policy"] == "requeue_next_bar"
        assert order["attached_stop_loss"]["stop_price"] == pytest.approx(100.5 * 0.95)

        harness.send_end()


# ---------------------------------------------------------------------------
# Default path: missing declaration → treated as v1
# ---------------------------------------------------------------------------


def test_strategy_missing_protocol_version_defaults_to_v1() -> None:
    """A strategy that omits ``protocol_version`` entirely is treated as
    v1 — the harness reports protocol_version=1 in the ready record and
    v1 features remain reachable. This is the default branch that all
    existing test fixtures (no ``protocol_version`` declared) rely on.
    """
    with StreamingHarness(_V1_IMPLICIT_CODE) as harness:
        resp = harness.send_start(config={"initial_capital": 100_000.0})
        assert resp.protocol_version == 1
        assert harness.protocol_version == 1

        bar_resp = harness.send_bar(bar=_bar(), state=_state(), is_warmup=False)
        assert len(bar_resp.orders) == 1
        order = bar_resp.orders[0]
        assert order["reason"] == "v1-implicit"
        assert order["unfilled_policy"] == "requeue_next_bar"
        assert order["attached_stop_loss"]["stop_price"] == pytest.approx(100.5 * 0.95)

        harness.send_end()


# ---------------------------------------------------------------------------
# Rejection path: any other declaration hard-fails at startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declaration",
    [
        "0",  # v0 legacy declared — there's no v0 mode any more
        "2",  # future version a v2 strategy would declare
        '"1"',  # right number, wrong type
        "None",  # explicit absence is NOT the same as omission
        "1.0",  # float — must be int
        "True",  # bool (a subclass of int) — excluded explicitly
    ],
)
def test_strategy_wrong_protocol_version_rejected(declaration: str) -> None:
    """Any explicit ``protocol_version`` declaration other than the
    integer ``1`` must fail at child startup with a structured error
    that names the offending value. This is the forward-evolution
    guard: a v2 strategy lifted unchanged onto this harness must NOT
    silently run as v1.
    """
    code = _wrong_pv_code(declaration)
    with StreamingHarness(code) as harness:
        with pytest.raises(StrategyRuntimeError) as excinfo:
            harness.send_start(config={"initial_capital": 100_000.0})

    # The child blocks on the parent's first message and responds with a
    # ``protocol_error`` envelope; ``_exchange`` re-raises it.
    assert excinfo.value.etype == "protocol_error"
    assert "protocol_version" in str(excinfo.value)
    # The offending repr() is surfaced so debugging is unambiguous.
    expected_repr = repr(eval(declaration))  # noqa: S307 — test-only literal eval
    assert expected_repr in str(excinfo.value)
