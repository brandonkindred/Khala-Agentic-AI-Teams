"""Engine-side look-ahead guard (Phase 2).

The subprocess harness already makes structural look-ahead impossible from
inside a strategy — there is simply no accessor for future bars in the
strategy process (see ``trading_service/strategy/contract.py``).  The
assertion in this module is the parent-side counterpart: it defends the
``signal-on-t / fill-on-t+1`` contract inside the ``FillSimulator`` so any
future engine refactor that accidentally fills an order against the same
bar on which it was submitted surfaces as a hard error instead of silently
producing inflated backtest results.

``BarSafetyAssertion`` is intentionally tiny: a timestamp comparison gated
on an ``enabled`` flag.  The ``TradingService`` catches ``LookAheadError``
and flips ``TradingServiceResult.lookahead_violation`` so the same
classification that the subprocess harness emits also applies when the
violation originates in the fill simulator.
"""

from __future__ import annotations


class LookAheadError(RuntimeError):
    """Raised when the fill simulator would fill an order against a bar the
    strategy saw at (or before) order-submission time.

    Carries the offending ``order_id`` and the two timestamps so failure
    messages in logs/tests point directly at the responsible bar.
    """

    def __init__(self, *, order_id: str, submitted_at: str, fill_bar_timestamp: str) -> None:
        super().__init__(
            f"order {order_id!r} submitted at {submitted_at!r} would fill against "
            f"bar at {fill_bar_timestamp!r}; fill must be strictly after submission."
        )
        self.order_id = order_id
        self.submitted_at = submitted_at
        self.fill_bar_timestamp = fill_bar_timestamp


class BarSafetyAssertion:
    """Stateless guard that enforces strict ``fill_bar > submitted_at``.

    ISO-8601 timestamps compare correctly as plain strings for both daily
    (``YYYY-MM-DD``) and intraday (``YYYY-MM-DDTHH:MM:SS``) formats, so the
    check is a single lexicographic comparison.  When ``enabled=False`` the
    assertion becomes a no-op — used only by tests that deliberately
    construct pathological traces to verify the assertion fires when enabled.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def check_fill(
        self,
        *,
        order_id: str,
        submitted_at: str,
        fill_bar_timestamp: str,
    ) -> None:
        if not self.enabled:
            return
        if not submitted_at or not fill_bar_timestamp:
            # Missing metadata would make the comparison meaningless; skip
            # rather than false-positive on records predating this field.
            return
        if fill_bar_timestamp <= submitted_at:
            raise LookAheadError(
                order_id=order_id,
                submitted_at=submitted_at,
                fill_bar_timestamp=fill_bar_timestamp,
            )


__all__ = ["BarSafetyAssertion", "LookAheadError"]
