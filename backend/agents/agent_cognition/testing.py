"""Shared test doubles for the Agent Cognition Core (mirrors ``shared_postgres.testing``).

Test-only helpers importable by any suite that exercises the invoke gate or
the run ledger without a live Postgres — currently the gate unit tests
(``agent_cognition/tests``) and the unified API route tests
(``unified_api/tests``). Keeping one fake here prevents the two suites from
drifting on claim/complete semantics.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agent_cognition.context import ClaimResult, ClaimState
from agent_cognition.models import Rule, RuleMode, RuleSource, RuleStatus

_RULE_TS = datetime(2026, 6, 1, tzinfo=timezone.utc)


class FakeGraphiti:
    """In-memory stand-in for the Graphiti client's ``search`` (no live Neo4j).

    Shared by the graph-retrieval tests and the reflection-grounding tests so the
    two suites cannot drift on the search contract. Returns one fact-bearing
    object per configured fact, or raises ``error`` to exercise the failure path.

    Invariants:
        * ``calls`` records every ``search`` invocation's kwargs in order.
    """

    def __init__(self, facts: list[str] | None = None, error: Exception | None = None) -> None:
        self._facts = facts or []
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def search(self, *, query, group_ids, num_results):
        self.calls.append({"query": query, "group_ids": group_ids, "num_results": num_results})
        if self._error is not None:
            raise self._error
        return [types.SimpleNamespace(fact=f) for f in self._facts]


def make_rule(
    predicate: dict[str, Any],
    *,
    agent_id: str,
    mode: RuleMode = RuleMode.ENFORCED,
    rule_id: str | None = None,
    text: str = "test rule",
) -> Rule:
    """Build an active test ``Rule`` around a predicate (shared by the suites).

    One builder for the gate unit tests and the unified API route tests, so the
    fixture shape (active status, operator source, fixed timestamps) cannot
    drift between them.

    Preconditions:
        * ``agent_id`` is non-empty; ``predicate`` is a predicate-DSL dict.
    Postconditions:
        * Returns an ``ACTIVE`` rule with the given mode and a stable id
          (``rule_id`` or a fresh UUID-suffixed one).
    """
    assert agent_id, "make_rule: agent_id must be non-empty"
    return Rule(
        id=rule_id or f"r-{uuid4()}",
        agent_id=agent_id,
        text=text,
        mode=mode,
        status=RuleStatus.ACTIVE,
        predicate=predicate,
        source=RuleSource.OPERATOR,
        created_at=_RULE_TS,
        updated_at=_RULE_TS,
    )


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
