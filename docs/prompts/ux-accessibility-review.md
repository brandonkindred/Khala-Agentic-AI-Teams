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
  `defer-focus.ts`, the `--kh-*` tokens, …), and §3A's paragraph on
  `backend/agents/accessibility_audit_team/wcag_criteria.py`, a Khala backend file
  that does not exist elsewhere — drop it and send the reviewer straight to the
  specification
- §7 House constraints — the whole section
- §5 Output format — the Verification bullet defers to §7's spec-selection rule, and
  the state table is pinned to the fourteen states of §3D
- §8 Do not report — the `expectNoAxeViolations` harness assumptions (the SCSS
  contrast guard is a §2 item, covered by adapting that section whole)

What genuinely ports unchanged is the method: the five lenses, the state-disposition
model, the SHAPE of the finding format and its severity rubric, and the noise-control
rules — everything except the repo-specific lines listed above.

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
  - Global nav and theming as a whole.
  - Visual taste. "I'd have used a different accent" is not a finding.

Shared primitives are the one case that crosses the line, so route them explicitly. A
defect <TEAM>'s usage creates is IN scope and filed normally. A defect internal to the
primitive itself is still a FINDING, not a note-and-move-on: file it ONCE against the
primitive with `shared primitive` as its blast radius, however many <TEAM> pages it
reaches, because one fix clears it across every team. Reserve "Out of scope, noted"
for primitive defects you deliberately chose not to pursue at all.

