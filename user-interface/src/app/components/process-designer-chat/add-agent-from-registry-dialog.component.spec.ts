import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import {
  AddAgentFromRegistryDialogComponent,
  AddAgentFromRegistryDialogData,
} from './add-agent-from-registry-dialog.component';
import { AgentCatalogApiService } from '../../services/agent-catalog-api.service';
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

function configure(data: AddAgentFromRegistryDialogData, listAgents = vi.fn().mockReturnValue(of([]))) {
  const ref = { close: vi.fn() };
  const api = { listAgents };
  TestBed.configureTestingModule({
    imports: [AddAgentFromRegistryDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
      { provide: AgentCatalogApiService, useValue: api },
    ],
  });
  const fixture = TestBed.createComponent(AddAgentFromRegistryDialogComponent);
  return { fixture, ref, api };
}

describe('AddAgentFromRegistryDialogComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('searches on init with an empty query', () => {
    const listAgents = vi.fn().mockReturnValue(of([summary('a.1')]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    expect(listAgents).toHaveBeenCalledWith({});
    expect(fixture.componentInstance.results()).toHaveLength(1);
  });

  it('re-searches with the trimmed query on change', () => {
    const listAgents = vi.fn().mockReturnValue(of([]));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    fixture.componentInstance.onQueryChange('  seo  ');
    expect(listAgents).toHaveBeenLastCalledWith({ q: 'seo' });
  });

  it('closes with the chosen manifest id', () => {
    const listAgents = vi.fn().mockReturnValue(of([summary('a.1', 'Planner')]));
    const { fixture, ref } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    fixture.componentInstance.choose(summary('a.1', 'Planner'));
    expect(ref.close).toHaveBeenCalledWith('a.1');
  });

  it('does not close when choosing an agent already on the roster', () => {
    const listAgents = vi.fn().mockReturnValue(of([summary('a.1')]));
    const { fixture, ref } = configure({ existingManifestIds: ['a.1'] }, listAgents);
    fixture.detectChanges();
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

  it('surfaces a search error', () => {
    const listAgents = vi.fn().mockReturnValue(throwError(() => new Error('boom')));
    const { fixture } = configure({ existingManifestIds: [] }, listAgents);
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('Could not search the agent catalog.');
    expect(fixture.componentInstance.loading()).toBe(false);
  });
});
