"""Tests for the TradingView MCP data source and its wiring into MarketDataService."""

from __future__ import annotations

import pytest

from investment_team.market_data_service import MarketDataService
from investment_team.tradingview_mcp import client as tv_client_mod
from investment_team.tradingview_mcp.client import TradingViewMcpClient, TradingViewMcpError
from investment_team.tradingview_mcp.config import (
    TradingViewMcpConfig,
    resolve_tradingview_mcp_config,
)
from investment_team.tradingview_mcp.provider import build_tradingview_client

# ---------------------------------------------------------------------------
# httpx fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, json_body=None, *, text: str = "", content_type: str = "application/json"):
        self._json = json_body
        self.text = text
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        return None

    def json(self):
        return self._json


class _FakeClient:
    """Context-manager stand-in for httpx.Client that returns a canned response."""

    def __init__(self, response=None, *, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.last_post = None

    def __call__(self, *args, **kwargs):  # allow use as the Client factory
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.last_post = {"url": url, "json": json, "headers": headers}
        if self._raise is not None:
            raise self._raise
        return self._response


def _patch_httpx(monkeypatch, response=None, *, raise_exc=None) -> _FakeClient:
    fake = _FakeClient(response, raise_exc=raise_exc)
    monkeypatch.setattr(tv_client_mod.httpx, "Client", fake)
    return fake


def _tool_result(rows) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": rows}}


def _patch_store(monkeypatch, meta=None, token="", *, token_spy=None):
    """Patch the config resolver's store accessors.

    ``meta`` None ⇒ store unavailable (both accessors None). Otherwise ``get_meta``
    returns ``meta`` and ``get_token`` returns ``token`` (or calls ``token_spy``).
    """
    if meta is None:
        monkeypatch.setattr(
            "investment_team.tradingview_mcp.config._store_accessors",
            lambda: (None, None),
        )
        return
    get_meta = lambda: meta  # noqa: E731
    get_token = token_spy if token_spy is not None else (lambda: token)  # noqa: E731
    monkeypatch.setattr(
        "investment_team.tradingview_mcp.config._store_accessors",
        lambda: (get_meta, get_token),
    )


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def _clear_env(monkeypatch):
    for var in (
        "TRADINGVIEW_MCP_ENABLED",
        "TRADINGVIEW_MCP_URL",
        "TRADINGVIEW_MCP_TOKEN",
        "TRADINGVIEW_MCP_TOOL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_config_env_overrides_store(monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_ENABLED", "true")
    monkeypatch.setenv("TRADINGVIEW_MCP_URL", "https://env.example/mcp")
    monkeypatch.setenv("TRADINGVIEW_MCP_TOKEN", "env-token")
    monkeypatch.setenv("TRADINGVIEW_MCP_TOOL", "env_tool")
    # Even if the store returns something, env wins (and the store isn't even read).
    _patch_store(
        monkeypatch,
        meta={"enabled": False, "mcp_server_url": "https://store/mcp", "tool_name": "t"},
        token="s",
    )
    cfg = resolve_tradingview_mcp_config()
    assert cfg.enabled is True
    assert cfg.server_url == "https://env.example/mcp"
    assert cfg.auth_token == "env-token"
    assert cfg.tool_name == "env_tool"
    assert cfg.usable is True


def test_config_falls_back_to_store(monkeypatch):
    _clear_env(monkeypatch)
    _patch_store(
        monkeypatch,
        meta={"enabled": True, "mcp_server_url": "https://store/mcp", "tool_name": ""},
        token="s",
    )
    cfg = resolve_tradingview_mcp_config()
    assert cfg.enabled is True
    assert cfg.server_url == "https://store/mcp"
    assert cfg.tool_name == "get_ohlcv"  # blank store value defaults
    assert cfg.auth_token == "s"  # token read because enabled + URL


def test_config_disabled_when_nothing(monkeypatch):
    _clear_env(monkeypatch)
    _patch_store(monkeypatch, meta=None)  # store unavailable
    cfg = resolve_tradingview_mcp_config()
    assert cfg.enabled is False
    assert cfg.usable is False


def test_config_env_disabled_overrides_store_enabled(monkeypatch):
    # An explicit falsey env flag is an operator override, not a fallback.
    _clear_env(monkeypatch)
    monkeypatch.setenv("TRADINGVIEW_MCP_ENABLED", "false")
    _patch_store(
        monkeypatch,
        meta={"enabled": True, "mcp_server_url": "https://store/mcp", "tool_name": ""},
        token="s",
    )
    cfg = resolve_tradingview_mcp_config()
    assert cfg.enabled is False
    assert cfg.usable is False


def test_config_skips_token_read_when_disabled(monkeypatch):
    # P2: the encrypted-token accessor must not be hit on the disabled path.
    _clear_env(monkeypatch)
    calls = {"n": 0}

    def _spy_token():
        calls["n"] += 1
        return "s"

    _patch_store(
        monkeypatch,
        meta={"enabled": False, "mcp_server_url": "https://store/mcp", "tool_name": ""},
        token_spy=_spy_token,
    )
    cfg = resolve_tradingview_mcp_config()
    assert cfg.enabled is False
    assert cfg.auth_token == ""
    assert calls["n"] == 0  # token never read while disabled


def test_config_usable_requires_url(monkeypatch):
    cfg = TradingViewMcpConfig(enabled=True, server_url="", auth_token="")
    assert cfg.usable is False


# ---------------------------------------------------------------------------
# Client parsing
# ---------------------------------------------------------------------------


def test_client_parses_structured_content(monkeypatch):
    rows = [
        {"date": "2024-01-02", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
        {"date": "2024-01-03", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 200},
    ]
    _patch_httpx(monkeypatch, _FakeResponse(_tool_result(rows)))
    c = TradingViewMcpClient("https://tv/mcp", auth_token="tok")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert len(out) == 2
    assert out[0]["date"] == "2024-01-02"
    assert out[1]["close"] == 2.0


def test_client_sends_auth_header(monkeypatch):
    fake = _patch_httpx(monkeypatch, _FakeResponse(_tool_result([])))
    c = TradingViewMcpClient("https://tv/mcp", auth_token="tok", tool_name="my_tool")
    c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert fake.last_post["headers"]["Authorization"] == "Bearer tok"
    assert fake.last_post["json"]["params"]["name"] == "my_tool"
    assert fake.last_post["json"]["params"]["arguments"]["symbol"] == "AAPL"


def test_client_parses_text_content_json(monkeypatch):
    import json

    rows = [{"time": "2024-05-06T00:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 5}]
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(rows)}]},
    }
    _patch_httpx(monkeypatch, _FakeResponse(body))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("BTC", "crypto", "2024-05-01", "2024-05-31")
    assert out[0]["date"] == "2024-05-06"
    assert out[0]["open"] == 10
    assert out[0]["close"] == 10.5