## 2. Context to load first (do not skip — findings that ignore this are noise)

  - `user-interface/docs/ACCESSIBILITY.md` — the house standard. Cite the rule you are
    invoking, and flag it explicitly if you believe the standard itself is wrong or
    silent on the case.
  - `user-interface/src/theme.scss` — the `--kh-*` token set (surface, text, border,
    accent, focus-ring, semantic success/warning/error/info, spacing, radius, type
    scale). Recommendations must name tokens, never raw hex. Watch one prefix
    collision: `--kh-text-*` covers BOTH colours (`-primary`, `-secondary`,
    `-tertiary`, `-muted`, `-on-accent`) and the type scale (`-xs` … `-2xl`), so
    `--kh-text-lg` is a font size, not a colour. Naming a size token in a colour
    recommendation produces an invalid declaration.
  - `user-interface/src/styles/scss-contrast-guard.spec.ts` — a regex lint over
    `src/app/**/*.scss` for a shortlist of low-contrast text colours and for
    suppressed focus outlines. Read what it actually bans before crediting it with
    anything: NOT hex literals as a class, but four families. (a) A repeated-digit
    PREFIX `#111`–`#777` or `#888`–`#eee` — anchored on the first three digits only,
    so `#777fff` and `#eee000` are banned too even though neither is a grey; do not
    read the ban as "greys". (b) Four named literals (`#484f58`, `#6e7681`,
    `#71717a`, `#8b949e`). (c) Opaque-black `rgb()`/`rgba()`. (d) Those same values
    appearing as a `var()` FALLBACK — `color: var(--kh-text-secondary, #6e7681)` is
    banned, which matters because `var(--token, #hex)` is this codebase's dominant
    idiom, so a remediation you propose in that form can fail CI. Everything else
    passes, including `color: #000`. Token drift is therefore NOT a solved problem and lens E has real
    work — around 90 raw-hex `color:` declarations across some 28 distinct values sit
    in `src/app` today, `#fff` only a handful of them. Measure it yourself with
    `grep -rhoE 'color: *#[0-9a-fA-F]{3,6}' user-interface/src/app --include=*.scss`
    rather than assuming CI caught it — approximate, since `grep -E` cannot express
    the guard's `(?<![-\w])` anchor and so would also count a `background-color` or
    `border-color` (there are none today), nor match an 8-digit alpha hex. Its `BURNDOWN` allowlist IS empty, and its
    docstring forbids adding entries to silence a failure, so never propose adding or
    removing one. `#fff` is the single gap the spec documents explicitly ("joins the
    set in the Phase-2 token sweep"), which makes a bare `color: #fff` queued debt
    rather than a settled choice — but it is one unbanned value among many, not the
    only one.
    What the guard does NOT do: it computes no contrast ratios, never pairs a
    foreground against a background, does not read token values, and does not see
    `theme.scss`, `styles.scss`, or inline template styles.
    So a real contrast failure — a `--kh-text-*` on the wrong `--kh-surface-*`, or a
    hardcoded `color: #fff` on a light chip — is caught by NEITHER this guard NOR the
    jsdom axe run. Report it, LABELLED as needing a browser, the same way §3A handles
    1.4.12: name the foreground/background pairing and the `file:line`, and say a
    real-browser measurement is required. The ratio itself cannot be settled from
    source; the suspicious pairing can.
  - `user-interface/src/app/shared/` — existing primitives to reuse before inventing
    anything: `dashboard-shell`, `empty-state`, `error-message`, `inline-banner`,
    `loading-spinner`, `stall-warning`, `confirm-dialog` / `confirm-destructive.service`,
    `breadcrumb`, plus the helpers `destructive-action.helper.ts`
    (`DestructiveActionHelper` — the confirm → re-entrancy guard → API call →
    error/toast orchestration behind the agent-runner, strategy-lab, load-draft, and
    run-history destructive actions; the target for any hand-rolled destructive flow),
    `defer-focus.ts` (move focus after a re-render),
    `extract-error-detail.ts` (`extractErrorDetail(err, fallback, options?)` — the
    repo-wide way to pull a human-readable message out of an HTTP error for an inline
    error field; grep `extractErrorDetail(` for the spread. Recommend it over
    hand-rolled `err?.error?.detail ?? …` chains when proposing error copy, and carry
    two conditions with the recommendation. First, `fallback` is REQUIRED, and it is
    where the state-specific copy §3D asks for actually goes. Second, an ARRAY
    `detail` — FastAPI's automatic 422 validation-error shape — is SKIPPED unless
    `{ joinValidationArray: true }` is passed, so the global interceptor can toast it
    instead. No manual raise in this backend passes a literal ARRAY `detail`, so that
    shape is a 422 automatic-validation artifact here. But the option is NOT a
    general fix: it rescues arrays only, and this backend does raise DICT details
    (`detail={"error": "planning_failed", …}`, on a 422 and on a status-passthrough
    4xx). A dict falls past the option straight to the `err.message` branch, so a
    component that passes `joinValidationArray` is still exposed. The trigger is
    "detail is not a non-empty string", not "detail is an array" — check the shape
    the endpoint actually raises. A component that opts out of that toast and
    omits the option does NOT fall through to your fallback: the helper returns
    `err.message` first and Angular always populates it, so the inline field renders
    the raw `Http failure response for http://…: 422 Unprocessable Entity`. That is a
    worse defect than generic copy — it leaks an internal URL — so score it as one),
    `result-count-announcement.ts` (live-region text for filtered lists),
    `latest-only.ts` (`LatestOnly` — monotonic "latest wins" guard so a slow response
    cannot overwrite a newer one; it is the intended replacement for the hand-rolled
    `const seq = ++this.xSeq` pattern, which still survives in several components —
    grep `++this.` and file what you find under the reuse rule below. Name `LatestOnly`
    for a refresh race or unstable ordering across polls), `clamp.util.ts` (integer clamp to a numeric `[min, max]` — NOT a
    string truncator; do not reach for it on text), `number-format.ts` and
    `date-only.pipe.ts` (number and date presentation), and
    `poll-while.ts` / `staleness.util.ts` (long-running job polling and staleness).
    "Build a custom X" when a shared X exists is itself a finding. This list is a
    starting point, not a census — `ls user-interface/src/app/shared/` before
    concluding no primitive exists.
  - `user-interface/src/app/core/error-handler.interceptor.ts` — the global HTTP
    error interceptor, registered app-wide in `app.config.ts`. Read it BEFORE scoring
    the failure states in §3D, because it already differentiates most of them. It
    toasts, by status: `0` → "Network error. Please check your connection and that the
    API is running." — `status === 0` is what this repo's offline state looks like;
    `401` → "Unauthorized. Please check your credentials."; `403` → "Access
    forbidden."; `503` → the server's own `detail`, but ONLY when that is a non-empty
    string. That is where a backend-unconfigured message such as "Career profile
    storage requires Postgres…" reaches the user; where it is absent the toast
    degrades to a generic "Service temporarily unavailable", which names nothing and
    leaves backend-unconfigured MISSING. `400`/`422` → one of three outcomes: a string
    `detail` verbatim, the joined messages from an array `detail`, or a content-free
    fallback when neither parses — `Bad request: ${statusText}`, which reads
    "Bad request: Bad Request" for an actual 400 but "Bad request: Unprocessable
    Entity" for a 422, since `statusText` is the transport's own reason phrase for
    whichever status arrived. `404` → "Not found:" plus `err.url` — the RESPONSE URL,
    normally identical to the request URL, and itself reportable for putting an
    internal API path in front of the user — falling back to `statusText` on the rare
    response that carries no `url`. `500` and the rest → a status-specific PREFIX
    ("Server error: ", "Error 502: ") followed by the server's detail — but when the
    body is not parseable JSON (a gateway HTML page, a proxy error, a bare traceback)
    that tail falls through to `err.message`, so the toast renders the same raw
    `Http failure response for http://…` URL leak described above. Treat a 5xx whose
    body the API does not control as leaking, not as "status-specific".
    The snackbar opens with
    `politeness: 'assertive'`, so it satisfies the HANDLED gate's announcement clause
    on its own.
    TWO ways a request escapes all of this, and both must be ruled out before you
    apply §3D's did-not-opt-out branch. It carries `SKIP_ERROR_NOTIFY` in its
    `HttpContext` (via `skipErrorNotify()` or `SKIP_NOTIFY_OPTIONS` — grep those three
    names), OR it never reaches `HttpClient` at all: `fetch()` and
    `new EventSource(…)` bypass the interceptor entirely, and several streaming and
    job-progress services use them. A bypassing request carries no `HttpContext`, so
    the three-name grep will not surface it — grep those two calls too. Either way
    there is no toast, so score its states as if it had opted out.
  - Existing `*.a11y.spec.ts` files and `user-interface/src/app/testing/a11y.ts`
    (`expectNoAxeViolations`, with `color-contrast` disabled under jsdom). Coverage is
    narrower than it looks, in four independent ways, and ALL FOUR must hold before
    you treat anything as guarded:
      * By state — a spec guards only the states its fixtures render. A spec whose
        fixtures supply empty listings, empty jobs, and empty runs audits the empty
        state and nothing else, so the populated, active-job, and error branches are
        unaudited even for defects axe could catch.
      * By assertion — the specs vary, so read the one you intend to rely on rather
        than assuming either way. Many are bare `expectNoAxeViolations` smoke tests;
        others (`strategy-lab`, `strategy-card`, `paper-trading-panel`) additionally
        assert accessible names and icon ARIA, and those assertions DO guard the
        behaviour they name. A bare axe call guards only what axe checks in jsdom,
        which excludes colour contrast (disabled outright in `axeOptions`) and
        everything interaction-dependent:
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
      * By per-spec rule disables — `expectNoAxeViolations(host, extraRules)` merges
        caller-supplied disables on top of `color-contrast`, and four specs use it:
        `agent-studio-persona` turns off `page-has-heading-one` and `region`,
        `job-listing-card` turns off `aria-required-parent`, `job-profile-form` turns
        off `aria-required-children`, and `strategy-lab` turns off `nested-interactive`
        on FEWER THAN HALF of its calls — the rest are bare, so `nested-interactive`
        genuinely IS guarded in those states. That is the general shape: a disable is
        per CALL, not per spec, so read the call's second argument, not the file.
        Re-derive the census rather than trusting this list, but not with a bare
        `grep -rn "enabled: false"` — a bare majority of those hits are unrelated
        service-mock fixture data (e.g. `of({ enabled: false, mcp_server_url: … })`),
        and that noise sits inside the SAME spec files that call the helper heavily,
        not only in specs that never call it — so filename alone will not separate
        signal from noise. Search for the rule NAMES — that is the only recipe that
        finds every site. Do NOT search for the two-argument call shape
        `expectNoAxeViolations(<host>, {`: two of the four specs hoist their disables
        into a module-level const and pass it by name
        (`expectNoAxeViolations(fixture.nativeElement, cardExtraRules)`), so that
        shape silently misses them and you would conclude those rules are enforced
        when they are switched off — the exact false negative this axis exists to
        prevent. When a call's second argument is an identifier, resolve it.
    Read each spec's fixtures, its assertions, what element it audits, AND which rules
    it disables before excluding a finding, then spend your attention on the unrendered
    states, the unrendered compositions, the disabled rules, and what axe structurally
    cannot see.
    Note that axe coverage does not always live in a `.a11y.spec.ts`: `empty-state`,
    `dashboard-shell`, and `inline-banner` call `expectNoAxeViolations` from their
    ordinary `*.component.spec.ts` and have no `.a11y.spec.ts` at all. Search for the
    call, not the filename.

## 3. Review lenses

Work all five. Under each, the listed checks are the floor, not the ceiling.

A. ACCESSIBILITY (WCAG 2.2 AA)
   - Semantics: landmark and heading structure, heading level order, lists/tables as
     real lists/tables, `role` only where native elements can't do the job.
   - Names and descriptions: every control has an accessible name that CONTAINS its
     visible label (2.5.3 Label in Name is a containment rule, not an equality one).
     Supplementing the visible text with disambiguating context is correct and often
     better — a visible "Cancel" named "Cancel <job label>" is compliant, and reporting
     it as a mismatch is a false positive. Flag only when the visible label's own text
     is absent from the accessible name, or appears with its words broken up or
     re-sequenced so the visible string is no longer contained (visible "Cancel Scan"
     named "Scan Cancel"). Word ORDER RELATIVE TO ADDED CONTEXT is not a conformance
     matter: "Backtest run 42 — Cancel" contains "Cancel" and conforms, even though
     the visible label does not come first. Containment is the test.
     Icon-only buttons carry an accessible name; form fields are programmatically
     associated with labels, hints, and errors. Which ARIA form to use is
     `ACCESSIBILITY.md`'s call — see §7.
   - Keyboard: every interaction reachable and operable without a pointer; no traps;
     tab order matches visual order; no positive `tabindex`; custom widgets implement
     the expected key bindings; visible focus ring survives (`--kh-focus-ring`).
   - Focus management: focus moves deliberately when content is destroyed or replaced
     (row triaged, job cancelled, dialog opened/closed, route changed) — see
     `defer-focus.ts`. Focus must never land on `<body>` after a user action.
   - Live regions: an update that arrives WITHOUT navigation or focus movement —
     async results, validation errors, job-status transitions, filtered result counts
     — is announced once, at the right politeness, without spamming on every poll
     tick. Where the component instead moves focus to the new result or error, that
     focus move IS the announcement: do not also recommend a live region, which would
     announce the content twice. Same condition as the HANDLED gate in §3D; if these
     two ever disagree, §3D governs.
   - Contrast and non-color signalling: text and UI-component contrast against the
     actual `--kh-surface-*` it sits on; status is never conveyed by hue alone (chips,
     graph edges, diff highlights, severity dots need text or shape too).
   - Zoom, reflow, and text spacing — three separate AA criteria; cite the right one:
       * 1.4.4 Resize Text (AA) — text resizes WITHOUT ASSISTIVE TECHNOLOGY up to
         200% without loss of content or functionality, EXCEPT captions and images of
         text. The "without assistive technology" clause is load-bearing: "the user
         can run a screen magnifier" does NOT satisfy it, so do not accept that as
         the mechanism. A control clipped or cut
         off at 200% zoom is a 1.4.4 failure, NOT a 1.4.10 one, and 1.4.10's
         two-dimensional exception does not apply to it.
       * 1.4.10 Reflow (AA) — the test is NOT "no horizontal scrollbar". It is that
         content can be presented WITHOUT LOSS OF INFORMATION OR FUNCTIONALITY and
         without requiring scrolling in TWO dimensions: at 320 CSS px width for
         vertically scrolling content, and at 256 CSS px height for horizontally
         scrolling content. Both halves matter. A layout that reaches 320 px by
         HIDING controls fails on the loss clause even with no horizontal scrollbar,
         and a single-direction horizontal scroller does not fail merely for scrolling
         horizontally. The exception covers the parts that genuinely require a
         two-dimensional layout for usage or meaning (data tables, task graphs,
         Mermaid diagrams) — test whether it is actually necessary before reporting: a
         wide data table scrolling inside its own container is compliant, a paragraph
         doing so is not.
       * 1.4.12 Text Spacing (AA) — in content using a markup language that supports
         these properties, no loss of content or functionality when the reader sets
         ALL of them and changes NO OTHER style property: line height to AT LEAST
         1.5× the font size, spacing FOLLOWING paragraphs to at least 2×, letter
         spacing to at least 0.12×, word spacing to at least 0.16×. They are minimums,
         not exact values. The criterion excepts human languages and scripts that do
         not use one or more of these properties, so do not report a word- or
         letter-spacing failure on a CJK surface without checking that first. Fixed-height containers
         that clip their overflow are the usual failure. This needs a running browser
         with the overrides applied — report it labelled as such rather than inferring
         it from a stylesheet.
   - Motion and timing, at the A/AA bar this review holds to, and only where the
     criterion actually applies. The summaries below are compressed, and so is
     `backend/agents/accessibility_audit_team/wcag_criteria.py`. That table is a
     convenience index for confirming a criterion's number, name, and level: complete
     for Level A and AA, and deliberately partial at AAA (3 of 31), so an AAA lookup
     usually misses — 2.3.3, cited a few lines below, among them. Absence from it is
     not evidence a criterion does not exist. It is NOT a source for conditions: its
     one-line descriptions keep some exceptions (2.5.8's "or have sufficient spacing",
     3.3.8's "unless alternatives exist", 2.4.11's "not entirely") and drop others
     entirely, and on 2.3.1 they state only the three-flash half, omitting the
     below-threshold alternative. Its `techniques` and `failures` lists are pointers
     into the W3C technique catalogue, not a checked mapping — open a technique and
     confirm it addresses the criterion you are citing before it goes in a finding.
     Where a finding turns on a threshold, applicability condition, or exception — or
     on an AAA criterion at all — go to the criterion's own normative text rather than
     the table or this summary.
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
     `prefers-reduced-motion` maps to 2.3.3 Animation from Interactions, which is
     AAA — but 2.3.3 covers only motion a USER'S INTERACTION initiates. Motion the
     PAGE starts on its own is 2.2.2 territory, at Level A, and therefore reportable
     when it meets 2.2.2's conditions above (automatic, more than five seconds, in
     parallel with other content). So classify by what starts the animation before
     picking a criterion: a scroll- or hover-triggered parallax is 2.3.3 and an
     enhancement; a page-initiated entrance animation is 2.2.2 and a failure only once
     it passes five seconds. Never report a missing `prefers-reduced-motion` query as
     an AA failure on its own.
   - WCAG 2.2 additions specifically:
       * Target size (2.5.8) is a size-OR-spacing rule, not a flat floor: a target
         passes at 24×24 CSS px, or under one of the criterion's own carve-outs. Check
         these before flagging, and say which one you checked — naming an exception is
         not the same as meeting it:
           - Spacing — undersized targets positioned so that a circle of 24 CSS px
             DIAMETER, centred on the BOUNDING BOX of each undersized target,
             intersects NEITHER another target NOR the circle around another
             undersized target. Diameter, not radius: reading it as a radius doubles
             the clearance and manufactures failures. A full-size neighbour counts by
             its own bounding box, so checking circle-to-circle alone wrongly exempts
             a small control sitting against a full-size one.
           - Inline — the target sits IN A SENTENCE, or its size is constrained by the
             line-height of surrounding non-target text. A control merely styled
             inline does not qualify: a compact toolbar action still owes 24×24 or the
             spacing test.
           - Equivalent — the same function is reachable from another control on the
             same page that MEETS THIS CRITERION. Note "meets the criterion", not
             "meets the size": an alternate control that itself passes via the
             spacing test or another carve-out is a valid Equivalent defence.
           - User-agent control — the size is set by the user agent and unmodified by
             the author.
           - Essential — a particular presentation is essential or legally required.
       * Focus not obscured: at the AA bar this review holds to (2.4.11), a focused
         control must not be ENTIRELY hidden by AUTHOR-CREATED content — a sticky
         header, toolbar, or footer being the usual culprits. Content the USER opened
         is carved out: if they can reveal the focused control without advancing
         focus, such as pressing Escape to dismiss an overlay they opened, it does not
         count as hidden, so check for that before reporting. A second carve-out
         covers configurable interfaces: where the user can reposition content, only
         its INITIAL position counts, so a panel the user themselves dragged over the
         focused control is not a failure. Partial obscuration fails only the AAA
         criterion (2.4.12) — report it, if at all, as an enhancement, never as an AA
         violation.
       * Dragging movements (2.5.7) have a single-pointer alternative that does NOT
         itself require dragging — a second drag gesture does not satisfy this, since
         dragging is already single-pointer. Essential dragging, and behaviour set by
         the user agent and not modified by the author, are excepted.
       * Redundant Entry (3.3.7, A) — information the user already entered, or that
         was provided to them, and that is required again IN THE SAME PROCESS, is
         auto-populated or offered for selection rather than demanded again. Scope and
         exceptions both matter: re-entry across two DIFFERENT processes is out of
         scope entirely, and the criterion excepts re-entry that is essential, that is
         required for the SECURITY of the content (so a password or API-key
         confirmation field passes), or where the earlier value is no longer valid.
       * Consistent Help (3.2.6, A) — where one of the criterion's help mechanisms
         (HUMAN contact details, a HUMAN contact mechanism, a self-help option, a
         FULLY AUTOMATED contact mechanism — the human/automated split is normative,
         so a support chat staffed by people and a chatbot are different mechanisms,
         not one repeated one) is repeated across a set of pages, it occurs IN THE SAME
         ORDER RELATIVE TO OTHER PAGE CONTENT — that is DOM/serialized order, not
         pixel position — unless the user initiated the change. A help link visually
         repositioned at a breakpoint but unmoved in the DOM passes; one moved in the
         DOM fails even if it looks the same.
       * Accessible Authentication (Minimum) (3.3.8, AA) — no step of an
         authentication process may require a cognitive function test (remembering a
         password, transcribing characters, solving a puzzle) unless THAT STEP itself
         provides one of FOUR exceptions — scope matters, an exception available
         elsewhere in the flow does not cover a step that offers none, so a login that
         supports password managers followed by a six-box one-digit-per-field TOTP
         step still fails at the TOTP step: an alternative authentication method
         exists, a mechanism assists (a password manager, paste support), the test is
         to RECOGNIZE OBJECTS
         (an image-selection CAPTCHA), or the test is to identify NON-TEXT CONTENT THE
         USER PROVIDED. The last two are commonly missed and make an image CAPTCHA or
         a "confirm the photo you uploaded" step compliant, not a blocker. This one is
         inside the AA bar and easy to miss: check it on any credential-entry or
         connect-an-account surface.

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
   Two of the fourteen are easy to mis-scope, so they are defined here:
     - partial — some of the requested content arrived and some did not: a page that
       renders three of five panels because one call failed, or a list truncated by an
       interrupted fetch. The distinguishing question is whether the user can tell
       WHAT is missing. Rendering the successful part and staying silent about the
       rest is the failure mode.
     - stale-data — what is on screen is real but no longer current: polling stopped,
       a refresh failed and the previous render stayed up, or the tab was
       backgrounded. The distinguishing question is whether the user can tell HOW OLD
       the view is. Do not reach for `staleness.util.ts` by default: it reads a JOB's
       server-stamped `last_activity_at`, so it measures job-side activity (which
       keeps advancing while the browser view is frozen) and returns nothing at all
       for a once-fetched list like `llm-config` or `integrations`. It fits only a
       job-backed view, and there it belongs to the STALLED state (§3B reaches the
       same helper through the `stall-warning` component), so naming it for
       stale-data risks conflating two states this section insists are distinct. For
       every other page the recommendation is a rendered fetched-at timestamp or an
       explicit refresh affordance, not this helper.
   The five failure states are a PARTITION, not overlapping labels — classify each
   observed failure into exactly one, in this order, so the same event does not appear
   in two rows:
     - offline — no transport at all; the request never reached a server. In this
       repo that is `HttpErrorResponse.status === 0`.
     - permission-denied — the server answered, refusing on identity or authorization
       (401/403).
     - backend-unconfigured — the server answered, reporting the feature is not set up
       (missing provider, credential, or configuration) rather than refusing the user.
       Typically a 503 whose `detail` names what is missing.
     - failed — the request SUCCEEDED and the domain operation or job it started then
       reached a terminal failed state. Work failed, not the call.
     - API-error — the residual: any other request failure (5xx, timeout, malformed or
       unparseable response). Reach for this only when none of the four above fits.
   One generic error branch commonly serves several of these at once. The repo-wide
   idiom is an RxJS subscribe callback, not a `try`/`catch` — `error: (err) => {
   this.error = extractErrorDetail(err, 'Failed to load.'); … }`. Grep for `error: (`
   to find those, but that grep alone misses a second RxJS form: several dashboards
   put the same handling in a `catchError((err) => …)` operator instead, so grep
   `catchError` too or you will read a page's primary list-load error branch as
   absent. Literal `try`/`catch` around this helper does not occur. Note the REQUIRED
   second argument: that fallback string IS the component's per-state copy, and a
   sketch that omits it does not compile.
   Score such a branch against the global interceptor rather than in isolation, because
   for most requests it is not the only thing the user receives:
     - Request opted out of the toast (`SKIP_ERROR_NOTIFY` / `skipErrorNotify()` /
       `SKIP_NOTIFY_OPTIONS`), or bypassed the interceptor via `fetch`/`EventSource` —
       the inline branch is the whole story, and one callback rendering the same
       message for a 401, a 503, and a network drop is scored per state on whether ITS
       OUTPUT gives that state's user a way forward. A bare "Failed to load" gives
       none of them one, so it is MISSING for permission-denied, backend-unconfigured,
       offline AND API-error alike — the HANDLED gate below puts all four in the
       recoverability list, and a dead end fails it whichever state produced it.
       Nothing was announced either, since there is no toast on this path. What varies
       between the four is not the disposition but the remedy each needs, which is
       what your one finding must spell out.
     - Request did not opt out — the interceptor already distinguished 0 / 401 / 403 /
       503 for the other three states, AND separately distinguishes 400 / 422 / 404 /
       500 (and the rest) for API-error, each with its own toast — so a generic inline
       message is NOT by itself a MISSING for ANY of the four, API-error included.
       What still earns MISSING there is an inline message that CONTRADICTS the toast
       — two explanations of one failure is its own finding — or a state whose
       recovery needs an affordance a toast cannot offer: a sign-in route for 401, a
       link to the setup the 503 names, a retry for offline or a 5xx. The missing
       affordance is the finding, not the wording.
   Either way, when one branch is MISSING for several states, file ONE finding naming
   every state it fails and the distinct copy or handling each needs — not one finding
   per state against the same line.
   Give every state exactly one disposition, and state which. Decide in this order,
   so MISSING and NOT APPLICABLE cannot both be argued from the same empty search:
   first ask whether the page can ENTER the state at all — if nothing in its
   operations or branches can reach it, or it hands the state elsewhere, that is NOT
   APPLICABLE and the enquiry ends. Only for a state the page CAN enter do you ask
   whether it is handled, and an empty search then means MISSING:
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
     the file and line that proves it. Where the finding IS an absence, §3D's rule
     governs instead — cite the nearest operation or conditional where the handling
     would belong, say what is absent there, and back it with the scoped search that
     found nothing. If you have neither a proving line nor an evidenced absence, the
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
    fourteen from §3D. Columns: state | HANDLED / MISSING / N/A | evidence. The
    evidence bar is §3D's, unrelaxed — a cell is not a lighter form of a finding: the
    `file:line` of the handled branch; for MISSING, either the incorrect branch or the
    nearest operation where handling would belong PLUS what is absent PLUS the scoped
    search that found nothing; for N/A, the handoff code or the demonstration from the
    page's own operations that it cannot enter the state. An N/A asserted without one
    of those two is itself a gap, in the table exactly as in the findings.
    This is the audit the definition of done requires; a page whose table omits a
    state is not finished. Every MISSING row is represented among the findings below —
    but per §3D, where one branch is MISSING for several states, that is ONE finding
    naming every state it fails, not one finding per row.

