"""Golden fixtures and regression tests for the trade simulator.

Locks current metric/trade-ledger behavior so the Phase 1-5 refactors of
``trade_simulator.py``/``execution/*`` can be detected regressions rather than
silent drift. Synthetic OHLCV is deterministic (fixed seed) and exercises two
regimes — a sinusoidal mean-reversion regime and a trend regime with jumps.
"""
