"""Cancellation-safe async wrapper around :func:`flock_lock` for checkout locks.

``flock_lock`` itself is a synchronous, blocking context manager (it calls the
blocking ``fcntl.flock`` syscall). Callers that need to hold it from async code
without blocking the event loop run its ``__enter__``/``__exit__`` in an
executor thread via ``loop.run_in_executor`` -- and that indirection has a
sharp edge: if the *awaiting* coroutine is cancelled (e.g. a client
disconnects) while ``__enter__`` is still running in the executor thread, the
cancellation reaches the coroutine immediately (asyncio delivers it
synchronously to the current await point), but the executor thread is not
stopped by that -- it keeps running and can go on to actually acquire the
flock *after* the coroutine has already unwound past any ``finally`` that
only checked a boolean set by the (now-abandoned) awaiting code. The result is
a flock held forever with nothing left able to release it.

:func:`held_checkout_lock` is the single, tested primitive for "acquire this
checkout's flock from async code, safely, even under cancellation" -- it was
independently hand-rolled (with this exact bug) at two call sites in
``unified_api.routes.integrations`` (``run_github_issue`` and
``address_github_pr_comments``); both now build on this instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from shared.concurrency.flock_lock import flock_lock

__all__ = ["CloneLockAcquisitionError", "held_checkout_lock"]

logger = logging.getLogger(__name__)


class CloneLockAcquisitionError(OSError):
    """Raised by :func:`held_checkout_lock` when ACQUIRING the checkout lock fails.

    Exists so callers can tell "this checkout's serialization lock could not be
    taken" (a local, transient, retryable condition worth a 503) apart from every
    OTHER ``OSError`` that can escape the ``async with`` body -- a missing git
    binary, a permission error writing the checkout, a full disk, or an
    ``aiohttp.ClientOSError`` from an upstream HTTP call, none of which are lock
    problems and none of which are necessarily retryable. Without this
    distinction a caller wrapping the whole block in ``except OSError`` can only
    guess, and would mislabel all of them as lock failures.

    Subclasses ``OSError`` deliberately: the previously documented contract was
    that acquisition failures propagate AS ``OSError``, and existing callers
    catching that keep working unchanged. Callers wanting the distinction catch
    this class BEFORE their generic ``OSError`` handler.

    Invariants:
        - Only ever raised for an acquisition failure (``mkdir`` or ``flock``),
          never for anything raised by the ``async with`` body.
        - The originating ``OSError`` is always attached as ``__cause__``.
    """


@asynccontextmanager
async def held_checkout_lock(
    loop: asyncio.AbstractEventLoop,
    lock_path: Path,
    *,
    platform_owned: bool,
    owner: str,
    repo: str,
    log_prefix: str,
) -> AsyncIterator[None]:
    """Hold ``flock_lock(lock_path)`` for the duration of the ``async with`` block.

    Creates ``lock_path``'s parent directory first (``flock_lock`` requires it
    to already exist); a ``mkdir(exist_ok=True)`` on an already-existing
    parent is a no-op needing no write permission, so this stays safe for an
    operator-pinned path under a read-only parent.

    Preconditions:
        - ``loop`` is the running event loop.
        - ``lock_path``'s parent may or may not exist yet.
        - ``owner``/``repo`` name the GitHub repository this checkout belongs
          to. They are diagnostic only -- they never take part in the lock
          key (``lock_path`` alone identifies the lock) -- and are included in
          the acquisition-failure warning below so an operator triaging a
          degraded pinned checkout can tell WHICH repository's run went
          unserialized from the log line alone, without correlating the
          opaque lock path back to a repo.

    Postconditions:
        - When acquisition (mkdir or flock) fails with an ``OSError`` and
          ``platform_owned`` is True, it propagates to the caller wrapped in
          :class:`CloneLockAcquisitionError` (itself an ``OSError``, with the
          original attached as ``__cause__``) and the block's body never runs
          -- callers map it to whatever failure response fits their context
          (this helper is intentionally HTTP-agnostic). The wrapper type is
          what lets a caller distinguish a genuine lock-acquisition failure
          from an unrelated ``OSError`` escaping the body, which its own
          generic ``except OSError`` would otherwise mislabel as one.
        - When acquisition fails with an ``OSError`` and ``platform_owned``
          is False (an operator-pinned checkout), the failure degrades to no
          additional locking: a warning is logged (tagged with
          ``log_prefix``, and naming ``owner``/``repo``) and the block's body
          runs WITHOUT the lock held, matching the pre-existing
          best-effort-for-pinned-paths contract.
        - Once acquired, the lock is guaranteed released by the time this
          context manager exits -- including when the awaiting coroutine is
          cancelled while the ``run_in_executor`` acquisition is still in
          flight (see module docstring): the acquisition is tracked as its
          own ``Future`` via :func:`asyncio.shield`, so a cancellation of
          *this* coroutine does not cancel the underlying acquisition; the
          ``finally`` below always awaits that future (ALSO shielded -- see
          below) to learn whether it actually completed, and releases the
          flock if so. A flock that never completes acquisition (mkdir
          failed, or the executor thread's ``__enter__`` itself raised) is
          correctly never "released" since it was never held.
        - The ``finally`` block's own await of ``acquire_future`` is itself
          wrapped in :func:`asyncio.shield`, and retried in a loop until the
          future is actually ``done()``: without the loop, a SECOND
          cancellation delivered to this coroutine while it is in the middle
          of that cleanup await would abort just that one shielded await
          (shielding protects the wrapped future from being cancelled, not
          the awaiting coroutine from being cancelled again) and unwind this
          coroutine with ``lock_acquired`` still False -- even though the
          executor thread can still go on to actually acquire the flock
          moments later, with no code left anywhere to release it. This is
          the same underlying hazard the module docstring describes for the
          initial acquisition, one level deeper: a single shielded await is
          not enough once a caller can be cancelled repeatedly; only looping
          until the future is genuinely done makes cleanup itself
          cancellation-safe against any number of repeat cancellations.
        - A release that RAISES is logged at WARNING (naming ``lock_path``) and
          never propagated: this cleanup runs in the ``finally``, so raising
          would displace whatever exception the body was already propagating.
          Inspecting the release future's ``exception()`` is what makes the
          failure visible at all -- nothing else awaits its result, so an
          unlogged failure would surface only as asyncio's incidental
          "exception was never retrieved" message at GC time.
        - A ``CancelledError`` raised while awaiting acquisition propagates
          to the caller as normal (this helper does not suppress
          cancellation); only the release bookkeeping is cancellation-safe.
    """
    lock_cm = flock_lock(lock_path)
    acquire_future: "asyncio.Future[None] | None" = None
    lock_acquired = False
    try:
        try:
            await loop.run_in_executor(None, functools.partial(lock_path.parent.mkdir, parents=True, exist_ok=True))
            # Tracked as its own Future (not just awaited inline) so that if THIS
            # await is cancelled, asyncio.shield keeps the underlying executor
            # work running to completion rather than abandoning it -- and the
            # finally below can still await it to find out how it landed.
            acquire_future = loop.run_in_executor(None, lock_cm.__enter__)
            await asyncio.shield(acquire_future)
            lock_acquired = True
        except OSError as e:
            if platform_owned:
                # Wrapped, not re-raised bare: callers wrap the whole `async
                # with` body in an OSError handler, and only THIS failure is a
                # lock-acquisition problem.
                raise CloneLockAcquisitionError(f"could not acquire checkout lock {lock_path}: {e}") from e
            logger.warning(
                "%s: could not acquire serialization lock for pinned checkout %s (%s/%s); proceeding without it: %s",
                log_prefix,
                lock_path,
                owner,
                repo,
                e,
            )
        yield
    finally:
        if acquire_future is not None and not lock_acquired:
            # Either this coroutine was cancelled while shielding the
            # acquisition (so it never observed the outcome), or the OSError
            # branch above already consumed a synchronous failure -- either
            # way, wait for the (possibly still-running) executor thread to
            # actually finish so a completed acquisition is never leaked.
            #
            # Looped rather than a single shielded await: asyncio.shield
            # protects the WRAPPED future from being cancelled, not this
            # coroutine from being cancelled again while awaiting it -- a
            # repeat cancellation here aborts only that one await, leaving
            # acquire_future still running in the background with nothing
            # left to observe it. Retrying the shielded await until the
            # future is genuinely `done()` makes this robust to any number
            # of repeat cancellations.
            while not acquire_future.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(acquire_future)
            if not acquire_future.cancelled():
                exc = acquire_future.exception()
                if exc is None:
                    lock_acquired = True
                elif not isinstance(exc, OSError):
                    raise exc
        if lock_acquired:
            # Pass the ACTIVE exception info (not a hardcoded None, None, None)
            # so a context manager that inspects it (e.g. to log what error was
            # in flight) sees the real one. This is NOT a full semantic
            # replacement for a real `with` block, though: a `with` statement
            # also honors __exit__'s return value to decide whether to
            # suppress the active exception, and this call discards that
            # return value -- lock_cm.__exit__ returning True here would NOT
            # suppress anything, unlike a real `with`.
            # flock releases are per open-file-description, not per-thread, so
            # releasing from a different executor thread than the one that
            # acquired it (or from a coroutine that never itself observed the
            # acquisition) is safe.
            exc_type, exc_val, exc_tb = sys.exc_info()
            # Hardened the same way as acquisition above: tracked as its own
            # Future and retried under shield until done(), so a repeat
            # cancellation while awaiting release cannot abandon the executor
            # work item before __exit__ actually runs (which would leak the
            # flock).
            release_future = loop.run_in_executor(None, lock_cm.__exit__, exc_type, exc_val, exc_tb)
            while not release_future.done():
                # TWO things are suppressed here, for different reasons.
                # ``CancelledError``: a repeat cancellation of THIS coroutine
                # aborts only this one await, so the loop retries it until the
                # future is genuinely done. ``Exception``: a release that FAILED
                # would otherwise be re-raised by this await, out of the
                # ``finally``, DISPLACING whatever exception the body was
                # already propagating -- turning a real error into a confusing
                # unlock error. The failure is not swallowed: it is reported by
                # the explicit inspection below.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(release_future)
            # A release that RAISED must not go unreported. Nothing awaits this
            # future's result, so without this the only trace of a failed
            # release is asyncio's incidental "exception was never retrieved"
            # message at GC time -- arbitrarily later, on an unrelated stack,
            # and easy to miss entirely. The contract of this primitive is that
            # the lock is always released, so a violation of it is exactly what
            # an operator needs to see: a stuck flock wedges every later
            # checkout on this path. Logged, never raised: this runs in the
            # generator's finally, and raising here would replace whatever
            # exception the body was already propagating.
            if not release_future.cancelled():
                release_exc = release_future.exception()
                if release_exc is not None:
                    logger.warning(
                        "checkout lock release failed for %s: %s",
                        lock_path,
                        release_exc,
                        exc_info=release_exc,
                    )