Then every finding, in ranked order:

  ### <ID: A11Y-n | FLOW-n | CLARITY-n | STATE-n | SYSTEM-n> — <short title>
  - **Severity**: Blocker | High | Medium | Low
  - **Location**: `path/to/file.html:120-134` (+ other locations for the same cause).
    A finding that needs a browser to confirm still names the file and the element or
    rule it is about, and adds `— needs browser` after the range; never invent a line
    number, and never demote a real AA failure to Open questions just because its
    confirmation is manual. Open questions is for what needs a PRODUCT decision, or a
    browser check you could not narrow to a location at all.
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
  - **Verification**: how a reviewer proves it fixed — the assertion to add and the
    spec to add it to (choose that spec by §7's rule, which carries the exceptions; do
    not re-derive it here), or the manual walkthrough to run (keyboard, screen reader,
    200% zoom for 1.4.4, 320 px for 1.4.10, the four text-spacing overrides for 1.4.12,
    real-browser contrast).

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
  - ARIA in templates — follow `ACCESSIBILITY.md`'s "ARIA attribute form" rule as
    written:
    it states a preference for NEW code (plain attribute for a constant,
    `[attr.aria-*]` for a computed value) and explicitly declines to enforce it
    retroactively. BOTH ARE CORRECT and both are in wide use — a couple of dozen
    templates carry constant values bound through `[attr.aria-*]` — so file no
    finding for a
    conversion in EITHER direction; that is a no-op refactor and the kind of style
    preference §8 forbids. If you think one form should govern everywhere, raise it
    under §5's Open questions rather than filing per-attribute findings.
    For an interrupting announcement follow the same file's `aria-live` rule: prefer
    `role="alert"`, and never pair it with `aria-live="assertive"` on one element.
  - Native semantics before ARIA; ARIA only where no native element does the job.
  - Design by Contract applies to any code you propose, per the repo-wide mandate in
    `CLAUDE.md` ("mandatory for all code and comments"): preconditions, postconditions,
    and invariants documented in the docstring, enforced rather than silently coerced.
    The narrower "public APIs" phrasing in `CONTRIBUTORS.md` describes what the
    software-engineering team enforces in generated code, and does not relax the
    repo-wide rule.
  - Any behavior change needs test coverage (90% line-coverage floor). Where a spec
    already calls `expectNoAxeViolations` for that component, extend it rather than
    writing a new harness — usually a `.a11y.spec.ts`, but for `empty-state`,
    `dashboard-shell`, and `inline-banner` it is the ordinary `*.component.spec.ts`.
    Most components have neither — far fewer spec files call the helper than there are
    components, so for the majority the correct recommendation IS to create
    `<component>.component.a11y.spec.ts`. Count both before assuming otherwise
    (`grep -rl expectNoAxeViolations` against `find -name '*.component.ts'`). Say which
    of the two you mean.
  - Never reference an external issue tracker in code, comments, or docs. Issue
    numbers belong in pull-request bodies and nowhere else — and there they are
    required, via `Closes #N`, on any PR that implements one of these findings.

