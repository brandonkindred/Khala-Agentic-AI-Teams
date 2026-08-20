import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { Subject, of, throwError } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  AddAgentFromRegistryDialogComponent,
  AddAgentFromRegistryDialogData,
  SEARCH_DEBOUNCE_MS,
} from './add-agent-from-registry-dialog.component';
import { AgentConsoleApiService } from '../../services/agent-console-api.service';
import type { AgentSummary } from '../../models/agent-catalog.model';

const summary = (id: string, name = id): AgentSummary => ({
  id,
  team: 't',
  name,
  summary: `${name} summary`,
  tags: [],
  has_input_schema: false,
  has_output_schema: false,
  has_invoke: false,
  has_sandbox: false,
  has_cognition: false,
  has_knowledge_graph: false,
});

// Real-timer flush of the debounce window. We use real timers (not
// vi.useFakeTimers / fakeAsync) because faking timers here breaks Angular
// TestBed's async component-resource resolution and leaks across spec files.
const flush = () => new Promise((r) => setTimeout(r, SEARCH_DEBOUNCE_MS + 20));

function configure(data: AddAgentFromRegistryDialogData, listAgents = vi.fn().mockReturnValue(of([]))) {
  const ref = { close: vi.fn() };
  const api = { listAgents };
  TestBed.configureTestingModule({
    imports: [AddAgentFromRegistryDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
      { provide: AgentConsoleApiService, useValue: api },
    ],
  });
  const fixture = TestBed.createComponent(AddAgentFromRegistryDialogComponent);
  return { fixture, ref, api };
}

describe('AddAgentFromRegistryDialogComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('searches on init with an empty query (after debounce)', async () => {
    const listAgents = vi.fn().mockReturnValue(of([summary('a.1')]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    expect(listAgents).not.toHaveBeenCalled(); // debounced, not yet fired
    await flush();
    expect(listAgents).toHaveBeenCalledWith({});
    expect(fixture.componentInstance.results()).toHaveLength(1);
  });

  it('filters out generated (roster-owned) agents from the results', async () => {
    // A generated agent is rejected by the from-registry endpoint (it belongs to a
    // team's roster), so it must never appear as addable here.
    const listAgents = vi.fn().mockReturnValue(
      of([summary('reg.1'), { ...summary('gen.1'), tags: ['generated', 'agentic_team_provisioning'] }]),
    );
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    await flush();
    expect(fixture.componentInstance.results().map((r) => r.id)).toEqual(['reg.1']);
  });

  it('shows loading immediately on open (not the empty state) before the debounce fires', () => {
    const listAgents = vi.fn().mockReturnValue(of([summary('a.1')]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges(); // ngOnInit → search()
    // loading is set synchronously in search(), before the debounced request fires,
    // so the template shows the spinner rather than the "no matches" empty state.
    expect(fixture.componentInstance.loading()).toBe(true);
    expect(listAgents).not.toHaveBeenCalled();
  });

  it('clears a stale error synchronously when a new search starts', async () => {
    const listAgents = vi
      .fn()
      .mockReturnValueOnce(throwError(() => new Error('boom')))
      .mockReturnValueOnce(of([summary('a.1')]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    const c = fixture.componentInstance;
    fixture.detectChanges();
    await flush(); // init search errors
    expect(c.error()).toBe('Could not search the agent catalog.');

    c.onQueryChange('x'); // a new search must drop the stale banner immediately
    expect(c.error()).toBeNull();
  });

  it('re-searches with the trimmed query on change', async () => {
    const listAgents = vi.fn().mockReturnValue(of([]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    await flush();
    fixture.componentInstance.onQueryChange('  seo  ');
    await flush();
    expect(listAgents).toHaveBeenLastCalledWith({ q: 'seo' });
  });

  it('debounces a burst of keystrokes into a single request', async () => {
    const listAgents = vi.fn().mockReturnValue(of([]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    await flush(); // flush the init search
    listAgents.mockClear();

    const c = fixture.componentInstance;
    c.onQueryChange('s');
    c.onQueryChange('se');
    c.onQueryChange('seo');
    await flush();
    // Only the final query fires a request, not one per keystroke.
    expect(listAgents).toHaveBeenCalledTimes(1);
    expect(listAgents).toHaveBeenCalledWith({ q: 'seo' });
  });

  it('a slow earlier response cannot clobber a newer query (switchMap cancels it)', async () => {
    // First query resolves via a Subject we control; the second resolves
    // immediately. switchMap must unsubscribe the first so its late emission
    // is dropped rather than overwriting the newer results.
    const slow = new Subject<AgentSummary[]>();
    const listAgents = vi
      .fn()
      .mockReturnValueOnce(slow.asObservable())
      .mockReturnValueOnce(of([summary('new.1', 'New')]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    const c = fixture.componentInstance;

    c.onQueryChange('a');
    await flush(); // fires the 'a' request (pending on `slow`)
    c.onQueryChange('ab');
    await flush(); // fires 'ab' → resolves to New; cancels 'a'

    // The stale 'a' response arrives late — it must be ignored.
    slow.next([summary('stale.1', 'Stale')]);
    slow.complete();

    expect(c.results().map((r) => r.id)).toEqual(['new.1']);
  });

  it('closes with the chosen manifest id', async () => {
    const listAgents = vi.fn().mockReturnValue(of([summary('a.1', 'Planner')]));
    const { fixture, ref } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    await flush();
    fixture.componentInstance.choose(summary('a.1', 'Planner'));
    expect(ref.close).toHaveBeenCalledWith('a.1');
  });

  it('does not close when choosing an agent already on the roster', async () => {
    const listAgents = vi.fn().mockReturnValue(of([summary('a.1')]));
    const { fixture, ref } = configure({ existingManifestIds: ['a.1'] }, listAgents);
    fixture.detectChanges();
    await flush();
    expect(fixture.componentInstance.isAlreadyOnRoster('a.1')).toBe(true);
    fixture.componentInstance.choose(summary('a.1'));
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('cancel closes with no result', () => {
    const { fixture, ref } = configure({ existingManifestIds: [] });
    fixture.detectChanges();
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });

  it('surfaces a search error and keeps the prior results', async () => {
    const listAgents = vi
      .fn()
      .mockReturnValueOnce(of([summary('a.1')]))
      .mockReturnValueOnce(throwError(() => new Error('boom')));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    const c = fixture.componentInstance;
    fixture.detectChanges();
    await flush(); // init search succeeds → one result
    expect(c.results()).toHaveLength(1);

    c.onQueryChange('zzz');
    await flush(); // this search errors
    expect(c.error()).toBe('Could not search the agent catalog.');
    expect(c.loading()).toBe(false);
    // Prior results are preserved rather than blanked on error.
    expect(c.results()).toHaveLength(1);
  });
});
