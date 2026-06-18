# Coding Team page — status panel redesign

> **Update (later iteration):** the two-column layout shown in these mockups proved too dense in use.
> The shipped design instead splits the page into **three single-focus views** selected by a toggle —
> **Chat** (the assistant), **GitHub** (the issues list), and **Jobs** (an accordion list of runs
> where selecting a run expands its progress inline). The mockups below capture the earlier
> two-column concept and the run states/components, which still inform the Jobs view; treat the
> three-view split as the current layout.

These notes capture the work of separating the Coding Team live status from the GitHub Issues list,
so the issues list is **always visible and selectable**, runs live in their **own persistent
status surface** that handles multiple runs, and a run can **never be dismissed into a dead end**.
The shipped implementation delivers that intent through a **three-view toggle** (see below); the
mockups predate the toggle and illustrate the earlier two-column arrangement of the same content.

The mockups use the app's real design tokens (`user-interface/src/theme.scss` — premium dark +
amber). They render directly in GitHub and any browser.

| # | Mockup | What it shows | Status |
|---|--------|---------------|--------|
| 1 | [`redesign-running.svg`](./redesign-running.svg) | The earlier **two-column** page in the running state: issues on the left (inline confirm under the selected row), the Runs panel on the right (Running + Recent lists + live detail). | **Historical** — superseded by the three-view layout; the content (issue rows, run rows, run detail) still matches, only the arrangement changed. |
| 2 | [`runs-panel-states.svg`](./runs-panel-states.svg) | The Runs panel in three states: **empty**, **waiting for answers** (pending-questions reachable), and **completed** (with a *Run again* affordance). | **Current** — these are exactly the **Jobs** view's run states. |
| 3 | [`before-after.svg`](./before-after.svg) | The fix at a glance: *before*, a run hid every issue and Dismiss was a dead end; *after*, issues stay visible and runs live in their own non-dismissable surface. | **Historical** — the *after* frame shows the two-column arrangement; the principle (issues never hidden, no Dismiss dead-end) still holds in the Jobs view. |
| — | [`mockup.html`](./mockup.html) | An interactive HTML version of frame 1 (open in a browser) using the real `--kh-*` CSS variables. | **Historical** — two-column. |

## Current layout — three single-focus views

The page opens on **Chat** and switches between three views via a `mat-button-toggle-group` (the
`.view-toggle`); only one view's content renders at a time. The header (back link, title, subtitle,
health indicator) and the runs/issues polling are shared and view-independent, so chips and progress
stay live even while a view is hidden.

- **Chat** — the `app-team-assistant-chat` component (assistant chat + the "Coding Team" form) plus
  the queued-job banner. The default view.
- **GitHub** — the full-width issues list. Selecting an issue expands an **inline confirm** beneath
  the row (it never replaces the list); starting a run marks the issue **In progress**.
- **Jobs** — an **accordion** of runs (*Running* + *Recent*). Clicking a row expands that run's live
  detail inline beneath it; clicking again collapses it. A run paused on questions shows a *needs
  answers* badge and is always reachable. There is **no Dismiss button**.

Cross-cutting, vs. the legacy page:

- **Re-themed** from legacy light Material colors to the `--kh-*` dark + amber tokens; reuses the
  shared `.kh-badge` / `.kh-empty-state` system.
- **Per-row bindings are precomputed** into view-models (`RunRowVm` / `IssueRowVm`) so list rows
  bind plain properties instead of calling helper methods every change-detection cycle.
- **Usability extras:** copy-job-id, *Run again* on terminal runs, tooltips on truncated text,
  a collapsed-by-default thinking panel, and a paginator range label.

### Earlier two-column concept (historical)

The first iteration placed the same content in a **two-column layout**
(`grid-template-columns: 1fr 380px`, stacking below 1024px): issues left, a persistent Runs panel
right. It proved too dense in use, which is why the shipped design splits the content across the
three views above. The two-column mockups (frames 1 and 3, `mockup.html`) are retained only for that
historical context.

## Figma

The live Figma file was not generated in this session: the authenticated Figma seat is **View-only**
(`seat_type: "view"`) and `create_new_file` is gated behind an interactive approval, so the write
could not complete unattended. The SVG/HTML mockups above are the canonical visual reference.

To generate the Figma file with edit access, run `mcp__Figma__create_new_file` (design editor), then
`mcp__Figma__use_figma` with the script below to build Frame 1 from auto-layout, using
`redesign-running.svg` as the visual target:

```text
Build top-down with auto-layout:
- Page frame: VERTICAL auto-layout, fill #000000, padding 32, gap 16.
- Header: text nodes — "Coding Team" (Inter Bold 28, #ffffff) + subtitle (Inter 13, #a1a1aa);
  a health pill (auto-layout, radius 9999, fill #171717) top-right.
- Row: HORIZONTAL auto-layout, gap 30, align-items MIN, holding two cards:
    • Issues card  → layoutGrow 1
    • Runs card    → fixed width 380
  Each card: VERTICAL auto-layout, fill #171717, stroke rgba(255,255,255,0.16), cornerRadius 12,
  padding 20, gap 12.
- Issue rows / run rows: HORIZONTAL auto-layout, layoutAlign STRETCH.
- Badges: auto-layout pills, cornerRadius 9999, semantic subtle fills
  (running rgba(251,191,36,.20)/#fde68a, completed rgba(74,222,128,.18)/#4ade80,
   failed rgba(248,113,113,.18)/#f87171, warning rgba(251,191,36,.18)/#fbbf24).
- Selected run: amber left bar (#fbbf24) + fill #262626.
Note: Inter weight is "Semi Bold" (with the space). Do not set figma.currentPage.
```

## Token reference (`src/theme.scss`)

surfaces `#000 / #0a0a0a / #171717 / #262626 / #333` · accent `#fbbf24` / text-accent `#fde68a` ·
text `#ffffff / #d4d4d8 / #a1a1aa / #909099` · success `#4ade80` · warning `#fbbf24` ·
error `#f87171` · info `#93c5fd` · radius 8/12/9999 · font Inter / JetBrains Mono.
