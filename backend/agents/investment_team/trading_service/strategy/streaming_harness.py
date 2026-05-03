"""Subprocess harness for running Strategy-Lab-generated scripts.

Replaces the batch-style ``SandboxRunner`` (which read all CSVs, ran, then
parsed a final JSON) with a stream-driven protocol:

    parent → child (stdin, JSONL):
        {"kind": "start", "config": {...}}
        {"kind": "bar", "bar": {...}, "state": {...}, "is_warmup": false}
        {"kind": "fill", "fill": {...}, "state": {...}}
        {"kind": "end"}

    child → parent (stdout, JSONL):
        {"kind": "order", "payload": {...}}
        {"kind": "cancel", "payload": {...}}
        {"kind": "log", "level": "info", "message": "..."}
        {"kind": "ready"}           # sent after every parent message processed
        {"kind": "error", "etype": "lookahead_violation", "message": "..."}

Every parent message is answered with zero-or-more ``order``/``cancel``/``log``
records followed by exactly one ``ready`` (or one ``error``). This gives the
engine a simple synchronous handshake while still keeping the strategy free
to emit multiple orders per event.

Strategy code must import ``Strategy`` from ``contract`` (the harness copies
``contract.py`` into the isolated working directory) and define a subclass.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TOTAL_TIMEOUT_SEC = 600  # hard ceiling for a full session
DEFAULT_EVENT_TIMEOUT_SEC = 30  # per-event watchdog


class StrategyRuntimeError(RuntimeError):
    """Raised when the strategy subprocess misbehaves (crash, timeout, protocol)."""

    def __init__(self, message: str, *, etype: str = "runtime_error") -> None:
        super().__init__(message)
        self.etype = etype


@dataclass
class HarnessResponse:
    """One parent→child round-trip result.

    ``bar_indices`` is a parallel list to ``orders`` (and ``cancels``,
    ``logs``) populated when running the chunked protocol (issue #377).
    Each entry is the 0-based position within the chunk of the bar that
    generated the corresponding order, or ``None`` when running
    per-bar / start / end / fill round-trips. The trading service uses
    these indices to pin per-order ``submitted_at`` to the originating
    bar's timestamp, preserving ``BarSafetyAssertion`` semantics.
    """

    orders: List[Dict[str, Any]] = field(default_factory=list)
    cancels: List[Dict[str, Any]] = field(default_factory=list)
    logs: List[Dict[str, Any]] = field(default_factory=list)
    order_bar_indices: List[Optional[int]] = field(default_factory=list)
    cancel_bar_indices: List[Optional[int]] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)


class StreamingHarness:
    """Parent-side handle over a long-running strategy subprocess.

    Typical use::

        with StreamingHarness(strategy_code) as h:
            h.send_start(config={"initial_capital": 100_000.0})
            h.send_bar(bar_event_dict, state_dict)
            # …
            h.send_end()
    """

    def __init__(
        self,
        strategy_code: str,
        *,
        total_timeout_sec: int = DEFAULT_TOTAL_TIMEOUT_SEC,
        event_timeout_sec: int = DEFAULT_EVENT_TIMEOUT_SEC,
    ) -> None:
        self._strategy_code = strategy_code
        self._total_timeout = total_timeout_sec
        self._event_timeout = event_timeout_sec
        self._tmpdir: Optional[tempfile.TemporaryDirectory] = None
        self._proc: Optional[subprocess.Popen] = None
        self._started_at: float = 0.0
        # Filled from the first ``ready`` (issue #377). Empty dict means
        # the child predates capability negotiation — treat as per-bar
        # only (no ``chunked_bars``).
        self._capabilities: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "StreamingHarness":
        self._tmpdir = tempfile.TemporaryDirectory(prefix="stratlab_stream_")
        tmp = self._tmpdir.name

        # Copy the contract types into the subprocess' working dir so the
        # strategy can ``from contract import Strategy, OrderSide, ...``.
        here = os.path.dirname(__file__)
        shutil.copy2(os.path.join(here, "contract.py"), os.path.join(tmp, "contract.py"))

        # Copy indicators library for parity with existing code-gen output.
        indicators_src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "strategy_lab",
            "executor",
            "indicators.py",
        )
        if os.path.exists(indicators_src):
            shutil.copy2(indicators_src, os.path.join(tmp, "indicators.py"))

        with open(os.path.join(tmp, "strategy.py"), "w", encoding="utf-8") as f:
            f.write(self._strategy_code)
        with open(os.path.join(tmp, "_harness.py"), "w", encoding="utf-8") as f:
            f.write(_HARNESS_SCRIPT)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONUNBUFFERED": "1",
        }
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:
            env["VIRTUAL_ENV"] = venv

        self._proc = subprocess.Popen(
            [sys.executable, "_harness.py"],
            cwd=tmp,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self._started_at = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        finally:
            self._proc = None
            if self._tmpdir is not None:
                self._tmpdir.cleanup()
                self._tmpdir = None

    # ------------------------------------------------------------------
    # Public message API
    # ------------------------------------------------------------------

    def send_start(self, *, config: Dict[str, Any]) -> HarnessResponse:
        return self._exchange({"kind": "start", "config": config})

    def send_bar(
        self,
        *,
        bar: Dict[str, Any],
        state: Dict[str, Any],
        is_warmup: bool = False,
    ) -> HarnessResponse:
        return self._exchange({"kind": "bar", "bar": bar, "state": state, "is_warmup": is_warmup})

    def send_bars(self, *, bars: List[Dict[str, Any]]) -> HarnessResponse:
        """Send a chunk of bars in a single round-trip (issue #377).

        ``bars`` is a list of ``{"bar": {...}, "state": {...},
        "is_warmup": bool}`` dicts. Each emitted ``order``/``cancel``
        record carries a ``bar_index`` field that the parent uses to
        route the order back to the source bar's timestamp. The chunk
        terminates with a single ``ready`` ack.

        Caller is responsible for checking :attr:`supports_chunked_bars`
        and for falling back to :meth:`send_bar` per bar when the child
        did not advertise ``chunked_bars``. The trading service does
        this gating before opting in.
        """
        if not bars:
            return HarnessResponse()
        return self._exchange({"kind": "bars", "bars": bars})

    def send_fill(self, *, fill: Dict[str, Any], state: Dict[str, Any]) -> HarnessResponse:
        return self._exchange({"kind": "fill", "fill": fill, "state": state})

    def send_end(self) -> HarnessResponse:
        return self._exchange({"kind": "end"})

    @property
    def supports_chunked_bars(self) -> bool:
        """True iff the child advertised ``chunked_bars`` in its first
        ``ready``. False until ``send_start`` has returned.
        """
        return bool(self._capabilities.get("chunked_bars"))

    # ------------------------------------------------------------------
    # Internal: protocol round-trip
    # ------------------------------------------------------------------

    def _exchange(self, message: Dict[str, Any]) -> HarnessResponse:
        if self._proc is None:
            raise StrategyRuntimeError("harness not started", etype="runtime_error")
        if self._total_timeout and (time.monotonic() - self._started_at) > self._total_timeout:
            self._proc.kill()
            raise StrategyRuntimeError(
                f"session exceeded total timeout of {self._total_timeout}s",
                etype="timeout",
            )
        try:
            line = json.dumps(message) + "\n"
            self._proc.stdin.write(line)  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
        except BrokenPipeError as exc:
            stderr = _drain(self._proc.stderr)
            raise StrategyRuntimeError(
                f"strategy subprocess exited unexpectedly: {stderr[:500]}",
                etype="crash",
            ) from exc

        resp = HarnessResponse()
        deadline = time.monotonic() + self._event_timeout
        while True:
            if time.monotonic() > deadline:
                self._proc.kill()
                raise StrategyRuntimeError(
                    f"strategy did not ack within {self._event_timeout}s",
                    etype="event_timeout",
                )
            raw = self._proc.stdout.readline()  # type: ignore[union-attr]
            if not raw:
                # EOF — subprocess died.
                stderr = _drain(self._proc.stderr)
                raise StrategyRuntimeError(
                    f"strategy subprocess closed stdout unexpectedly: {stderr[:1000]}",
                    etype="crash",
                )
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._proc.kill()
                raise StrategyRuntimeError(
                    f"invalid JSON from strategy: {raw[:200]!r}",
                    etype="protocol_error",
                ) from exc

            kind = record.get("kind")
            if kind == "order":
                resp.orders.append(record.get("payload", {}))
                resp.order_bar_indices.append(record.get("bar_index"))
            elif kind == "cancel":
                resp.cancels.append(record.get("payload", {}))
                resp.cancel_bar_indices.append(record.get("bar_index"))
            elif kind == "log":
                resp.logs.append(record)
            elif kind == "ready":
                # Capability handshake (issue #377): the child advertises
                # ``chunked_bars`` in its first ready after start. Update
                # ``self._capabilities`` whenever a ready carries one so a
                # late-binding child can still negotiate cleanly. Empty
                # payloads from older builds remain treated as per-bar
                # only via ``supports_chunked_bars``.
                caps = record.get("capabilities")
                if isinstance(caps, dict):
                    self._capabilities = caps
                    resp.capabilities = caps
                return resp
            elif kind == "error":
                etype = record.get("etype", "runtime_error")
                raise StrategyRuntimeError(
                    record.get("message", "unknown strategy error"),
                    etype=etype,
                )
            else:
                self._proc.kill()
                raise StrategyRuntimeError(
                    f"unknown message kind from strategy: {kind!r}",
                    etype="protocol_error",
                )


def _drain(stream) -> str:
    if stream is None:
        return ""
    try:
        return stream.read() or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Child-side harness script. Kept as a string (rather than a separate .py file)
# so the parent process can ship exactly one file into the subprocess tmpdir
# and remain self-describing.
# ---------------------------------------------------------------------------

_HARNESS_SCRIPT = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Child-side strategy harness. Auto-written; do not edit."""
    import json
    import sys
    import traceback

    # contract.py and strategy.py are both copied into this directory by the
    # parent StreamingHarness before we launch. indicators.py is optional.
    sys.path.insert(0, ".")

    import contract  # type: ignore  # noqa: E402

    try:
        import strategy  # type: ignore  # noqa: E402
    except Exception:
        msg = "".join(traceback.format_exception(*sys.exc_info()))
        print(json.dumps({"kind": "error", "etype": "import_error", "message": msg}))
        sys.exit(1)


    def _emit(record):
        sys.stdout.write(json.dumps(record) + "\\n")
        sys.stdout.flush()


    # Harness-private bar_index state for the chunked protocol (PR #425
    # review defense). ``_chunk_state["i"]`` is the harness-managed
    # current bar index during a chunk dispatch and ``None`` outside.
    # ``_tagged_emit`` overwrites ``bar_index`` on every emitted
    # ``order``/``cancel`` so a strategy that mutates any context
    # attribute (or sets ``bar_index`` directly on the record dict
    # before ``_emit`` is called) can never forge an earlier bar_index
    # after observing later bars in the chunk.
    #
    # Threat model: this defends against *buggy* strategies that
    # accidentally write to ``ctx`` attributes. A determined
    # adversarial strategy can still defeat the defense via
    # ``import _harness; _harness._chunk_state["i"] = 0`` because
    # ``_chunk_state`` lives at module scope in the harness child
    # process and Python doesn't enforce hard isolation. Closing that
    # gap requires parent-side enforcement (e.g. ``bar_started``
    # markers between bars that the parent uses to derive bar_index
    # without trusting subprocess-emitted values) and is out of scope
    # for #377.
    _chunk_state = {"i": None}


    def _tagged_emit(record):
        kind = record.get("kind")
        if kind in ("order", "cancel"):
            idx = _chunk_state["i"]
            if idx is None:
                # Per-bar protocol or non-chunk emission: strip any
                # bar_index a malicious strategy may have set on the
                # record before _emit was called.
                record.pop("bar_index", None)
            else:
                record["bar_index"] = idx
        _emit(record)


    def _find_strategy_class():
        candidates = []
        for name in dir(strategy):
            obj = getattr(strategy, name)
            if isinstance(obj, type) and issubclass(obj, contract.Strategy) and obj is not contract.Strategy:
                candidates.append(obj)
        if not candidates:
            raise RuntimeError(
                "strategy module must define a subclass of contract.Strategy"
            )
        if len(candidates) > 1:
            raise RuntimeError(
                "strategy module defines multiple Strategy subclasses: "
                + ", ".join(c.__name__ for c in candidates)
            )
        return candidates[0]


    # Capability set advertised in the first ready (issue #377). The
    # parent uses this to decide whether it may invoke ``send_bars``
    # with chunked payloads. Older parents that don't read
    # ``capabilities`` simply ignore the field.
    _CAPABILITIES = {"chunked_bars": True}


    def main():
        try:
            cls = _find_strategy_class()
        except Exception as exc:
            _emit({"kind": "error", "etype": "contract_error", "message": str(exc)})
            sys.exit(1)

        instance = cls()
        # Use the bar_index-tagging emit so strategies can never forge
        # bar_index regardless of what they do with ``ctx`` attributes.
        ctx = contract.StrategyContext(emit=_tagged_emit)
        started = False

        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                _emit({"kind": "error", "etype": "protocol_error", "message": str(exc)})
                sys.exit(1)

            kind = msg.get("kind")
            try:
                if kind == "start":
                    instance.on_start(ctx)
                    started = True
                elif kind == "bar":
                    bar = contract.Bar(**msg["bar"])
                    state = msg.get("state") or {}
                    _apply_state(ctx, state, is_warmup=bool(msg.get("is_warmup", False)))
                    ctx._ingest_bar(bar)
                    instance.on_bar(ctx, bar)
                elif kind == "bars":
                    # Chunked-bar protocol (issue #377). Each entry has its
                    # own ``state``/``is_warmup`` so the strategy sees the
                    # parent-supplied state per bar; ``bar_index`` is set
                    # on the context around each dispatch so emitted
                    # orders/cancels are tagged for the originating bar.
                    #
                    # Override safety (PR review #425): a vectorised
                    # ``on_bars`` override would receive the whole chunk
                    # before the parent replays bars one-by-one, letting
                    # the strategy peek at later bars and emit an order
                    # tagged to an earlier ``bar_index``. ``_run_chunked``
                    # trusts that index for ``submitted_at``, so the
                    # override path could bypass look-ahead safety. Reject
                    # outright; vectorised authors should run with
                    # ``BAR_CHUNK_SIZE=1`` (per-bar dispatch) where bar
                    # safety is enforced by the per-bar message protocol.
                    if type(instance).on_bars is not contract.Strategy.on_bars:
                        _emit({
                            "kind": "error",
                            "etype": "contract_error",
                            "message": (
                                "Overriding Strategy.on_bars is not supported under "
                                "the chunked protocol: a vectorised override could "
                                "see future bars in the chunk and emit orders tagged "
                                "to earlier bars, bypassing look-ahead safety. "
                                "Implement on_bar instead, or run with "
                                "BAR_CHUNK_SIZE=1."
                            ),
                        })
                        sys.exit(1)
                    chunk = msg.get("bars") or []
                    try:
                        for i, item in enumerate(chunk):
                            bar = contract.Bar(**item["bar"])
                            state = item.get("state") or {}
                            _apply_state(
                                ctx, state, is_warmup=bool(item.get("is_warmup", False))
                            )
                            ctx._ingest_bar(bar)
                            # Harness-managed bar_index for ``_tagged_emit``;
                            # strategy code cannot reach ``_chunk_state``.
                            _chunk_state["i"] = i
                            instance.on_bar(ctx, bar)
                    finally:
                        _chunk_state["i"] = None
                elif kind == "fill":
                    fill = contract.Fill(**msg["fill"])
                    state = msg.get("state") or {}
                    _apply_state(ctx, state, is_warmup=ctx.is_warmup)
                    instance.on_fill(ctx, fill)
                elif kind == "end":
                    if started:
                        instance.on_end(ctx)
                    _emit({"kind": "ready", "capabilities": _CAPABILITIES})
                    return
                else:
                    _emit({"kind": "error", "etype": "protocol_error",
                           "message": f"unknown kind: {kind!r}"})
                    sys.exit(1)
            except AttributeError as exc:
                # Most likely a look-ahead attempt that hit a non-existent
                # attribute on Bar/StrategyContext.
                tb = "".join(traceback.format_exception(*sys.exc_info()))
                _emit({"kind": "error", "etype": "lookahead_violation",
                       "message": f"{exc!s}\\n{tb}"})
                sys.exit(1)
            except contract.UnsupportedOrderFeatureError as exc:
                # Runtime-support gates from OrderRequest.validate_prices
                # ("feature ships in a later step of #379") raise this
                # specific subclass of NotImplementedError; surface them as a
                # structured ``unsupported_feature`` failure so the parent's
                # StrategyRuntimeError carries a meaningful etype.
                # Plain ``raise NotImplementedError(...)`` from strategy code
                # (e.g. ``on_bar`` placeholders) deliberately falls through
                # to the generic ``runtime_error`` branch below. See #383.
                tb = "".join(traceback.format_exception(*sys.exc_info()))
                _emit({"kind": "error", "etype": "unsupported_feature",
                       "message": f"{exc!s}\\n{tb}"})
                sys.exit(1)
            except Exception as exc:
                tb = "".join(traceback.format_exception(*sys.exc_info()))
                _emit({"kind": "error", "etype": "runtime_error",
                       "message": f"{exc!s}\\n{tb}"})
                sys.exit(1)

            # Always include capabilities so even a parent that only
            # inspects the *first* ready (e.g. legacy debug tooling) can
            # negotiate, and a parent that re-checks before chunking
            # always sees fresh state.
            _emit({"kind": "ready", "capabilities": _CAPABILITIES})


    def _apply_state(ctx, state, *, is_warmup):
        positions = []
        for p in state.get("positions") or []:
            positions.append(contract._PositionSnapshot(**p))
        ctx._ingest_state(
            capital=float(state.get("capital", 0.0)),
            equity=float(state.get("equity", 0.0)),
            positions=positions,
            is_warmup=is_warmup,
        )


    if __name__ == "__main__":
        main()
''')
