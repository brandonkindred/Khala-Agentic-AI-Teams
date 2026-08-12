# Host Persona Audit Under Agent Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host the existing persona audit panel at `/agent-studio/persona-run/:runId` inside the Studio shell, and let Stage 4 open it via **View full audit** without touching `/persona-testing`.

**Architecture:** `/agent-studio` stays `AgentStudioShellComponent` (header + stepper + session providers). Its main area becomes a `router-outlet` whose default child is the extracted four-stage host and whose `persona-run/:runId` child is a thin wrapper around `PersonaTestAuditPanelComponent`. Stage 4 keeps the inline live-run.

**Tech Stack:** Angular 19 standalone components, Angular Router (`loadComponent` + nested children), Vitest, `RouterTestingHarness`

**Spec:** `docs/superpowers/specs/2026-08-12-studio-persona-audit-host-design.md`

**Worktree:** `.worktrees/5942-studio-journey-children` on branch `5942-studio-journey-children`. Run frontend commands from `user-interface/` (`nvm use` for Node 22).

## Global Constraints

- Follow the approved design spec exactly
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public function/method/module
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Coverage ≥ 90% on new/changed frontend files
- `AgentStudioStateService.navigateToStage` is **0-based**; Personas is index `3`. The spec’s “navigateToStage(4)” means Stage 4 / Personas — call `navigateToStage(3)` (use a `STAGE_PERSONAS = 3` constant)
- Do not delete `/persona-testing` or `/persona-testing/audit/:runId`
- Do not add Browse/Test slide-outs
- Do not replace Stage 4’s inline live-run
- Do not change audit polling, tabs, artifacts, or persona-chat behavior
- Do not change backend APIs
- Do not move `AgentStudioStateService` / `AgentStudioFacade` off the shell `providers` array

## File map