def test_client_parses_nested_container(monkeypatch):
    rows = {"bars": [{"date": "2024-01-02", "close": 1.5}]}
    _patch_httpx(monkeypatch, _FakeResponse(_tool_result(rows)))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert out[0]["date"] == "2024-01-02"
    # open/high/low absent -> filled from close by the client (a flat bar).
    assert out[0]["open"] == out[0]["high"] == out[0]["low"] == out[0]["close"] == 1.5


def test_client_parses_epoch_seconds(monkeypatch):
    rows = [{"timestamp": 1704153600, "close": 42}]  # 2024-01-02 UTC
    _patch_httpx(monkeypatch, _FakeResponse(_tool_result(rows)))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert out[0]["date"] == "2024-01-02"


def test_client_parses_epoch_millis(monkeypatch):
    rows = [{"timestamp": 1704153600000, "close": 42}]  # 2024-01-02 UTC in ms
    _patch_httpx(monkeypatch, _FakeResponse(_tool_result(rows)))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert out[0]["date"] == "2024-01-02"


def test_client_parses_compact_yyyymmdd(monkeypatch):
    # A compact integer date must be read as a calendar day, not an epoch.
    rows = [{"date": 20240102, "close": 5}, {"date": "20240103", "close": 6}]
    _patch_httpx(monkeypatch, _FakeResponse(_tool_result(rows)))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert [r["date"] for r in out] == ["2024-01-02", "2024-01-03"]


def test_client_drops_unparseable_date(monkeypatch):
    rows = [{"date": "not-a-date", "close": 5}, {"date": "2024-01-02", "close": 6}]
    _patch_httpx(monkeypatch, _FakeResponse(_tool_result(rows)))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert [r["date"] for r in out] == ["2024-01-02"]


def test_client_drops_row_without_date_or_close(monkeypatch):
    rows = [{"open": 1, "close": 2}, {"date": "2024-01-02"}, {"date": "2024-01-03", "close": 5}]
    _patch_httpx(monkeypatch, _FakeResponse(_tool_result(rows)))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert len(out) == 1
    assert out[0]["date"] == "2024-01-03"


def test_client_raises_on_jsonrpc_error(monkeypatch):
    body = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
    _patch_httpx(monkeypatch, _FakeResponse(body))
    c = TradingViewMcpClient("https://tv/mcp")
    with pytest.raises(TradingViewMcpError, match="boom"):
        c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")


def test_client_raises_on_tool_error(monkeypatch):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"isError": True, "content": [{"type": "text", "text": "rate limited"}]},
    }
    _patch_httpx(monkeypatch, _FakeResponse(body))
    c = TradingViewMcpClient("https://tv/mcp")
    with pytest.raises(TradingViewMcpError, match="rate limited"):
        c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")


