"""End-to-end structured-wire smoke for issue #557.

Verifies the contract step 8 of the #537 migration calls out:
- A structured ``StrategySpec`` POST to ``/strategies`` returns 200 and
  echoes the structured shape.
- Prose-shaped ``entry_rules`` / ``exit_rules`` / ``sizing`` payloads
  return HTTP 422 (Pydantic discriminator rejection).

Uses ``TestClient`` against the FastAPI app with the persistence dict
swapped for an in-memory ``dict`` so the test doesn't need the job
service or Postgres.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(monkeypatch_module) -> TestClient:
    from investment_team.api import main as api_main

    monkeypatch_module.setattr(api_main, "_strategies", {})
    with TestClient(api_main.app) as c:
        yield c


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


_STRUCTURED_BODY = {
    "authored_by": "smoke-test",
    "asset_class": "stocks",
    "hypothesis": "sma crossover",
    "signal_definition": "close > sma(20)",
    "timeframe": "1d",
    "entry_rules": [
        {
            "kind": "entry",
            "side": "long",
            "when": {
                "lhs": "bar.close",
                "op": ">",
                "rhs": {"name": "sma", "params": {"period": 20}},
            },
        }
    ],
    "exit_rules": [{"kind": "stop_loss", "pct": 0.03}],
    "sizing": {"kind": "fixed_fraction", "fraction": 0.02},
}


def test_structured_strategy_post_round_trips(client: TestClient) -> None:
    response = client.post("/strategies", json=_STRUCTURED_BODY)
    assert response.status_code == 200, response.text

    strategy = response.json()["strategy"]
    assert strategy["entry_rules"][0]["kind"] == "entry"
    assert strategy["entry_rules"][0]["side"] == "long"
    assert strategy["entry_rules"][0]["when"]["lhs"] == "bar.close"
    assert strategy["exit_rules"][0]["kind"] == "stop_loss"
    assert strategy["exit_rules"][0]["pct"] == 0.03
    assert strategy["sizing"]["kind"] == "fixed_fraction"
    assert strategy["sizing"]["fraction"] == 0.02


@pytest.mark.parametrize(
    "field, prose_value",
    [
        ("entry_rules", ["close > sma(20)"]),
        ("exit_rules", ["stop loss 3%"]),
        ("sizing", "risk 2% per trade"),
    ],
)
def test_prose_strategy_post_rejected_with_422(
    client: TestClient, field: str, prose_value: object
) -> None:
    body = dict(_STRUCTURED_BODY)
    body[field] = prose_value

    response = client.post("/strategies", json=body)

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "legacy_field, legacy_value",
    [
        ("sizing_rules", ["risk 2% per trade"]),
        ("unparsed_rules", ["enter on vibes"]),
        ("requires_redesign", True),
    ],
)
def test_legacy_field_names_rejected_with_422(
    client: TestClient, legacy_field: str, legacy_value: object
) -> None:
    """Stale-client payloads carrying legacy/internal field names hit
    ``extra='forbid'`` on ``CreateStrategyRequest`` and are rejected at
    the HTTP boundary instead of being silently dropped (which would let
    a legacy ``sizing_rules`` payload fall back to default sizing and
    return 200 with the wrong sizing rule)."""
    body = dict(_STRUCTURED_BODY)
    body[legacy_field] = legacy_value

    response = client.post("/strategies", json=body)

    assert response.status_code == 422, response.text
