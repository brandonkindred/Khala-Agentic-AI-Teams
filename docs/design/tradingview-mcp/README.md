# TradingView MCP — Integration Mockups

Static HTML mockups for the TradingView MCP integration flow. Open
[`mockup.html`](./mockup.html) in a browser (no build step — inlined CSS using
the live Khala design tokens: dark theme, amber accent, Inter).

## Requirements covered

1. **From the Integrations page, a user can configure the TradingView MCP.**
   The TradingView card is discoverable in the integrations grid (Screen 1) and
   expands in place to a configuration form — MCP server URL, optional bearer
   token, OHLCV tool name, and an enable toggle (Screen 2).

2. **From the Strategy Lab, a user can jump to Integrations and land with the
   TradingView Configuration panel in focus.** A data-source notice on the
   Strategy Lab (Screen 3) links to `/integrations?focus=tradingview`. Arriving
   there (Screen 4), the page scrolls the TradingView card into view, expands
   it, rings it with the accent glow, and shows a context banner back to the
   Strategy Lab.

## Screens

| # | Screen | Requirement |
|---|--------|-------------|
| 1 | Integrations grid — TradingView card, collapsed | discoverability |
| 2 | TradingView card expanded — MCP config form | Requirement 1 |
| 3 | Strategy Lab — data-source notice with "Configure TradingView" link | entry point |
| 4 | Integrations arrived from lab — TradingView panel in focus | Requirement 2 |

These are visual references only — the backing component (`integrations-dashboard`)
and Strategy Lab already exist; the mockups illustrate the discoverability and
deep-link-to-focus behaviour the requirements ask for.
