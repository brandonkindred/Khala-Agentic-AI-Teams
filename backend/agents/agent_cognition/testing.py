"""Shared test doubles for the Agent Cognition Core (mirrors ``shared_postgres.testing``).

Test-only helpers importable by any suite that exercises the invoke gate or
the run ledger without a live Postgres — currently the gate unit tests
(``agent_cognition/tests``) and the unified API route tests
(``unified_api/tests``). Keeping one fake here prevents the two suites from
drifting on claim/complete semantics.
"""

from __future__ import annotations

from typing import Any

from agent_cognition.context import ClaimResult, ClaimState


class FakeRunLedger:
    """In-memory stand-in for the ``agent_cognition_runs`` claim/complete/abandon trio.

    Mirrors the facade's observable semantics (see ``agent_cognition.context``):
    first-sight insert → CLAIMED; terminal row + matching hash → REPLAY; hash
    mismatch or valid lease → CONFLICT; expired lease → in-place reclaim with a
    rotated token. Lease expiry is modelled as the boolean ``lease_valid`` flag
    on a row — tests flip it instead of advancing a clock.

    Invariants:
        * ``rows`` is keyed ``(agent_id, source_run_id)``; each row carries
          ``status`` / ``hash`` / ``token`` / ``response`` / ``lease_valid``.
        * ``claim_calls`` counts every ``claim_run`` invocation (tests assert
          ledger silence on paths that must not claim).
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.claim_calls = 0

    def claim_run(self, agent_id, source_run_id, request_hash, lease) -> ClaimResult:
        """Mirror ``context.claim_run``'s CLAIMED/REPLAY/CONFLICT/reclaim contract."""
        self.claim_calls += 1
        key = (agent_id, source_run_id)
        row = self.rows.get(key)
        if row is None:
            token = f"tok-{self.claim_calls}"
            self.rows[key] = {
                "status": "in_progress",
                "hash": request_hash,
                "token": token,
                "response": None,
                "lease_valid": True,
            }
            return ClaimResult(ClaimState.CLAIMED, claim_token=token)
        if row["hash"] != request_hash:
            return ClaimResult(ClaimState.CONFLICT)
        if row["status"] in ("completed", "blocked"):
            return ClaimResult(ClaimState.REPLAY, response=row["response"])
        if row["lease_valid"]:
            return ClaimResult(ClaimState.CONFLICT)
        row["lease_valid"] = True
        row["token"] = f"tok-{self.claim_calls}"
        return ClaimResult(ClaimState.CLAIMED, claim_token=row["token"])

    def complete_run(self, agent_id, source_run_id, *, status, response, claim_token) -> None:
        """Mirror ``context.complete_run``: only the owning claim may complete."""
        row = self.rows[(agent_id, source_run_id)]
        assert row["token"] == claim_token, "complete with a stale claim token"
        row["status"] = status
        row["response"] = dict(response)

    def abandon_run(self, agent_id, source_run_id, *, claim_token) -> bool:
        """Mirror ``context.abandon_run``: delete only the owned in-progress row."""
        key = (agent_id, source_run_id)
        row = self.rows.get(key)
        if row is None or row["status"] != "in_progress" or row["token"] != claim_token:
            return False
        del self.rows[key]
        return True
