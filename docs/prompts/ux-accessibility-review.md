# UX & Accessibility Review Prompt

A reusable prompt for reviewing one team's pages in the Khala Angular frontend
(`user-interface/`). Replace every `<TEAM>` with the team's display name or route
slug (e.g. `job-matching`, `investment`, `agent-studio`) before running it.

The prompt is deliberately repo-aware: the **Context to load first** and
**House constraints** sections carry most of what keeps the output specific to this
codebase instead of a generic WCAG checklist.

Reusing it on a different project takes more than swapping those two, because
repo-specific references are load-bearing elsewhere too. Adapt all of these:

- §1 Scope — the route file (`user-interface/src/app/app.routes.ts`)
- §2 Context to load first — the whole section
- §3 Review lenses — the named shared primitives and helpers (`stall-warning`,
  `defer-focus.ts`, the `--kh-*` tokens, …)
- §7 House constraints — the whole section
- §8 Do not report — the test-harness assumptions (`.a11y.spec.ts`,
  `expectNoAxeViolations`, the SCSS contrast guard)

What genuinely ports unchanged is the method: the five lenses, the state-disposition
model, the finding format and severity rubric, and the noise-control rules.

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
    `breadcrumb`, plus the helpers `destructive-action.helper.ts`
    (`DestructiveActionHelper` — the confirm → re-entrancy guard → API call →
    error/toast orchestration behind the agent-runner, strategy-lab, load-draft, and
    run-history destructive actions; the target for any hand-rolled destructive flow),
    `defer-focus.ts` (move focus after a re-render),
    `extract-error-detail.ts` (`extractErrorDetail` — the repo-wide way to pull a
    human-readable message out of an HTTP error for an inline error field, used by
    ~70 call sites; recommend it rather than hand-rolled `err?.error?.detail ?? …`
    chains when proposing error copy),
    `result-count-announcement.ts` (live-region text for filtered lists), and
    `poll-while.ts` / `staleness.util.ts` (long-running job polling and staleness).
    "Build a custom X" when a shared X exists is itself a finding.
  - Existing `*.a11y.spec.ts` files and `user-interface/src/app/testing/a11y.ts`
    (`expectNoAxeViolations`, with `color-contrast` disabled under jsdom). Coverage is
    narrower than it looks, in three independent ways, and ALL THREE must hold before
    you treat anything as guarded:
      * By state — a spec guards only the states its fixtures render. One that mounts
        empty listings, jobs, and runs audits the empty state and nothing else, so the
        populated, active-job, and error branches are unaudited even for defects axe
        could catch.
      * By assertion — the specs vary, so read the one you intend to rely on rather
        than assuming either way. Many are bare `expectNoAxeViolations` smoke tests;
        others (`strategy-lab`, `strategy-card`, `paper-trading-panel`) additionally
        assert accessible names and icon ARIA, and those assertions DO guard the
        behaviour they name. A bare axe call guards only what axe checks in jsdom,
        which excludes colour contrast (disabled outright in `axeOptions`; the SCSS
        guard and browser axe cover it instead) and everything interaction-dependent:
        focus restoration after a mutation, live-region announcements, keyboard
        sequences, reflow at 320 px and 200%, and target size or spacing.
      * By scope — `expectNoAxeViolations` runs `axe(host, …)` on the element it is
        given, and the specs pass a single component instance (`fixture.nativeElement`)
        or a sub-element. Nothing outside that subtree is audited: sibling components,
        a second instance of the same component, and the page shell are all absent. So
        a defect that exists only in the routed composition is unguarded even when the
        component's own spec renders the same state — duplicate ARIA ids across
        repeated instances, landmark and heading structure across the page, and
        `aria-labelledby` pointing at an id outside the component. Before excluding a
        composition-level finding, confirm a fixture actually renders that composition
        and that multiplicity.
    Read each spec's fixtures, its assertions, AND what element it audits before
    excluding a finding, then spend your attention on the unrendered states, the
    unrendered compositions, and what axe structurally cannot see.