| File | Role |
|---|---|
| `user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.ts` | **Modify** — optional `backLink` / `backLabel` inputs |
| `user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.html` | **Modify** — bind those inputs on the back `<a>` |
| `user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.spec.ts` | **Modify** — default + custom back-link tests |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.ts` | **Create** — Studio wrapper; `navigateToStage(3)` on init |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.html` | **Create** — mounts audit panel with Studio back inputs |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.scss` | **Create** — fill Studio main area |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.spec.ts` | **Create** — stage + back-input tests |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.ts` | **Create** — extracted `@switch` of four stages |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.html` | **Create** — current shell main `@switch` |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.spec.ts` | **Create** — stage-mount tests moved off the shell |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts` | **Modify** — `RouterOutlet`, `hideFooter`, drop stage imports |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.html` | **Modify** — outlet in main; hide footer when `hideFooter()` |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts` | **Modify** — `provideRouter`; drop stage-mount tests; add footer-hide test |
| `user-interface/src/app/app.routes.ts` | **Modify** — nested children on `agent-studio` |
| `user-interface/src/app/app.routes.spec.ts` | **Modify** — assert nested `persona-run/:runId` child |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.ts` | **Modify** — `openFullAudit()` |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.html` | **Modify** — **View full audit** in live-run header |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.scss` | **Modify** — trailing actions group |
| `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.spec.ts` | **Modify** — absent/present/navigate tests |

---

### Task 1: Parameterize the audit panel back link

**Files:**
- Modify: `user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.ts`
- Modify: `user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.html`
- Modify: `user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.spec.ts`

**Interfaces:**
- Consumes: existing `RouterLink` back `<a>` in the panel header
- Produces:
  - `@Input() backLink: string` default `'/persona-testing'`
  - `@Input() backLabel: string` default `'Back to Testing Personas'`

- [ ] **Step 1: Write the failing tests**

Add to `persona-test-audit-panel.component.spec.ts` (keep `buildFixture` as-is; call `fixture.detectChanges()` so the template binds):

```typescript
  it('defaults the back link to /persona-testing', () => {
    buildFixture();
    fixture.detectChanges();
    const link = fixture.nativeElement.querySelector('a.back-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/persona-testing');
    expect(link.textContent).toContain('Back to Testing Personas');
  });

  it('renders a custom backLink and backLabel when provided', () => {
    buildFixture();
    component.backLink = '/agent-studio';
    component.backLabel = 'Back to Agent Studio';
    fixture.detectChanges();
    const link = fixture.nativeElement.querySelector('a.back-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/agent-studio');
    expect(link.textContent).toContain('Back to Agent Studio');
    expect(link.textContent).not.toContain('Testing Personas');
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.spec.ts
```

Expected: FAIL — `backLink` / `backLabel` are not properties, or the template still hardcodes `/persona-testing`.

- [ ] **Step 3: Write minimal implementation**

In `persona-test-audit-panel.component.ts`, add `Input` to the `@angular/core` import and these fields on the class (defaults preserve the old dashboard):

```typescript
  /**
   * Router path for the header back control.
   *
   * Preconditions: a non-empty absolute-from-root path (leading `/`).
   * Postconditions: the template's back `routerLink` equals this value.
   */
  @Input() backLink = '/persona-testing';

  /**
   * Visible label for the header back control.
   *
   * Preconditions: a non-empty string.
   * Postconditions: the template renders this text next to the back icon.
   */
  @Input() backLabel = 'Back to Testing Personas';
```

In `persona-test-audit-panel.component.html`, replace the hardcoded back link:

```html
<div class="page-header">
  <a [routerLink]="backLink" class="back-link">
    <mat-icon>arrow_back</mat-icon> {{ backLabel }}
  </a>
```

Leave every other template node unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run the same vitest command as Step 2.

Expected: PASS (existing status/error tests still pass).

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.ts \
        user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.html \
        user-interface/src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.spec.ts
git commit -m "$(cat <<'EOF'
Let the persona audit panel take its back link as inputs so Studio can point it at /agent-studio without changing the Testing Personas default.

EOF
)"
```

---

### Task 2: Studio audit wrapper

**Files:**
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.ts`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.html`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.scss`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.spec.ts`

**Interfaces:**
- Consumes: `AgentStudioStateService.navigateToStage(index: number): void`; `PersonaTestAuditPanelComponent` `backLink` / `backLabel` from Task 1
- Produces: `AgentStudioPersonaAuditComponent` (selector `app-agent-studio-persona-audit`). On construction/init, active stage is Personas (`3`). Template mounts `app-persona-test-audit-panel` with `backLink="/agent-studio"` and `backLabel="Back to Agent Studio"`. Does not read `:runId`.

- [ ] **Step 1: Write the failing tests**

Create `agent-studio-persona-audit.component.spec.ts`:

```typescript
import { Component, Input } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { PersonaTestAuditPanelComponent } from '../persona-test-audit-panel/persona-test-audit-panel.component';
import { AgentStudioPersonaAuditComponent } from './agent-studio-persona-audit.component';

@Component({ selector: 'app-persona-test-audit-panel', standalone: true, template: '' })
class StubAuditPanelComponent {
  @Input() backLink = '/persona-testing';
  @Input() backLabel = 'Back to Testing Personas';
}

describe('AgentStudioPersonaAuditComponent', () => {
  let fixture: ComponentFixture<AgentStudioPersonaAuditComponent>;
  let state: AgentStudioStateService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioPersonaAuditComponent],
      providers: [AgentStudioStateService],
    })
      .overrideComponent(AgentStudioPersonaAuditComponent, {
        remove: { imports: [PersonaTestAuditPanelComponent] },
        add: { imports: [StubAuditPanelComponent] },
      })
      .compileComponents();

    state = TestBed.inject(AgentStudioStateService);
    state.setRegistryAgentId('blogging.planner');
    fixture = TestBed.createComponent(AgentStudioPersonaAuditComponent);
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('moves the stepper to Personas (index 3) on init', () => {
    expect(state.activeStage()).toBe(3);
  });

  it('does not clear existing handoff state', () => {
    expect(state.registryAgentId()).toBe('blogging.planner');
  });

  it('mounts the audit panel with Studio back inputs', () => {
    const panel = fixture.debugElement.query(By.directive(StubAuditPanelComponent));
    expect(panel).toBeTruthy();
    const stub = panel.componentInstance as StubAuditPanelComponent;
    expect(stub.backLink).toBe('/agent-studio');
    expect(stub.backLabel).toBe('Back to Agent Studio');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.spec.ts
```

Expected: FAIL — `AgentStudioPersonaAuditComponent` is not defined / cannot be imported.

- [ ] **Step 3: Write minimal implementation**

`agent-studio-persona-audit.component.ts`:

```typescript
import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { PersonaTestAuditPanelComponent } from '../persona-test-audit-panel/persona-test-audit-panel.component';

/** 0-based index of Stage 4 (Personas) in STUDIO_STAGES. */
const STAGE_PERSONAS = 3;

/**
 * Studio host for the persona audit panel (nested `/agent-studio/persona-run/:runId`).
 *
 * Preconditions: provided inside a Studio shell so `AgentStudioStateService` resolves.
 * Postconditions: after init, `activeStage()` is Personas (3). The audit panel is
 *   mounted with Studio back inputs; `:runId` is left to the panel's `ActivatedRoute`.
 * Invariants: this wrapper does not poll, fetch artifacts, or own a second Back control.
 */
@Component({
  selector: 'app-agent-studio-persona-audit',
  standalone: true,
  imports: [PersonaTestAuditPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-persona-audit.component.html',
  styleUrl: './agent-studio-persona-audit.component.scss',
})
export class AgentStudioPersonaAuditComponent implements OnInit {
  private readonly state = inject(AgentStudioStateService);

  ngOnInit(): void {
    this.state.navigateToStage(STAGE_PERSONAS);
  }
}
```

`agent-studio-persona-audit.component.html`:

```html
<section class="studio-audit">
  <app-persona-test-audit-panel
    backLink="/agent-studio"
    backLabel="Back to Agent Studio"
  />
</section>
```

`agent-studio-persona-audit.component.scss`:

```scss
:host {
  display: block;
  height: 100%;
}

.studio-audit {
  height: 100%;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run the same vitest command as Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.html \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.scss \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.spec.ts
git commit -m "$(cat <<'EOF'
Add a Studio wrapper that mounts the persona audit panel and selects Stage 4 so a deep link still shows Personas on the stepper.

EOF
)"
```

---

### Task 3: Nested Studio routes, stage host, hide footer

**Files:**
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.ts`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.html`
- Create: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.spec.ts`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.html`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts`
- Modify: `user-interface/src/app/app.routes.ts`
- Modify: `user-interface/src/app/app.routes.spec.ts`

**Interfaces:**
- Consumes: `AgentStudioPersonaAuditComponent` from Task 2; existing four stage components; `STUDIO_STAGES` / `AgentStudioStateService` already on the shell
- Produces:
  - `AgentStudioStageHostComponent` (selector `app-agent-studio-stage-host`) — the current shell `@switch`
  - `AgentStudioShellComponent.hideFooter(): boolean` — true when the deepest child route snapshot has `data.hideStudioFooter === true`
  - Route children on `path: 'agent-studio'`:
    - `{ path: '', loadComponent: AgentStudioStageHostComponent }`
    - `{ path: 'persona-run/:runId', loadComponent: AgentStudioPersonaAuditComponent, data: { hideStudioFooter: true } }`
  - `/persona-testing/audit/:runId` unchanged

- [ ] **Step 1: Write the failing tests**

**1a. Stage host spec** — create `agent-studio-stage-host.component.spec.ts`. Copy the shell spec’s stubs (`StubAgentRunnerComponent`, `StubAgentCatalogComponent`, `StubAgentProvisioningPanelComponent`, `StubPersonaComponent`, `StubComposeTeamComponent`) and the same `overrideComponent` pattern, but import `AgentStudioStageHostComponent` instead of the shell. Provide `AgentStudioStateService` (the host does not provide it). Move these three assertions here:

```typescript
  it('renders the real Build Agent stage (not the placeholder) on Stage 1', () => {
    expect(state.activeStage()).toBe(0);
    expect(fixture.nativeElement.querySelector('app-agent-studio-build-agent')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('renders the real Test Agent stage (not the placeholder) on Stage 2', () => {
    state.navigateToStage(1);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-test-agent')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('renders the real Compose Team stage (not the placeholder) on Stage 3', () => {
    state.navigateToStage(2);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-compose-team')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });

  it('renders the Personas stage on Stage 4', () => {
    state.navigateToStage(3);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-agent-studio-persona')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeNull();
  });
```

`beforeEach` must `overrideComponent` on `AgentStudioStageHostComponent` (not the shell) to swap persona/compose, and override build/test children the same way the shell spec does today.

**1b. Routes spec** — add to `app.routes.spec.ts`:

```typescript
  it('nests persona-run under agent-studio and keeps the old audit route', async () => {
    const shell = routes[0];
    const children = (shell?.children ?? []) as Route[];
    const studio = children.find((r) => r.path === 'agent-studio');
    expect(studio).toBeDefined();
    expect(studio!.children?.map((c) => c.path)).toEqual(['', 'persona-run/:runId']);
    const auditChild = studio!.children?.find((c) => c.path === 'persona-run/:runId');
    expect(auditChild?.data).toEqual({ hideStudioFooter: true });
    expect(typeof auditChild?.loadComponent).toBe('function');
    const { AgentStudioPersonaAuditComponent } = await import(
      './components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component'
    );
    expect(await auditChild!.loadComponent!()).toBe(AgentStudioPersonaAuditComponent);

    const emptyChild = studio!.children?.find((c) => c.path === '');
    expect(typeof emptyChild?.loadComponent).toBe('function');
    const { AgentStudioStageHostComponent } = await import(
      './components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component'
    );
    expect(await emptyChild!.loadComponent!()).toBe(AgentStudioStageHostComponent);

    const oldAudit = children.find((r) => r.path === 'persona-testing/audit/:runId');
    expect(oldAudit).toBeDefined();
    expect(typeof oldAudit?.loadComponent).toBe('function');
  });
```

**1c. Shell spec** — add `provideRouter([])` to the existing `providers` array so `<router-outlet>` can construct. Delete the three tests that query `app-agent-studio-build-agent` / `app-agent-studio-test-agent` / `app-agent-studio-compose-team` (they now live on the stage host). Drop the stage-component `overrideComponent` blocks and their stub classes from this file (the shell no longer imports those stages). Keep stepper / draft / footer-gate tests.

Add this new test (uses `RouterTestingHarness` so the child route actually activates):

```typescript
  it('hides the continue footer on the persona-run child and keeps handoff state', async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [AgentStudioShellComponent, NoopAnimationsModule],
      providers: [
        { provide: AgentStudioApiService, useValue: agentStudioApi },
        { provide: AgenticTeamApiService, useValue: agenticTeamApi },
        provideRouter([
          {
            path: '',
            component: AgentStudioShellComponent,
            children: [
              { path: '', component: StubStageHostComponent },
              {
                path: 'persona-run/:runId',
                component: StubAuditHostComponent,
                data: { hideStudioFooter: true },
              },
            ],
          },
        ]),
      ],
    }).compileComponents();

    const harness = await RouterTestingHarness.create();
    const shell = await harness.navigateByUrl('/', AgentStudioShellComponent);
    shell.state.setRegistryAgentId('reg-keep');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeTruthy();

    await harness.navigateByUrl('/persona-run/run-1');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeNull();
    expect(harness.routeNativeElement?.querySelector('app-stub-audit-host')).toBeTruthy();
    expect(shell.state.registryAgentId()).toBe('reg-keep');

    await harness.navigateByUrl('/');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('.studio__footer')).toBeTruthy();
    expect(shell.state.registryAgentId()).toBe('reg-keep');
  });
```

Declare the two stubs at the top of the spec:

```typescript
@Component({ selector: 'app-stub-stage-host', standalone: true, template: '' })
class StubStageHostComponent {}

@Component({ selector: 'app-stub-audit-host', standalone: true, template: '' })
class StubAuditHostComponent {}
```

Import `provideRouter` from `@angular/router` and `RouterTestingHarness` from `@angular/router/testing`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run \
  src/app/app.routes.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts
```

Expected: FAIL — stage host / nested children / `hideFooter` do not exist. Existing shell tests that still query stage selectors fail once those tests are deleted; the new footer-hide test fails until the outlet + `hideFooter` land.

- [ ] **Step 3: Write minimal implementation**

**Stage host** — `agent-studio-stage-host.component.ts`:

```typescript
import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { STUDIO_STAGES } from '../../../models/agent-studio.model';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';
import { AgentStudioComposeTeamComponent } from './agent-studio-compose-team.component';
import { AgentStudioPersonaComponent } from './agent-studio-persona.component';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';
import { AgentStudioTestAgentComponent } from './agent-studio-test-agent.component';

/**
 * Default `/agent-studio` child: the four-stage switch that used to live in the
 * shell template.
 *
 * Preconditions: `AgentStudioStateService` is provided by an ancestor (the shell).
 * Postconditions: the template renders exactly one of the four stage components
 *   (or the defensive placeholder) matching `state.activeStage()`.
 */
@Component({
  selector: 'app-agent-studio-stage-host',
  standalone: true,
  imports: [
    AgentStudioBuildAgentComponent,
    AgentStudioComposeTeamComponent,
    AgentStudioPersonaComponent,
    AgentStudioStagePlaceholderComponent,
    AgentStudioTestAgentComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-studio-stage-host.component.html',
})
export class AgentStudioStageHostComponent {
  readonly state = inject(AgentStudioStateService);
  readonly stages = STUDIO_STAGES;

  readonly activeStageDef = computed(() => {
    const idx = this.state.activeStage();
    /* v8 ignore next 3 -- defensive: activeStage is range-guarded by AgentStudioStateService */
    if (idx < 0 || idx >= this.stages.length) {
      throw new RangeError(`activeStageDef: active stage index ${idx} is out of range`);
    }
    return this.stages[idx];
  });
}
```

`agent-studio-stage-host.component.html` — move the current shell `<main>` `@switch` here **without** the `<main>` wrapper (the shell still owns `.studio__stage`):

```html
@switch (activeStageDef().key) {
  @case ('build') {
    <app-agent-studio-build-agent />
  }
  @case ('test') {
    <app-agent-studio-test-agent />
  }
  @case ('compose') {
    <app-agent-studio-compose-team />
  }
  @case ('personas') {
    <app-agent-studio-persona />
  }
  @default {
    <app-agent-studio-stage-placeholder
      [title]="activeStageDef().label"
      [blurb]="activeStageDef().blurb"
      [icon]="activeStageDef().icon"
      [handoff]="state.handoff()"
    />
  }
}
```

**Shell TS** — keep `AgentStudioStateService` and `AgentStudioFacade` in `providers`. Remove imports of the four stage components and the placeholder. Add:

```typescript
import { ActivatedRoute, NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { filter, map, startWith } from 'rxjs';
```

Add `RouterOutlet` to the `@Component.imports` array.

Inject and expose `hideFooter`:

```typescript
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /**
   * True when the active child route sets `data.hideStudioFooter`.
   *
   * Preconditions: this component is the routed `/agent-studio` parent.
   * Postconditions: `true` iff the deepest activated child snapshot has
   *   `hideStudioFooter === true`; `false` when there is no child (unit tests
   *   that construct the shell without navigating).
   */
  readonly hideFooter = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map(() => this.childHidesFooter()),
      startWith(this.childHidesFooter()),
    ),
    { initialValue: false },
  );

  private childHidesFooter(): boolean {
    let child = this.route.firstChild;
    while (child?.firstChild) {
      child = child.firstChild;
    }
    return child?.snapshot.data['hideStudioFooter'] === true;
  }
```

Leave `activeStageDef`, `forwardDisabled`, draft load/save, and `onContinue` on the shell — the footer still needs them.

**Shell HTML** — replace the `<main>` `@switch` with an outlet, and wrap the footer:

```html
  <main class="studio__stage">
    <router-outlet />
  </main>

  @if (!hideFooter()) {
  <footer class="studio__footer">
```

Close the `@if` after the existing `</footer>`. Header and stepper stay as they are.

**Routes** — replace the current `agent-studio` entry in `app.routes.ts` with:

```typescript
      {
        path: 'agent-studio',
        loadComponent: () =>
          import('./components/agent-team-studio/agent-studio-shell/agent-studio-shell.component').then(
            (m) => m.AgentStudioShellComponent,
          ),
        title: 'Agent Studio',
        data: { breadcrumb: 'Agent Studio' },
        children: [
          {
            path: '',
            loadComponent: () =>
              import(
                './components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component'
              ).then((m) => m.AgentStudioStageHostComponent),
          },
          {
            path: 'persona-run/:runId',
            loadComponent: () =>
              import(
                './components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component'
              ).then((m) => m.AgentStudioPersonaAuditComponent),
            data: { hideStudioFooter: true },
          },
        ],
      },
```

Do not change the `persona-testing/audit/:runId` entry.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run \
  src/app/app.routes.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.spec.ts
```

Expected: PASS. The existing “lazily loads every feature route” test still passes because `agent-studio` keeps `loadComponent` (nested children are not top-level AppShell children).

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.html \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.spec.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.html \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts \
        user-interface/src/app/app.routes.ts \
        user-interface/src/app/app.routes.spec.ts
git commit -m "$(cat <<'EOF'
Nest the persona audit view under /agent-studio so the Studio shell stays mounted and can hide the continue footer on that child.

EOF
)"
```

---

### Task 4: Stage 4 “View full audit”

**Files:**
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.ts`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.html`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.scss`
- Modify: `user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.spec.ts`

**Interfaces:**
- Consumes: `run(): PersonaTestRunDetail | null` (existing signal); `Router.navigate`
- Produces: `openFullAudit(): void` — navigates to `/agent-studio/persona-run/:runId` using `run().run_id`; no-ops when `run()` is null. Never navigates to `/persona-testing`.

- [ ] **Step 1: Write the failing tests**

In `agent-studio-persona.component.spec.ts`, add `provideRouter([])` to the `build()` `providers` array (the component will inject `Router`). Import `Router` from `@angular/router` and `vi` is already imported.

Add:

```typescript
  it('does not show View full audit when there is no current run', () => {
    build();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).not.toContain('View full audit');
  });

  it('shows View full audit after a run exists and navigates to the Studio audit route', () => {
    build();
    fixture.detectChanges();
    const router = TestBed.inject(Router);
    const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    component.launch();
    fixture.detectChanges();
    expect(component.run()).toBeTruthy();
    const btn = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>,
    ).find((b) => b.textContent?.includes('View full audit'));
    expect(btn).toBeTruthy();
    btn!.click();
    expect(nav).toHaveBeenCalledWith(['/agent-studio', 'persona-run', 'run-1']);
    expect(nav.mock.calls.some((c) => String(c[0]).includes('persona-testing'))).toBe(false);
  });

  it('openFullAudit is a no-op when there is no run', () => {
    build();
    fixture.detectChanges();
    const router = TestBed.inject(Router);
    const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    component.openFullAudit();
    expect(nav).not.toHaveBeenCalled();
  });
```

Default `getRunStatus` already returns `{ run_id: 'run-1', status: 'completed', decisions: [] }`, and `launch()` uses `job_id: 'run-1'`, so the first test after launch has a run.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd user-interface && npx vitest run src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.spec.ts
```

Expected: FAIL — `openFullAudit` is not a function; “View full audit” is absent after launch.

- [ ] **Step 3: Write minimal implementation**

In `agent-studio-persona.component.ts`, import `Router` from `@angular/router` and inject it:

```typescript
  private readonly router = inject(Router);
```

Add:

```typescript
  /**
   * Open the full audit view for the current persona run inside Studio.
   *
   * Preconditions: none (safe to call with no run).
   * Postconditions: when `run()` is set, navigates to
   *   `/agent-studio/persona-run/:runId` with that run's `run_id`. When `run()`
   *   is null, does not navigate.
   */
  openFullAudit(): void {
    const id = this.run()?.run_id;
    if (!id) return;
    void this.router.navigate(['/agent-studio', 'persona-run', id]);
  }
```

In `agent-studio-persona.component.html`, inside `.persona__run-head`, wrap the existing Stop button and the new control:

```html
                <div class="persona__run-actions">
                @if (runInProgress()) {
                  <button
                    mat-stroked-button
                    type="button"
                    class="persona__stop"
                    [disabled]="cancelling()"
                    (click)="stopRun()"
                  >
                    <mat-icon aria-hidden="true">stop</mat-icon>
                    {{ cancelling() ? 'Stopping…' : 'Stop run' }}
                  </button>
                }
                  <button
                    mat-stroked-button
                    type="button"
                    class="persona__audit"
                    (click)="openFullAudit()"
                  >
                    <mat-icon aria-hidden="true">open_in_new</mat-icon>
                    View full audit
                  </button>
                </div>
```

This block is already inside `@if (run(); as r)`, so the button is absent when there is no run.

In `agent-studio-persona.component.scss`, replace `&__stop { margin-inline-start: auto; }` with:

```scss
  &__run-actions {
    display: inline-flex;
    align-items: center;
    gap: var(--kh-space-2);
    margin-inline-start: auto;
  }
```

Keep `&__stop` if other rules exist; do not leave `margin-inline-start: auto` on `&__stop` or it will fight the group.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd user-interface && npx vitest run \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.a11y.spec.ts
```

Expected: PASS (a11y spec still compiles; add `provideRouter([])` there too if it constructs the persona component and now fails on missing `Router`).

If `agent-studio-persona.component.a11y.spec.ts` injects the component, add `provideRouter([])` to its `providers` the same way.

Then run coverage on the touched files:

```bash
cd user-interface && npx vitest run --coverage \
  src/app/components/agent-team-studio/persona-test-audit-panel/persona-test-audit-panel.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona-audit.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-stage-host.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-shell.component.spec.ts \
  src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.spec.ts \
  src/app/app.routes.spec.ts
```

Expected: line coverage ≥ 90% on each new/changed component file. If `childHidesFooter`’s `while (child?.firstChild)` branch is uncovered, add a nested-child case to the shell harness test rather than a pragma.

- [ ] **Step 5: Commit**

```bash
git add user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.html \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.scss \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.spec.ts \
        user-interface/src/app/components/agent-team-studio/agent-studio-shell/agent-studio-persona.component.a11y.spec.ts
git commit -m "$(cat <<'EOF'
Add a Stage 4 View full audit control that opens the nested Studio audit route instead of the Testing Personas dashboard.

EOF
)"
```

---

## Self-review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| Nested `/agent-studio/persona-run/:runId` | 3 |
| Default child is the four-stage switch | 3 |
| Wrapper mounts existing audit panel | 2 |
| Wrapper `navigateToStage` → Personas (index 3) | 2 |
| `backLink` / `backLabel` inputs; old defaults | 1 |
| Studio back inputs `/agent-studio` / “Back to Agent Studio” | 2 |
| No second Back control on the wrapper | 2 |
| Header + stepper stay; continue footer hidden | 3 |
| State providers stay on the shell | 3 |
| Stage 4 keeps inline live-run | 4 (additive button only) |
| **View full audit** when `run()` set; uses `run_id` | 4 |
| Never navigate to `/persona-testing` from Studio | 4 |
| Old `/persona-testing/audit/:runId` unchanged | 3 |
| Missing `:runId` / fetch errors stay on the panel | 1 (no new error UI) |
| Deep-link back can land on empty Stage 4 | existing Stage 4 empty state |
| Tests listed in spec Testing section | 1–4 |

**Placeholder scan:** none.

**Type consistency:** `hideStudioFooter` route data, `backLink` / `backLabel` inputs, `STAGE_PERSONAS = 3`, `openFullAudit()`, selector `app-agent-studio-persona-audit`, path `persona-run/:runId` are used the same way in every task.
