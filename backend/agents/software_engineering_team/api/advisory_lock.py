"""coding_team API — shared cross-worker admission-lock primitive.

Combines a process-local lock with a best-effort Postgres transaction-scoped
advisory lock, degrading to the process lock alone when Postgres is
unavailable. For any caller that needs cross-worker mutual exclusion keyed
on a namespace/key pair.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Iterator

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def advisory_lock(process_lock: threading.Lock, namespace: str, key: str) -> Iterator[None]:
    """Mutual exclusion combining a process-local lock with a best-effort Postgres advisory lock.

    Preconditions:
        - ``process_lock`` is the caller's process-local lock; ``namespace``/``key``
          identify the resource being admitted (passed verbatim to ``hashtext``).
    Postconditions:
        - While the ``with`` body runs, no other caller using the SAME ``process_lock``
          can run — in this process always, and across worker processes via
          ``pg_advisory_xact_lock(hashtext(namespace), hashtext(key))`` when Postgres
          is configured. The advisory lock auto-releases when its transaction ends
          (body exit, exception, or connection death — crash-safe). When Postgres is
          unconfigured or the lock cannot be taken, degrades to the process-local lock
          alone (logged): single-worker serialization stays intact, and the residual
          cross-worker window is the pre-lock behavior, never worse. Exceptions from
          the body propagate unchanged; lock acquisition itself never raises.

    Invariants:
        - ``process_lock`` is always taken before, and released after, the advisory
          lock's transaction, so lock ordering is fixed and deadlock-free.
    """
    with process_lock, contextlib.ExitStack() as stack:
        try:
            from shared_postgres import (  # noqa: PLC0415 - optional dep path
                get_conn,
                is_postgres_enabled,
            )

            if is_postgres_enabled():
                conn = stack.enter_context(get_conn())
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (namespace, key),
                )
        except Exception:  # noqa: BLE001 - degrade to process-local lock, never block the caller
            # Release the Postgres connection (and its transaction) immediately; the
            # advisory lock, if acquired, is now released.
            stack.pop_all().close()
            logger.warning(
                "could not take cross-worker advisory lock (namespace=%s, key=%s); "
                "falling back to process-local locking only",
                namespace,
                key,
                exc_info=True,
            )
        yield