def test_client_raises_on_http_error(monkeypatch):
    import httpx

    _patch_httpx(monkeypatch, raise_exc=httpx.ConnectError("refused"))
    c = TradingViewMcpClient("https://tv/mcp")
    with pytest.raises(TradingViewMcpError, match="request failed"):
        c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")


def test_client_decodes_sse(monkeypatch):
    import json

    rows = [{"date": "2024-01-02", "close": 3}]
    payload = json.dumps(_tool_result(rows))
    sse_text = f"event: message\ndata: {payload}\n\n"
    _patch_httpx(monkeypatch, _FakeResponse(None, text=sse_text, content_type="text/event-stream"))
    c = TradingViewMcpClient("https://tv/mcp")
    out = c.fetch_ohlcv("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert out[0]["date"] == "2024-01-02"


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def test_build_client_none_when_disabled():
    cfg = TradingViewMcpConfig(enabled=False, server_url="https://tv/mcp", auth_token="")
    assert build_tradingview_client(cfg) is None


def test_build_client_when_usable():
    cfg = TradingViewMcpConfig(
        enabled=True, server_url="https://tv/mcp", auth_token="t", tool_name="x"
    )
    c = build_tradingview_client(cfg)
    assert isinstance(c, TradingViewMcpClient)
    assert c.server_url == "https://tv/mcp"
    assert c.tool_name == "x"


# ---------------------------------------------------------------------------
# MarketDataService wiring
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self, rows):
        self._rows = rows

    def fetch_ohlcv(self, symbol, asset_class, start_date, end_date, *, interval="1d"):
        return self._rows


def test_market_data_chain_prepends_tradingview():
    svc = MarketDataService(tradingview_client=_StubClient([]))
    chain = svc._get_named_provider_chain("stocks")
    assert chain[0][0] == "tradingview_mcp"
    crypto_chain = svc._get_named_provider_chain("crypto")
    assert crypto_chain[0][0] == "tradingview_mcp"


def test_market_data_chain_unchanged_without_client():
    svc = MarketDataService(tradingview_client=None)
    slugs = [slug for slug, _ in svc._get_named_provider_chain("stocks")]
    assert "tradingview_mcp" not in slugs


def test_fetch_tradingview_mcp_returns_bars():
    rows = [
        {"date": "2024-01-02", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
        {"date": "2024-01-03", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 200},
    ]
    svc = MarketDataService(tradingview_client=_StubClient(rows))
    bars = svc._fetch_tradingview_mcp("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert [b.date for b in bars] == ["2024-01-02", "2024-01-03"]
    assert bars[0].close == 1.5
    assert bars[1].volume == 200


def _flat(date_str, price):
    """A fully-formed (client-normalized) flat bar row."""
    return {
        "date": date_str,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 0,
    }


def test_fetch_tradingview_mcp_accepts_flat_bars():
    # The client fills O/H/L from close; the service consumes the already-flat row.
    svc = MarketDataService(tradingview_client=_StubClient([_flat("2024-01-02", 5.0)]))
    bars = svc._fetch_tradingview_mcp("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert len(bars) == 1
    assert bars[0].open == bars[0].high == bars[0].low == bars[0].close == 5.0


def test_fetch_tradingview_mcp_filters_out_of_range():
    rows = [_flat("2023-12-31", 1.0), _flat("2024-01-02", 2.0), _flat("2024-02-15", 3.0)]
    svc = MarketDataService(tradingview_client=_StubClient(rows))
    bars = svc._fetch_tradingview_mcp("AAPL", "stocks", "2024-01-01", "2024-01-31")
    assert [b.date for b in bars] == ["2024-01-02"]


def test_fetch_tradingview_mcp_falls_back_on_error():
    class _Boom:
        def fetch_ohlcv(self, *a, **k):
            raise RuntimeError("nope")

    svc = MarketDataService(tradingview_client=_Boom())
    assert svc._fetch_tradingview_mcp("AAPL", "stocks", "2024-01-01", "2024-01-31") == []


def test_market_data_client_resolved_lazily_and_memoized(monkeypatch):
    # P1: construction does NO config resolution; the chain resolves once, then caches.
    import investment_team.market_data_service as mds

    calls = {"n": 0}

    def _fake_resolve():
        calls["n"] += 1
        return None

    monkeypatch.setattr(mds, "_resolve_tradingview_client", _fake_resolve)
    svc = MarketDataService()  # default → _UNSET, must not resolve yet
    assert calls["n"] == 0
    svc._get_named_provider_chain("stocks")
    assert calls["n"] == 1
    svc._get_named_provider_chain("crypto")
    assert calls["n"] == 1  # memoized, not re-resolved