## 3. Review lenses

Work all five. Under each, the listed checks are the floor, not the ceiling.

A. ACCESSIBILITY (WCAG 2.2 AA)
   - Semantics: landmark and heading structure, heading level order, lists/tables as
     real lists/tables, `role` only where native elements can't do the job.
   - Names and descriptions: every control has an accessible name that CONTAINS its
     visible label (2.5.3 Label in Name is a containment rule, not an equality one).
     Supplementing the visible text with disambiguating context is correct and often
     better — a visible "Cancel" named "Cancel <job label>" is compliant, and reporting
     it as a mismatch is a false positive. Flag only when the visible label is absent
     from or reordered within the accessible name, which is what actually breaks
     speech-input users. Icon-only buttons carry `[attr.aria-label]`; form fields are
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
   - Zoom and reflow: usable at 200% zoom and 320 CSS px wide without clipped controls
     and without horizontal scrolling — except where 1.4.10 exempts it, for the parts
     of the content that genuinely require a two-dimensional layout for meaning or use
     (data tables, task graphs, Mermaid diagrams). Test whether the two-dimensional
     layout is actually necessary before reporting horizontal scroll: a wide data table
     scrolling inside its own container is compliant, a paragraph doing so is not.
     Text-spacing overrides don't break layout.
   - Motion and timing, at the A/AA bar this review holds to, and only where the
     criterion actually applies. These summaries are compressed; before reporting any
     motion or timing finding, re-read the criterion in
     `backend/agents/accessibility_audit_team/wcag_criteria.py` and confirm its
     applicability conditions and exceptions hold — a compressed restatement is a
     starting point, not the standard.
       * 2.2.1 Timing Adjustable (A) — judge a time limit by its mechanism, not by
         whether the duration feels generous: a comfortable but fixed limit still
         fails. Passing takes ONE of three, and the thresholds are part of the
         criterion, so a token control does not satisfy it:
           - the user can turn the limit off before encountering it; or
           - the user can adjust it, BEFORE ENCOUNTERING IT, over a range AT LEAST
             TEN TIMES the default — a twofold adjustment does not qualify, and
             neither does a tenfold one offered only once the limit is already
             running; or
           - the user is warned before it expires, given AT LEAST 20 SECONDS to extend
             with a simple action, and can extend AT LEAST TEN TIMES.
         The criterion's own exceptions (real-time events, essential limits, and
         limits beyond twenty hours) apply.
       * 2.2.2 Pause, Stop, Hide (A) — covers moving, blinking, or scrolling content
         that starts automatically, lasts MORE THAN FIVE SECONDS, and is presented in
         parallel with other content; and auto-updating content that starts
         automatically and is presented in parallel. Only then is a mechanism
         required, and the two branches accept different ones: pause, stop, or hide
         for moving/blinking/scrolling content; pause, stop, hide, OR a control over
         the update frequency for auto-updating content — so a feed whose refresh
         interval the user can change is compliant. BOTH branches are excepted where
         the movement or the auto-updating is part of an activity in which it is
         essential. A short transition, or a progress display that is the only thing
         on screen, is not a 2.2.2 failure.
       * 2.3.1 is satisfied EITHER by flashing no more than three times in any one
         second OR by staying below the general-flash and red-flash thresholds — a
         faster but sub-threshold effect is compliant, so check the threshold before
         reporting.
     Honouring
     `prefers-reduced-motion` is 2.3.3, which is AAA — worth recommending as an
     enhancement on a decorative entrance animation, but never reported as an AA
     failure merely because the media query is absent.
   - WCAG 2.2 additions specifically:
       * Target size (2.5.8) is a size-OR-spacing rule, not a flat floor: a target
         passes at 24×24 CSS px, or under one of the criterion's own carve-outs. Check
         these before flagging, and say which one you checked — naming an exception is
         not the same as meeting it:
           - Spacing — undersized targets positioned so a 24 px circle centred on each
             intersects NEITHER another target (its actual bounding box, which is how
             a full-size neighbour counts) NOR the circle around another undersized
             target. Checking circle-to-circle alone wrongly exempts a small control
             sitting against a full-size one.
           - Inline — the target sits IN A SENTENCE, or its size is constrained by the
             line-height of surrounding non-target text. A control merely styled
             inline does not qualify: a compact toolbar action still owes 24×24 or the
             spacing test.
           - Equivalent — the same function is reachable from another control on the
             same page that does meet the size.
           - User-agent control — the size is set by the user agent and unmodified by
             the author.
           - Essential — a particular presentation is essential or legally required.
       * Focus not obscured: at the AA bar this review holds to (2.4.11), a focused
         control must not be ENTIRELY hidden by AUTHOR-CREATED content — a sticky
         header, toolbar, or footer being the usual culprits. Content the USER opened
         is carved out: if they can reveal the focused control without advancing
         focus, such as pressing Escape to dismiss an overlay they opened, it does not
         count as hidden, so check for that before reporting. Partial obscuration
         fails only the AAA criterion (2.4.12) — report it, if at all, as an
         enhancement, never as an AA violation.
       * Dragging movements (2.5.7) have a single-pointer alternative that does NOT
         itself require dragging — a second drag gesture does not satisfy this, since
         dragging is already single-pointer. Essential dragging, and behaviour set by
         the user agent and not modified by the author, are excepted.
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
   For each page, walk all of: first visit; empty; first load; refresh; partial;
   populated; long-running; stalled; failed; permission-denied; backend-unconfigured;
   offline; API-error; and stale-data. Each is its own state, and no disposition
   covers two of them: first visit and empty differ (onboarding a new user is not the
   same as giving a returning user with zero or filtered-out results a way forward),
   and first load and refresh differ (a refresh that silently blanks already-rendered
   content is a defect that a correct first-load spinner hides).
   The five failure states are a PARTITION, not overlapping labels — classify each
   observed failure into exactly one, in this order, so the same event does not appear
   in two rows:
     - offline — no transport at all; the request never reached a server.
     - permission-denied — the server answered, refusing on identity or authorization
       (401/403).
     - backend-unconfigured — the server answered, reporting the feature is not set up
       (missing provider, credential, or configuration) rather than refusing the user.
     - failed — the request SUCCEEDED and the domain operation or job it started then
       reached a terminal failed state. Work failed, not the call.
     - API-error — the residual: any other request failure (5xx, timeout, malformed or
       unparseable response). Reach for this only when none of the four above fits.
   Give every state exactly one disposition, and state which:
     - HANDLED — the UI is (1) rendered at all; (2) recoverable from, where the state
       represents a failure or interruption (partial, failed, stalled, stale-data,
       permission-denied, backend-unconfigured, offline, API-error) — a stated way
       forward rather than a dead end, so partial results after an interrupted request
       do not earn HANDLED merely by rendering. A successful state has nothing to
       recover from, so do not withhold HANDLED from a correct populated or empty view
       on this ground; and (3) where the state arrives dynamically — without navigation
       or focus movement — announced to assistive tech. An initially rendered state
       needs correct semantics, not a live region: the house standard reserves
       `aria-live` for dynamic content and status changes, so neither report a
       compliant initial render as missing an announcement nor recommend adding one.
     - MISSING — a finding. Evidence for an absence is not the same shape as evidence
       for a presence, so either form counts: cite the branch that handles the state
       incorrectly, OR — where the state is wholly unimplemented and no such branch
       exists — cite the nearest operation or conditional where the handling would
       belong, say what is absent there, and back it with the scoped search that
       found nothing handling it. Never invent a line number to satisfy the format.
     - NOT APPLICABLE — the page does not own this state. Evidence is required, not
       assertion, and either form counts: name the code that hands the state elsewhere
       (a landing page redirecting to a dashboard as soon as work exists owns neither
       the populated nor the long-running state), OR show from the page's operations
       and branches that nothing there can enter the state at all (a viewer whose only
       call is a single fetch with success and error branches has no long-running or
       stalled state to own — there is no handoff to point at, and demanding one would
       force exactly the fabricated finding forbidden below). An N/A asserted without
       either form of evidence is itself a gap.
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
  - **Top 5** — up to five, one line each, highest leverage first. Fewer is correct
    when fewer survive verification, and none is a valid result; never pad this list
    by duplicating or inventing findings to reach five.
  - **State dispositions** — one table per routed page, a row per state, covering all
    fourteen from §3D. Columns: state | HANDLED / MISSING / N/A | evidence (the
    `file:line` of the handled branch; for MISSING, either the incorrect branch or
    the nearest operation where handling would belong plus what is absent; for N/A,
    the justification).
    This is the audit the definition of done requires; a page whose table omits a
    state is not finished. Each MISSING row is also a finding below.

