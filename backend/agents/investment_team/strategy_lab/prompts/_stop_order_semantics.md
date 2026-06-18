# Stop-order semantics — reference (read before reasoning about exits)

These are the three stop order types and the exact behavior the engine implements.
Attribute observed exit behavior to these mechanics; do **not** describe correct,
by-design behavior as a defect, bug, or misconfiguration.

- **Stop (stop-market).** A trigger price that, once touched, submits a *market*
  order. It guarantees execution, not price: on a gap-through it fills past the
  trigger (e.g. a long stop at 95 fills at the 80 open if the bar gaps down).

- **Stop-limit.** A trigger price that submits a *limit* order at a separate limit
  price. It guarantees price-or-better, **not execution**: if the bar gaps through
  the limit, the order goes unfilled and **the position stays open**. A
  triggered-but-unfilled stop-limit is the defining, intended risk of this order
  type — never describe a gap-through non-fill as a malfunction.

- **Trailing stop.** A stop whose trigger ratchets in the *favorable* direction as
  price moves favorably — it rises as a long appreciates, falls as a short
  appreciates — and never loosens back. A trailing stop's trigger moving **above
  the entry price** as a long position appreciates is the **correct, intended,
  gain-locking behavior** — it is NOT a defect, a bug, or evidence of
  misconfiguration. The same applies symmetrically to a short's trigger falling
  below entry. The point of a trailing stop is exactly to lift the protective
  level above entry once the trade is in profit, converting an open gain into a
  protected one.

The engine implements all three with these exact semantics, and they are covered
by deterministic golden tests, so the execution is correct by construction. When
analyzing or designing a strategy, reason from these mechanics rather than
flagging them.