## 8. Do not report

  - Findings a spec genuinely guards. ALL FOUR conditions from §2 must hold before you
    drop a finding: the fixtures render THAT STATE, the spec asserts THAT BEHAVIOUR,
    the audited element covers THAT COMPOSITION (a component-scoped `axe(host, …)`
    never sees siblings, a second instance, or the page shell), and the relevant rule
    is NOT disabled via `extraRules`. Any one of the four failing means unguarded —
    report it. The spec need not be named `.a11y.spec.ts`; search for the
    `expectNoAxeViolations` call. A bare
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

You are finished when: the primary task walkthrough is written out with its step
count, so every interaction-cost finding has a baseline to measure against; every
routed <TEAM> page carries a stated disposition —
handled, missing, or evidenced not-applicable — for all fourteen states in §3D; every
finding cites a file and line, or — for an absence — meets §3D's evidence bar in
full (the nearest relevant location, what is missing there, AND the scoped search
that found nothing), or is explicitly marked as needing a browser; every
recommendation names a concrete mechanism — an element, attribute, token, shared
primitive, copy change, or component-logic change — rather than
describing an intention; and the Top 5 — however many survived verification, which may
be none — is ordered by §4 step 7's rank, (user impact × frequency) ÷ effort, which
is what "highest leverage" in §5 means; where that ordering and "removes the most
user pain" disagree, the rank governs and you say so in one line. Shipping the Top 5
should remove the largest share of user
pain.
```

---

## Usage notes

- **One team per run.** The prompt trades breadth for specificity; running it across
  every team at once collapses it into a generic checklist.
- **Teams are not a partition of the UI.** Do not derive the run list from the backend
  `TEAM_CONFIGS`: several routed pages (`cognition`, `integrations`, `llm-config`,
  `llm-usage`, the root dashboard, and others) belong to no team in it, and at least
  one configured team has no routed page at all. Enumerate from
  `user-interface/src/app/app.routes.ts` and assign every route to a run, or pages get
  reviewed by nobody.
- **Browser-dependent checks** — real contrast against composited surfaces, 200% zoom
  reflow, and actual screen-reader output — cannot be settled from source alone. The
  prompt requires those to be labelled rather than guessed, so the labelled set is the
  manual QA list for the review.
- **Feeding results back**: findings scoped to a shared primitive
  (`user-interface/src/app/shared/`) are the highest-leverage output, since one fix
  clears the same defect across every team's pages.
