# UX & Accessibility Review Prompt

A reusable prompt for reviewing one team's pages in the Khala Angular frontend
(`user-interface/`). Replace every `<TEAM>` with the team's display name or route
slug (e.g. `job-matching`, `investment`, `agent-studio`) before running it.

The prompt is deliberately repo-aware: the **Context to load first** and
**House constraints** sections are what keep the output specific to this codebase
instead of a generic WCAG checklist. Swap those two sections when reusing the
prompt on a different project; the rest is portable.

---

## Prompt

```text
role: senior UI/UX designer
specialty: accessibility (WCAG 2.2 AA) and operator-facing SaaS applications
audience: the engineers who own the <TEAM> pages — they act on your findings directly,
  so every finding must be specific enough to implement without a follow-up conversation

objective: review the <TEAM> pages and return a prioritized, evidence-backed set of
  changes that
    (a) remove accessibility barriers for keyboard, screen-reader, low-vision, and
        cognitive-load-sensitive users,
    (b) reduce the number of steps, decisions, and waiting states a task costs, and
    (c) make each page's purpose, current state, and next action obvious on first
        encounter.
  Findings, not a redesign. Prefer the smallest change that removes the barrier.

## 1. Scope

In scope:
  - The routed pages for <TEAM> in `user-interface/src/app/app.routes.ts`, and every
    component they compose (templates, SCSS, and the component classes that drive
    conditional rendering and focus).
  - The end-to-end task a user comes to these pages to accomplish — starting from the
    nav entry point, not from the component boundary. Cross-page seams (nav → form →
    running job → results → re-run) are in scope even when each page is fine alone.
  - Shared primitives ONLY where <TEAM> uses them in a way that creates the problem.

Out of scope (note it and move on, do not fix):
  - Backend APIs, agent behavior, and response shapes — unless a UI barrier is
    unfixable without an API change, in which case state exactly what the API must
    return.
  - Global nav, theming, and shared components as a whole. A defect in a shared
    primitive is a single finding filed against that primitive, not repeated per page.
  - Visual taste. "I'd have used a different accent" is not a finding.

## 2. Context to load first (do not skip — findings that ignore this are noise)

  - `user-interface/docs/ACCESSIBILITY.md` — the house standard. Cite the rule you are
    invoking, and flag it explicitly if you believe the standard itself is wrong or
    silent on the case.
  - `user-interface/src/theme.scss` — the `--kh-*` token set (surface, text, border,
    accent, focus-ring, semantic success/warning/error/info, spacing, radius, type
    scale). Recommendations must name tokens, never raw hex.
  - `user-interface/src/styles/scss-contrast-guard.spec.ts` — the static guard against
    hardcoded low-contrast text and suppressed focus outlines. Its allowlist is a
    burndown list: if a <TEAM> file is on it, that is a known debt, and clearing it is
    a legitimate finding.
  - `user-interface/src/app/shared/` — existing primitives to reuse before inventing
    anything: `dashboard-shell`, `empty-state`, `error-message`, `inline-banner`,
    `loading-spinner`, `stall-warning`, `confirm-dialog` / `confirm-destructive.service`,
    `breadcrumb`, plus the helpers `defer-focus.ts` (move focus after a re-render),
    `result-count-announcement.ts` (live-region text for filtered lists), and
    `poll-while.ts` / `staleness.util.ts` (long-running job polling and staleness).
    "Build a custom X" when a shared X exists is itself a finding.
  - Existing `*.a11y.spec.ts` files and `user-interface/src/app/testing/a11y.ts`
    (`expectNoAxeViolations`, with `color-contrast` disabled under jsdom). Anything
    axe already covers for <TEAM> is regression-guarded — spend your attention on what
    axe structurally cannot see.

## 3. Review lenses

Work all five. Under each, the listed checks are the floor, not the ceiling.

A. ACCESSIBILITY (WCAG 2.2 AA)
   - Semantics: landmark and heading structure, heading level order, lists/tables as
     real lists/tables, `role` only where native elements can't do the job.
   - Names and descriptions: every control has an accessible name that matches its
     visible label; icon-only buttons carry `[attr.aria-label]`; form fields are
     programmatically associated with labels, hints, and errors.
   - Keyboard: every interaction reachable and operable without a pointer; no traps;
     tab order matches visual order; no positive `tabindex`; custom widgets implement
     the expected key bindings; visible focus ring survives (`--kh-focus-ring`).
   - Focus management: focus moves deliberately when content is destroyed or replaced
     (row triaged, job cancelled, dialog opened/closed, route changed) — see
     `defer-focus.ts`. Focus must never land on `<body>` after a user action.
   - Live regions: async results, validation errors, job-status transitions, and
     filtered result counts are announced once, at the right politeness, without
     spamming on every poll tick.
   - Contrast and non-color signalling: text and UI-component contrast against the
     actual `--kh-surface-*` it sits on; status is never conveyed by hue alone (chips,
     graph edges, diff highlights, severity dots need text or shape too).
   - Zoom and reflow: usable at 200% zoom and 320 CSS px wide without horizontal
     scrolling or clipped controls; text-spacing overrides don't break layout.
   - Motion and timing: animation respects `prefers-reduced-motion`; nothing
     auto-advances or auto-dismisses faster than a user can read it.
   - WCAG 2.2 additions specifically:
       * Target size (2.5.8) is a size-OR-spacing rule, not a flat floor: a target
         passes at 24×24 CSS px or with sufficient spacing, and the inline, essential,
         equivalent, and user-agent-control exceptions apply. Do not report a small but
         well-separated or inline target as a violation — check spacing and exceptions
         before flagging, and say which one you checked.
       * Focus not obscured by sticky headers, toolbars, or footers.
       * Dragging movements have a single-pointer alternative.
       * Redundant entry, and consistent help placement.

B. INTERACTION COST
   - Count the clicks, keystrokes, page transitions, and decisions in the primary task.
     Name the number, then name the number your change achieves.
   - Required input that the app already knows, could infer, or could remember.
   - Modal or navigational detours where inline editing would do; conversely, dense
     inline forms that should be progressively disclosed.
   - Destructive and expensive actions: confirmation proportional to cost, undo where
     it is cheaper than confirmation, and no confirmation theatre on reversible things.
   - Long-running work: can the user leave and come back? Is progress legible and
     honest? Is there a cancel? Does a stalled job say so (`stall-warning`) rather than
     spinning forever?

C. CLARITY AND INFORMATION ARCHITECTURE
   - Does the page state its purpose above the fold, in the user's vocabulary rather
     than the agent pipeline's?
   - Is the primary action visually and structurally primary, and is there exactly one?
   - Labels, empty states, and errors: does each say what happened, why, and what to do
     next? Replace dead ends ("No data") with a next action.
   - Terminology consistency with the rest of the product and with the API's own nouns
     (job vs run vs scan vs task) — inconsistency here is a real cognitive cost.
   - Scannability of dense output: grouping, truncation with a way to see the whole,
     sensible defaults for sort and filter, stable ordering across polls.

D. STATE COVERAGE
   For each page, walk all of: first visit / empty, loading (first load vs refresh),
   partial, populated, long-running, stalled, failed, permission-denied or
   backend-unconfigured, offline or API-error, and stale-data.
   Give every state exactly one disposition, and state which:
     - HANDLED — the UI is (1) rendered at all, (2) announced to assistive tech, and
       (3) recoverable from.
     - MISSING — a finding. Cite the branch in the template or component that proves
       it is missing.
     - NOT APPLICABLE — the page does not own this state. Evidence is required, not
       assertion: name the code that hands the state elsewhere (a landing page that
       redirects to a dashboard as soon as work exists owns neither the populated nor
       the long-running state). An unevidenced N/A is itself a gap.
   Never manufacture a finding to fill a state the page does not own — a page with
   fewer owned states is not a page with more defects.

E. CONSISTENCY WITH THE DESIGN SYSTEM
   - Hardcoded colors, spacing, radii, or font sizes where a `--kh-*` token exists.
   - Locally reimplemented shared primitives.
   - Divergence from how sibling team pages solve the same problem — and, where <TEAM>
     is the better pattern, say so, because that is a candidate for promotion.

## 4. Method

  1. Read the routes and enumerate the <TEAM> pages. State the list you are reviewing
     before you review it, so an omission is visible.
  2. Load the context in §2.
  3. Reconstruct the primary user task end-to-end from the templates, component logic,
     and the service calls they make. Write it out as a numbered walkthrough with the
     step count.
  4. Run the five lenses over each page and over the seams between pages.
  5. For each candidate finding, verify it against the source before reporting it: name
     the file and line that proves it. If you cannot point at the code, either the
     finding is wrong or it needs a running browser — say which.
  6. Deduplicate: collapse the same root cause across pages into one finding with
     multiple locations.
  7. Rank by (user impact × frequency) ÷ effort. Report in that order.

## 5. Output format

Open with:
  - **Pages reviewed** — the list from step 1.
  - **Primary task walkthrough** — the numbered steps and the current step count.
  - **Top 5** — one line each, highest leverage first.

Then every finding, in ranked order:

  ### <ID: A11Y-n | FLOW-n | CLARITY-n | STATE-n | SYSTEM-n> — <short title>
  - **Severity**: Blocker | High | Medium | Low
  - **Location**: `path/to/file.html:120-134` (+ other locations for the same cause)
  - **What the user hits**: a concrete scenario naming the user and their tool
    ("a screen-reader user tabs to Cancel Scan; after the row is removed, focus is on
    body and nothing is announced")
  - **Why it's a problem**: the WCAG success criterion (number and name) or the UX
    principle. Do not cite a criterion you have not checked against.
  - **Recommended change**: specific enough to implement — the element, attribute,
    token, or shared primitive to use. Include a code sketch when the change is subtle.
  - **Cost**: S / M / L, plus blast radius (this page | this team | shared primitive).
  - **Verification**: how a reviewer proves it fixed — the `.a11y.spec.ts` assertion to
    add, the contrast-guard allowlist entry to remove, or the manual keyboard /
    screen-reader / 200%-zoom walkthrough to run.

Close with:
  - **Open questions** — anything that needs a product decision or a running browser.
  - **Out of scope, noted** — backend or shared-primitive issues you deliberately
    did not pursue.

## 6. Severity rubric

  - **Blocker** — a user of an assistive technology cannot complete the primary task
    at all (keyboard trap, unlabeled required control, unreachable action), or data
    loss is possible.
  - **High** — the task is completable but materially harder or error-prone: silent
    async failure, focus loss on every mutation, contrast below AA on primary content,
    a destructive action without confirmation or undo.
  - **Medium** — friction, ambiguity, or an inconsistency that costs comprehension:
    dead-end empty state, unlabeled icon in a secondary control, unnecessary step.
  - **Low** — polish, token drift, wording. Real but deferrable.

## 7. House constraints

  - No new dependencies. Angular 19 standalone components, SCSS, existing `--kh-*`
    tokens, existing shared primitives.
  - ARIA attributes in templates use the `[attr.aria-*]` form.
  - Native semantics before ARIA; ARIA only where no native element does the job.
  - Design by Contract applies to any code you propose: preconditions, postconditions,
    and invariants documented in the docstring, enforced rather than silently coerced.
  - Any behavior change needs test coverage (90% line-coverage floor). Prefer extending
    the existing `.a11y.spec.ts` for the component over writing a new harness.
  - Never reference an external issue tracker in code, comments, or docs.

## 8. Do not report

  - Findings axe already catches and an existing `.a11y.spec.ts` already guards.
  - Speculation that requires a running browser without labelling it as such.
  - Wholesale redesigns, restructures of the shared shell, or framework swaps.
  - Style preferences with no accessibility, comprehension, or step-count consequence.
  - The same root cause restated once per page.

## 9. Definition of done

You are finished when: every routed <TEAM> page carries a stated disposition —
handled, missing, or evidenced not-applicable — for all ten states in §3D; every
finding cites a file and line or is explicitly marked as needing a
browser; every recommendation names a token, primitive, or attribute rather than
describing an intention; and the top 5 are ordered such that shipping only those
removes the largest share of user pain.
```

---

## Usage notes

- **One team per run.** The prompt trades breadth for specificity; running it across
  all 23 teams at once collapses it into a generic checklist.
- **Browser-dependent checks** — real contrast against composited surfaces, 200% zoom
  reflow, and actual screen-reader output — cannot be settled from source alone. The
  prompt requires those to be labelled rather than guessed, so the labelled set is the
  manual QA list for the review.
- **Feeding results back**: findings scoped to a shared primitive
  (`user-interface/src/app/shared/`) are the highest-leverage output, since one fix
  clears the same defect across every team's pages.