Then every finding, in ranked order:

  ### <ID: A11Y-n | FLOW-n | CLARITY-n | STATE-n | SYSTEM-n> — <short title>
  - **Severity**: Blocker | High | Medium | Low
  - **Location**: `path/to/file.html:120-134` (+ other locations for the same cause)
  - **What the user hits**: a concrete scenario naming the user and their tool
    ("a screen-reader user tabs to Cancel Scan; after the row is removed, focus is on
    body and nothing is announced")
  - **Why it's a problem**: the WCAG success criterion (number and name) or the UX
    principle. Do not cite a criterion you have not checked against.
  - **Recommended change**: specific enough to implement — name the concrete mechanism,
    whichever fits the finding: an element, attribute, token, or shared primitive; the
    replacement copy; or the component-logic change (remembering known input,
    stabilizing sort order across polls). Include a code sketch when the change is
    subtle. Do not invent an irrelevant token or attribute to satisfy the format.
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
  - Design by Contract applies to any code you propose, per the repo-wide mandate in
    `CLAUDE.md` ("mandatory for all code and comments"): preconditions, postconditions,
    and invariants documented in the docstring, enforced rather than silently coerced.
    The narrower "public APIs" phrasing in `CONTRIBUTORS.md` describes what the
    software-engineering team enforces in generated code, and does not relax the
    repo-wide rule.
  - Any behavior change needs test coverage (90% line-coverage floor). Prefer extending
    the existing `.a11y.spec.ts` for the component over writing a new harness.
  - Never reference an external issue tracker in code, comments, or docs.

## 8. Do not report

  - Findings that an existing `.a11y.spec.ts` guards with an assertion covering THAT
    BEHAVIOUR in THAT STATE. Both halves are required: a state the fixtures never
    render is not guarded, and neither is a behaviour the spec never asserts. A bare
    `expectNoAxeViolations` in the populated state does not cover a focus-management,
    announcement, reflow, target-size, or contrast defect in that state, nor keyboard
    behaviour axe cannot exercise — key sequences, focus movement and restoration,
    traps. Report those. It DOES run axe's DOM-only rules — positive `tabindex` and
    the rest of the default set that depends on attributes and structure alone — so do
    not re-report those in a state the spec renders. But rules needing rendered
    geometry do not fire under jsdom, which implements no layout: `color-contrast` is
    disabled outright for that reason, and `scrollable-region-focusable` silently
    matches nothing because `scrollHeight` and `clientHeight` are both 0, yielding
    neither a violation nor an incomplete. An unfocusable scrollable region is
    therefore NOT guarded by a jsdom spec — treat it as manual/source review.
  - Speculation that requires a running browser without labelling it as such.
  - Wholesale redesigns, restructures of the shared shell, or framework swaps.
  - Style preferences with no accessibility, comprehension, or step-count consequence.
  - The same root cause restated once per page.

## 9. Definition of done

You are finished when: every routed <TEAM> page carries a stated disposition —
handled, missing, or evidenced not-applicable — for all fourteen states in §3D; every
finding cites a file and line, or — for an absence — the nearest relevant location
plus what is missing there, or is explicitly marked as needing a browser; every
recommendation names a concrete mechanism — an element, attribute, token, shared
primitive, copy change, or component-logic change — rather than
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
