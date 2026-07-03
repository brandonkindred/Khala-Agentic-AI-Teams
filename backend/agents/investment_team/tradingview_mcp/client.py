"""Streamable-HTTP JSON-RPC client for a TradingView MCP server.

The client issues a single MCP ``tools/call`` request per fetch and normalizes the
tool result into a list of raw OHLCV row dicts (``date``/``open``/``high``/``low``/
``close``/``volume``). It is deliberately tolerant of the two shapes an MCP tool can
return its payload in — a ``structuredContent`` object or a JSON text content block —
and of common field aliases, because there is no single canonical TradingView MCP
server. Value coercion / finiteness repair is *not* done here: the caller
(:class:`MarketDataService`) owns that so the TradingView path shares the exact same
normalization as every other provider.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

# Field-name aliases seen across MCP OHLCV tools, mapped to our canonical keys.
_DATE_KEYS = ("date", "datetime", "time", "timestamp", "t")
_OPEN_KEYS = ("open", "o")
_HIGH_KEYS = ("high", "h")
_LOW_KEYS = ("low", "l")
_CLOSE_KEYS = ("close", "c")
_VOLUME_KEYS = ("volume", "vol", "v")

# Keys under which a tool may nest the row list inside a structured object.
_ROWS_CONTAINER_KEYS = ("bars", "values", "ohlcv", "candles", "data", "series")


class TradingViewMcpError(RuntimeError):
    """Raised when the TradingView MCP server returns a protocol/tool error."""


class TradingViewMcpClient:
    """Minimal MCP ``tools/call`` client for OHLCV retrieval.

    Preconditions (constructor): ``server_url`` is a non-empty http(s) URL.
    Invariants: the client is stateless between calls apart from its immutable config;
        it opens and closes one short-lived HTTP connection per :meth:`fetch_ohlcv`.
    """

    def __init__(
        self,
        server_url: str,
        *,
        auth_token: str = "",
        tool_name: str = "get_ohlcv",
        timeout: float = 30.0,
    ) -> None:
        assert server_url, "server_url is required"
        self.server_url = server_url
        self.auth_token = auth_token
        self.tool_name = tool_name or "get_ohlcv"
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        asset_class: str,
        start_date: str,
        end_date: str,
        *,
        interval: str = "1d",
    ) -> List[Dict[str, Any]]:
        """Call the MCP OHLCV tool and return raw row dicts (oldest→newest not guaranteed).

        Preconditions: ``symbol`` is non-empty; ``start_date``/``end_date`` are ISO dates.
        Postconditions: returns a list of dicts each carrying canonical keys ``date``,
            ``open``, ``high``, ``low``, ``close``, ``volume`` (raw provider values, not
            coerced). Returns ``[]`` when the tool reports no data. Raises
            :class:`TradingViewMcpError` on an HTTP failure, a JSON-RPC error, or an MCP
            tool error so the caller's provider chain can fall back to the next source.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": self.tool_name,
                "arguments": {
                    "symbol": symbol,
                    "asset_class": asset_class,
                    "start_date": start_date,
                    "end_date": end_date,
                    "interval": interval,
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            # MCP streamable HTTP allows a JSON or an SSE response; accept both.
            "Accept": "application/json, text/event-stream",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self.server_url, json=payload, headers=headers)
                resp.raise_for_status()
                body = self._decode_body(resp)
        except httpx.HTTPError as exc:
            raise TradingViewMcpError(f"TradingView MCP request failed: {exc}") from exc

        return self._rows_from_response(body)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_body(resp: httpx.Response) -> Dict[str, Any]:
        """Decode a JSON or SSE ``tools/call`` response into a JSON-RPC dict.

        MCP streamable HTTP may answer with ``application/json`` or an
        ``text/event-stream`` carrying one ``data:`` line of JSON. Handle both.
        """
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data = line[len("data:") :].strip()
                    if data:
                        return json.loads(data)
            raise TradingViewMcpError("TradingView MCP SSE response carried no data frame")
        return resp.json()

    def _rows_from_response(self, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract normalized row dicts from a JSON-RPC ``tools/call`` response."""
        if not isinstance(body, dict):
            raise TradingViewMcpError("TradingView MCP response was not a JSON object")

        if body.get("error"):
            err = body["error"]
            message = err.get("message", err) if isinstance(err, dict) else err
            raise TradingViewMcpError(f"TradingView MCP returned an error: {message}")

        result = body.get("result")
        if not isinstance(result, dict):
            raise TradingViewMcpError("TradingView MCP response missing a result object")

        if result.get("isError"):
            raise TradingViewMcpError(
                f"TradingView MCP tool reported an error: {self._error_text(result)}"
            )

        raw_rows = self._extract_raw_rows(result)
        rows: List[Dict[str, Any]] = []
        for item in raw_rows:
            row = self._normalize_row(item)
            if row is not None:
                rows.append(row)
        return rows

    @staticmethod
    def _error_text(result: Dict[str, Any]) -> str:
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text", ""))
        return "unknown error"

    def _extract_raw_rows(self, result: Dict[str, Any]) -> List[Any]:
        """Pull the OHLCV row list out of a tool result (structured or text content)."""
        structured = result.get("structuredContent")
        rows = self._coerce_row_list(structured)
        if rows is not None:
            return rows

        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "TradingView MCP text content was not valid JSON; skipping block"
                    )
                    continue
                rows = self._coerce_row_list(parsed)
                if rows is not None:
                    return rows
        return []

    @staticmethod
    def _coerce_row_list(payload: Any) -> List[Any] | None:
        """Return a row list from ``payload`` (a list, or an object nesting one)."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in _ROWS_CONTAINER_KEYS:
                nested = payload.get(key)
                if isinstance(nested, list):
                    return nested
        return None

    @staticmethod
    def _first(item: Dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in item and item[key] is not None:
                return item[key]
        return None

    @staticmethod
    def _coerce_date(raw_date: Any) -> str | None:
        """Coerce a raw date field to an ``YYYY-MM-DD`` string.

        Accepts an ISO date/datetime string (first 10 chars are taken) or an epoch
        timestamp in seconds or milliseconds. Returns ``None`` when the value can't be
        interpreted as a date, so the row is dropped rather than mis-dated.
        """
        if isinstance(raw_date, bool):
            return None
        if isinstance(raw_date, (int, float)):
            seconds = raw_date / 1000.0 if raw_date >= 1_000_000_000_000 else float(raw_date)
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        text = str(raw_date).strip()
        if not text:
            return None
        # Numeric string epoch (e.g. "1700000000").
        if text.isdigit():
            return TradingViewMcpClient._coerce_date(int(text))
        return text[:10]

    @classmethod
    def _normalize_row(cls, item: Any) -> Dict[str, Any] | None:
        """Map one raw row (dict) to canonical OHLCV keys, or ``None`` if unusable.

        Postconditions: returns a dict with ``date`` (string) and O/H/L/C/volume when
            a date and at least a close are present; ``volume`` defaults to ``0``.
            Rows missing a date or close are dropped (returns ``None``) — a bar we
            can't place in time or price is not usable data.
        """
        if not isinstance(item, dict):
            return None
        raw_date = cls._first(item, _DATE_KEYS)
        close = cls._first(item, _CLOSE_KEYS)
        if raw_date is None or close is None:
            return None
        bar_date = cls._coerce_date(raw_date)
        if bar_date is None:
            return None
        return {
            "date": bar_date,
            "open": cls._first(item, _OPEN_KEYS),
            "high": cls._first(item, _HIGH_KEYS),
            "low": cls._first(item, _LOW_KEYS),
            "close": close,
            "volume": cls._first(item, _VOLUME_KEYS) or 0,
        }
