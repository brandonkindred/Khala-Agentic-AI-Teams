# Coding Team page — status panel redesign

> **Update (later iteration):** the two-column layout shown in these mockups proved too dense in use.
> The shipped design instead splits the page into **three single-focus views** selected by a toggle —
> **Chat** (the assistant), **GitHub** (the issues list), and **Jobs** (an accordion list of runs
> where selecting a run expands its progress inline). The mockups below capture the earlier
> two-column concept and the run states/components, which still inform the Jobs view; treat the
> three-view split as the current layout.

Design mockups for separating the Coding Team live status panel from the GitHub Issues list, so the
issues list is **always visible and selectable**, the status panel becomes its **own persistent
panel** that handles multiple runs, and **cannot be dismissed**.

These mockups use the app's real design tokens (`user-interface/src/theme.scss` — premium dark +
amber). They render directly in GitHub and any browser.

| # | Mockup | What it shows |
|---|--------|---------------|
| 1 | [`redesign-running.svg`](./redesign-running.svg) | The full two-column page in the running state: issues always on the left (with an inline confirm under the selected row), the persistent Runs panel on the right (Running + Recent lists + live detail of the selected run). |
| 2 | [`runs-panel-states.svg`](./runs-panel-states.svg) | The Runs panel in three states: **empty**, **waiting for answers** (pending-questions reachable), and **completed** (with a *Run again* affordance). |
| 3 | [`before-after.svg`](./before-after.svg) | The fix at a glance: *before*, a run hid every issue and Dismiss was a dead end; *after*, issues stay visible and runs live in their own non-dismissable panel. |
| — | [`mockup.html`](./mockup.html) | An interactive HTML version of frame 1 (open in a browser) using the real `--kh-*` CSS variables. |

## What changed vs. the old page

- **Two-column layout** (`grid-template-columns: 1fr 380px`, stacks below 1024px). Issues left,
  Runs right.
- **Issues list is always rendered** once loaded — it is no longer gated on "no active job".
  Selecting an issue expands an **inline confirm** beneath the row instead of replacing the list.
- **Runs panel is persistent** — a *Running* + *Recent* list (selecting a run shows its live
  detail) with **no Dismiss button**. A run paused on questions is pinned in *Running* with a
  *needs answers* badge, so it can always be reached and answered.
- **Re-themed** from legacy light Material colors to the `--kh-*` dark + amber tokens; reuses the
  shared `.kh-badge` / `.kh-empty-state` system.
- **Usability extras:** copy-job-id, *Run again* on terminal runs, tooltips on truncated text,
  a panel legend, collapsed-by-default thinking panel, and a paginator range label.

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
