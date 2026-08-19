import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentCatalogApiService } from '../../../../services/agent-catalog-api.service';
import type { AgentDetail, AgentSummary, TeamGroup } from '../../../../models/agent-catalog.model';
import { AgentCatalogComponent } from './agent-catalog.component';

interface CatalogApiMock {
  listAgents: ReturnType<typeof vi.fn>;
  listTeams: ReturnType<typeof vi.fn>;
  getAgent: ReturnType<typeof vi.fn>;
  getInputSchema: ReturnType<typeof vi.fn>;
  getOutputSchema: ReturnType<typeof vi.fn>;
}

describe('AgentCatalogComponent', () => {
  const writer: AgentSummary = {
    id: 'blogging.writer',
    team: 'blogging',
    name: 'Writer',
    summary: 'Drafts long-form posts.',
    tags: ['content', 'drafting'],
    has_input_schema: true,
    has_output_schema: true,
    has_invoke: true,
    has_sandbox: false,
    has_cognition: false,
    has_knowledge_graph: false,
  };
  const auditor: AgentSummary = {
    id: 'soc2.auditor',
    team: 'soc2',
    name: 'Auditor',
    summary: 'Runs compliance checks.',
    tags: ['compliance', 'requires-live-integration'],
    has_input_schema: false,
    has_output_schema: false,
    has_invoke: false,
    has_sandbox: false,
    has_cognition: false,
    has_knowledge_graph: false,
  };
  const bloggingGroup: TeamGroup = { team: 'blogging', display_name: 'Blogging', agent_count: 1, tags: ['content'] };
  const soc2Group: TeamGroup = { team: 'soc2', display_name: 'SOC2 Compliance', agent_count: 1, tags: ['compliance'] };

  const writerDetail: AgentDetail = {
    manifest: {
      schema_version: 1,
      id: 'blogging.writer',
      team: 'blogging',
      name: 'Writer',
      summary: 'Drafts long-form posts.',
      description: 'Writes and edits long-form blog drafts.',
      tags: ['content', 'drafting'],
      inputs: { schema_ref: 'writer.input' },
      outputs: { schema_ref: 'writer.output' },
      invoke: { kind: 'http', method: 'POST', path: '/invoke' },
      sandbox: { manifest_path: 'writer/sandbox.yaml' },
      source: { entrypoint: 'writer.py' },
    },
    anatomy_markdown: '# Writer',
  };

  const auditorDetail: AgentDetail = {
    ...writerDetail,
    manifest: {
      ...writerDetail.manifest,
      id: 'soc2.auditor',
      team: 'soc2',
      name: 'Auditor',
      tags: ['compliance', 'requires-live-integration'],
    },
  };

  let api: CatalogApiMock;

  const setup = async (overrides: Partial<CatalogApiMock> = {}) => {
    api = {
      listAgents: vi.fn().mockReturnValue(of([writer, auditor])),
      listTeams: vi.fn().mockReturnValue(of([bloggingGroup, soc2Group])),
      getAgent: vi.fn().mockReturnValue(of(writerDetail)),
      getInputSchema: vi.fn(),
      getOutputSchema: vi.fn(),
      ...overrides,
    };
    await TestBed.configureTestingModule({
      imports: [AgentCatalogComponent, NoopAnimationsModule],
      providers: [{ provide: AgentCatalogApiService, useValue: api }],
    }).compileComponents();
    const fixture = TestBed.createComponent(AgentCatalogComponent);
    const component = fixture.componentInstance;
    fixture.detectChanges();
    return { fixture, component };
  };

  afterEach(() => {
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  it('loads agents and teams on init', async () => {
    const { component } = await setup();
    expect(component.agents()).toEqual([writer, auditor]);
    expect(component.teams()).toEqual([bloggingGroup, soc2Group]);
    expect(component.loading()).toBe(false);
    expect(component.error()).toBeNull();
  });

  it('logs and swallows a listTeams failure without touching agents', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { component } = await setup({
      listAgents: vi.fn().mockReturnValue(of([writer])),
      listTeams: vi.fn().mockReturnValue(throwError(() => new Error('teams down'))),
    });

    expect(component.agents()).toEqual([writer]);
    expect(component.teams()).toEqual([]);
    expect(consoleSpy).toHaveBeenCalledWith('Failed to load teams', expect.any(Error));
  });

  it('unsubscribes the in-flight refresh request on destroy', async () => {
    const pending$ = new Subject<AgentSummary[]>();
    const { component, fixture } = await setup({ listAgents: vi.fn().mockReturnValue(pending$) });

    fixture.destroy();
    pending$.next([writer]); // emitted after destroy — must be ignored

    expect(component.agents()).toEqual([]);
  });

  it('cancels a stale in-flight request so only the latest filter result lands', async () => {
    const first$ = new Subject<AgentSummary[]>();
    const second$ = new Subject<AgentSummary[]>();
    const { component } = await setup({
      listAgents: vi.fn().mockReturnValueOnce(first$).mockReturnValueOnce(second$),
      listTeams: vi.fn().mockReturnValue(of([])),
    });

    component.onSearchChange('writer'); // refresh() unsubscribes first$, subscribes second$

    first$.next([auditor]); // stale — must be ignored
    second$.next([writer]); // latest — must win

    expect(component.agents()).toEqual([writer]);
  });

  it('onSearchChange sets the query filter and re-fetches with it', async () => {
    const { component } = await setup();
    component.onSearchChange('writer');
    expect(component.query()).toBe('writer');
    expect(api.listAgents).toHaveBeenLastCalledWith({ team: undefined, tag: undefined, q: 'writer' });
  });

  it('onTeamChange sets the team filter and re-fetches with it', async () => {
    const { component } = await setup();
    component.onTeamChange('blogging');
    expect(component.selectedTeam()).toBe('blogging');
    expect(api.listAgents).toHaveBeenLastCalledWith({ team: 'blogging', tag: undefined, q: undefined });
  });

  it('onTagToggle selects a tag, then toggling the same tag again clears it', async () => {
    const { component } = await setup();
    component.onTagToggle('content');
    expect(component.selectedTag()).toBe('content');
    expect(api.listAgents).toHaveBeenLastCalledWith({ team: undefined, tag: 'content', q: undefined });

    component.onTagToggle('content');
    expect(component.selectedTag()).toBeNull();
    expect(api.listAgents).toHaveBeenLastCalledWith({ team: undefined, tag: undefined, q: undefined });
  });

  it('clearFilters resets query, team, and tag and re-fetches', async () => {
    const { component } = await setup();
    component.onSearchChange('writer');
    component.onTeamChange('blogging');
    component.onTagToggle('content');

    component.clearFilters();

    expect(component.query()).toBe('');
    expect(component.selectedTeam()).toBeNull();
    expect(component.selectedTag()).toBeNull();
    expect(api.listAgents).toHaveBeenLastCalledWith({ team: undefined, tag: undefined, q: undefined });
  });

  it('sets a server-provided error message when refresh fails', async () => {
    const { component } = await setup({
      listAgents: vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'registry unavailable' } }))),
      listTeams: vi.fn().mockReturnValue(of([])),
    });

    expect(component.error()).toBe('registry unavailable');
    expect(component.loading()).toBe(false);
  });

  it('falls back to err.message when there is no error.detail', async () => {
    const { component } = await setup({
      listAgents: vi.fn().mockReturnValue(throwError(() => ({ message: 'network down' }))),
      listTeams: vi.fn().mockReturnValue(of([])),
    });

    expect(component.error()).toBe('network down');
  });

  it('falls back to a generic message when the error has neither detail nor message', async () => {
    const { component } = await setup({
      listAgents: vi.fn().mockReturnValue(throwError(() => ({}))),
      listTeams: vi.fn().mockReturnValue(of([])),
    });

    expect(component.error()).toBe('Failed to load agents');
  });

  it('openDetail opens the drawer and loads detail', async () => {
    const { component } = await setup();
    component.openDetail(writer);

    expect(component.drawerOpen()).toBe(true);
    expect(component.detailLoading()).toBe(false);
    expect(component.selectedDetail()).toEqual(writerDetail);
    expect(api.getAgent).toHaveBeenCalledWith('blogging.writer');
  });

  it('openDetail logs and clears detailLoading when getAgent fails', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { component } = await setup();
    api.getAgent.mockReturnValue(throwError(() => new Error('boom')));

    component.openDetail(writer);

    expect(component.detailLoading()).toBe(false);
    expect(consoleSpy).toHaveBeenCalledWith('Failed to load agent detail', expect.any(Error));
  });

  it('closeDetail hides the drawer and clears the selected detail', async () => {
    const { component } = await setup();
    component.openDetail(writer);
    component.closeDetail();

    expect(component.drawerOpen()).toBe(false);
    expect(component.selectedDetail()).toBeNull();
  });

  it('runFromDrawer emits requestRun with the manifest id and closes the drawer', async () => {
    const { component } = await setup();
    const emitted: string[] = [];
    component.requestRun.subscribe((id) => emitted.push(id));

    component.openDetail(writer);
    component.runFromDrawer();

    expect(emitted).toEqual(['blogging.writer']);
    expect(component.drawerOpen()).toBe(false);
  });

  it('runFromDrawer is a no-op when nothing is selected', async () => {
    const { component } = await setup();
    const emitted: string[] = [];
    component.requestRun.subscribe((id) => emitted.push(id));

    component.runFromDrawer();

    expect(emitted).toEqual([]);
  });

  it('requiresLiveIntegration reflects the selected detail, and is false with nothing selected', async () => {
    const { component } = await setup();
    expect(component.requiresLiveIntegration()).toBe(false);

    component.openDetail(writer);
    expect(component.requiresLiveIntegration()).toBe(false);

    api.getAgent.mockReturnValue(of(auditorDetail));
    component.openDetail(auditor);
    expect(component.requiresLiveIntegration()).toBe(true);
  });

  it('teamDisplayName resolves a known team and falls back to the raw key otherwise', async () => {
    const { component } = await setup();
    expect(component.teamDisplayName('blogging')).toBe('Blogging');
    expect(component.teamDisplayName('unknown-team')).toBe('unknown-team');
  });

  it('allTags dedupes and sorts tags across the current agent list', async () => {
    const { component } = await setup();
    expect(component.allTags()).toEqual(['compliance', 'content', 'drafting', 'requires-live-integration']);
  });

  it('trackAgent and trackTeam key by id/team', async () => {
    const { component } = await setup();
    expect(component.trackAgent(0, writer)).toBe('blogging.writer');
    expect(component.trackTeam(0, bloggingGroup)).toBe('blogging');
  });
});
