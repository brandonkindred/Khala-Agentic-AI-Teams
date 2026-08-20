import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse, HttpResponse } from '@angular/common/http';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentCatalogApiService } from '../../../../services/agent-catalog-api.service';
import { AgentRunnerApiService } from '../../../../services/agent-runner-api.service';
import { ConfirmDestructiveService } from '../../../../shared/confirm-destructive.service';
import type { AgentDetail, AgentSummary } from '../../../../models/agent-catalog.model';
import type { InvokeEnvelope, SandboxHandle } from '../../../../models/agent-runner.model';
import type { RunRecord, RunSummary, SavedInput } from '../../../../models/agent-history.model';
import { AgentDiffDialogComponent } from '../agent-diff-dialog/agent-diff-dialog.component';
import { AgentRunnerComponent } from './agent-runner.component';

describe('AgentRunnerComponent', () => {
  const writerSummary: AgentSummary = {
    id: 'blogging.writer',
    team: 'blogging',
    name: 'Writer',
    summary: 'Drafts long-form posts.',
    tags: ['content'],
    has_input_schema: true,
    has_output_schema: true,
    has_invoke: true,
    has_sandbox: true,
    has_cognition: false,
    has_knowledge_graph: false,
  };

  const writerDetail: AgentDetail = {
    manifest: {
      schema_version: 1,
      id: 'blogging.writer',
      team: 'blogging',
      name: 'Writer',
      summary: 'Drafts long-form posts.',
      description: 'Writes drafts.',
      tags: ['content'],
      inputs: { schema_ref: 'writer.input' },
      outputs: { schema_ref: 'writer.output' },
      invoke: { kind: 'http', method: 'POST', path: '/invoke' },
      sandbox: { manifest_path: 'writer/sandbox.yaml' },
      source: { entrypoint: 'writer.py' },
    },
    anatomy_markdown: '# Writer',
  };

  const liveIntegrationDetail: AgentDetail = {
    ...writerDetail,
    manifest: {
      ...writerDetail.manifest,
      id: 'soc2.auditor',
      team: 'soc2',
      name: 'Auditor',
      tags: ['requires-live-integration'],
    },
  };

  const coldHandle: SandboxHandle = {
    agent_id: 'blogging.writer',
    team: 'blogging',
    status: 'cold',
    url: null,
    service_name: 'writer-svc',
    container_name: 'writer-container',
    host_port: 8100,
    idle_seconds: null,
  };

  const warmHandle: SandboxHandle = { ...coldHandle, status: 'warm', url: 'http://sandbox/writer' };

  const savedInput: SavedInput = {
    id: 'saved-1',
    agent_id: 'blogging.writer',
    name: 'Happy path',
    input_data: { topic: 'launch' },
    author: 'brandon',
    description: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  };

  const runSummary: RunSummary = {
    id: 'run-1',
    agent_id: 'blogging.writer',
    team: 'blogging',
    saved_input_id: null,
    status: 'ok',
    duration_ms: 120,
    trace_id: 'trace-1234567890',
    author: 'brandon',
    created_at: '2026-08-01T00:05:00Z',
  };

  interface CatalogApiMock {
    listAgents: ReturnType<typeof vi.fn>;
    getAgent: ReturnType<typeof vi.fn>;
    getInputSchema: ReturnType<typeof vi.fn>;
  }

  interface RunnerApiMock {
    ensureWarm: ReturnType<typeof vi.fn>;
    getSandbox: ReturnType<typeof vi.fn>;
    teardown: ReturnType<typeof vi.fn>;
    invoke: ReturnType<typeof vi.fn>;
    listSamples: ReturnType<typeof vi.fn>;
    getSample: ReturnType<typeof vi.fn>;
    listSavedInputs: ReturnType<typeof vi.fn>;
    createSavedInput: ReturnType<typeof vi.fn>;
    deleteSavedInput: ReturnType<typeof vi.fn>;
    listRuns: ReturnType<typeof vi.fn>;
    getRun: ReturnType<typeof vi.fn>;
    deleteRun: ReturnType<typeof vi.fn>;
    diff: ReturnType<typeof vi.fn>;
  }

  let catalogApi: CatalogApiMock;
  let runnerApi: RunnerApiMock;
  let dialogOpen: ReturnType<typeof vi.fn>;
  let confirmServiceConfirm: ReturnType<typeof vi.fn>;
  let fixture: ComponentFixture<AgentRunnerComponent>;
  let component: AgentRunnerComponent;

  const setup = async (catalogOverrides: Partial<CatalogApiMock> = {}) => {
    TestBed.resetTestingModule();
    catalogApi = {
      listAgents: vi.fn().mockReturnValue(of([writerSummary])),
      getAgent: vi.fn().mockReturnValue(of(writerDetail)),
      getInputSchema: vi.fn().mockReturnValue(throwError(() => new Error('no schema'))),
      ...catalogOverrides,
    };
    runnerApi = {
      ensureWarm: vi.fn().mockReturnValue(of(warmHandle)),
      getSandbox: vi.fn().mockReturnValue(of(coldHandle)),
      teardown: vi.fn().mockReturnValue(of({ agent_id: 'blogging.writer', status: 'stopped' })),
      invoke: vi.fn(),
      listSamples: vi.fn().mockReturnValue(of([])),
      getSample: vi.fn(),
      listSavedInputs: vi.fn().mockReturnValue(of([])),
      createSavedInput: vi.fn(),
      deleteSavedInput: vi.fn(),
      listRuns: vi.fn().mockReturnValue(of([])),
      getRun: vi.fn(),
      deleteRun: vi.fn(),
      diff: vi.fn(),
    };
    dialogOpen = vi.fn();
    confirmServiceConfirm = vi.fn();

    TestBed.configureTestingModule({
      imports: [AgentRunnerComponent, NoopAnimationsModule],
      providers: [
        { provide: AgentCatalogApiService, useValue: catalogApi },
        { provide: AgentRunnerApiService, useValue: runnerApi },
      ],
    });
    TestBed.overrideProvider(MatDialog, { useValue: { open: dialogOpen } });
    TestBed.overrideProvider(ConfirmDestructiveService, {
      useValue: { confirm: confirmServiceConfirm },
    });
    await TestBed.compileComponents();

    fixture = TestBed.createComponent(AgentRunnerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  };

  const selectWriter = () => {
    component.onAgentChange('blogging.writer');
    fixture.detectChanges();
  };

  beforeEach(setup);
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('loads the agent list on init', () => {
    expect(component.agents()).toEqual([writerSummary]);
  });

  it('logs and swallows a failure to load the agent list', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    await setup({ listAgents: vi.fn().mockReturnValue(throwError(() => new Error('down'))) });

    expect(component.agents()).toEqual([]);
    expect(consoleSpy).toHaveBeenCalledWith('Runner: failed to load agents', expect.any(Error));
  });

  describe('preselectedAgentId input', () => {
    it('loads the agent detail when set to a new id', () => {
      component.preselectedAgentId = 'blogging.writer';
      expect(component.selectedAgentId()).toBe('blogging.writer');
      expect(catalogApi.getAgent).toHaveBeenCalledWith('blogging.writer');
    });

    it('is a no-op when set to null', () => {
      component.preselectedAgentId = null;
      expect(component.selectedAgentId()).toBeNull();
      expect(catalogApi.getAgent).not.toHaveBeenCalled();
    });

    it('is a no-op when set to the id already selected', () => {
      component.preselectedAgentId = 'blogging.writer';
      catalogApi.getAgent.mockClear();
      component.preselectedAgentId = 'blogging.writer';
      expect(catalogApi.getAgent).not.toHaveBeenCalled();
    });
  });

  describe('onAgentChange', () => {
    it('resets transient state and loads detail when given an id', () => {
      component.lastResponse.set({ output: {}, duration_ms: 1, trace_id: 't', logs_tail: [] });
      component.activeRunId.set('run-x');

      selectWriter();

      expect(component.selectedAgent()).toEqual(writerDetail);
      expect(component.lastResponse()).toBeNull();
      expect(component.activeRunId()).toBeNull();
      expect(component.editorMode()).toBe('json');
    });

    it('resets state and stays empty when given null', () => {
      selectWriter();
      component.onAgentChange(null);

      expect(component.selectedAgent()).toBeNull();
      expect(component.selectedAgentId()).toBeNull();
      expect(component.sandbox()).toBeNull();
    });
  });

  it('logs and surfaces an error when loading agent detail fails', () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    catalogApi.getAgent.mockReturnValue(throwError(() => new Error('missing')));

    selectWriter();

    expect(component.lastError()).toBe('Could not load agent detail.');
    expect(consoleSpy).toHaveBeenCalledWith('Failed to load agent detail', expect.any(Error));
  });

  describe('golden samples', () => {
    it('auto-applies the first sample when none is picked yet', () => {
      runnerApi.listSamples.mockReturnValue(of(['sample-a', 'sample-b']));
      runnerApi.getSample.mockReturnValue(of({ topic: 'from sample' }));

      selectWriter();

      expect(component.selectedPickerValue()).toBe('golden:sample-a');
      expect(runnerApi.getSample).toHaveBeenCalledWith('blogging.writer', 'sample-a');
      expect(component.inputText()).toBe(JSON.stringify({ topic: 'from sample' }, null, 2));
    });

    it('clears golden samples on a load failure', () => {
      runnerApi.listSamples.mockReturnValue(throwError(() => new Error('down')));
      selectWriter();
      expect(component.goldenSamples()).toEqual([]);
    });
  });

  it('clears saved inputs on a load failure', () => {
    runnerApi.listSavedInputs.mockReturnValue(throwError(() => new Error('down')));
    selectWriter();
    expect(component.savedInputs()).toEqual([]);
  });

  describe('input schema', () => {
    it('switches to form mode when a schema loads', () => {
      catalogApi.getInputSchema.mockReturnValue(of({ type: 'object', properties: {} }));
      selectWriter();
      expect(component.inputSchema()).toEqual({ type: 'object', properties: {} });
      expect(component.editorMode()).toBe('form');
    });

    it('falls back to json mode when the schema fails to load', () => {
      selectWriter();
      expect(component.inputSchema()).toBeNull();
      expect(component.editorMode()).toBe('json');
    });
  });

  describe('onPickerChange', () => {
    beforeEach(() => selectWriter());

    it('clears the picker on a null value', () => {
      component.selectedPickerValue.set('golden:sample-a');
      component.onPickerChange(null);
      expect(component.selectedPickerValue()).toBeNull();
    });

    it('applies a golden sample', () => {
      runnerApi.getSample.mockReturnValue(of({ topic: 'golden' }));
      component.onPickerChange('golden:sample-a');
      expect(runnerApi.getSample).toHaveBeenCalledWith('blogging.writer', 'sample-a');
      expect(component.selectedPickerValue()).toBe('golden:sample-a');
    });

    it('applies a matching saved input', () => {
      component.savedInputs.set([savedInput]);
      component.onPickerChange(`saved:${savedInput.id}`);
      expect(component.selectedPickerValue()).toBe(`saved:${savedInput.id}`);
      expect(component.inputText()).toBe(JSON.stringify(savedInput.input_data, null, 2));
    });

    it('is a no-op for a saved id with no match', () => {
      component.savedInputs.set([]);
      const before = component.inputText();
      component.onPickerChange('saved:missing');
      expect(component.selectedPickerValue()).toBeNull();
      expect(component.inputText()).toBe(before);
    });
  });

  describe('input editing', () => {
    beforeEach(() => selectWriter());

    it('resetInput clears text, picker, and error', () => {
      component.inputText.set('{"a":1}');
      component.selectedPickerValue.set('golden:sample-a');
      component.inputError.set('bad json');

      component.resetInput();

      expect(component.inputText()).toBe('{}');
      expect(component.selectedPickerValue()).toBeNull();
      expect(component.inputError()).toBeNull();
    });

    it('onInputTextChange accepts valid JSON', () => {
      component.onInputTextChange('{"a":1}');
      expect(component.inputError()).toBeNull();
    });

    it('onInputTextChange records a parse error for invalid JSON', () => {
      component.onInputTextChange('{not json');
      expect(component.inputError()).toContain('JSON');
    });

    it('onFormValueChange serialises the form value into inputText', () => {
      component.onFormValueChange({ topic: 'x' });
      expect(component.inputText()).toBe(JSON.stringify({ topic: 'x' }, null, 2));
      expect(component.inputError()).toBeNull();
    });
  });

  describe('saved inputs — save', () => {
    beforeEach(() => selectWriter());

    it('does not open the dialog when the current input is invalid JSON', () => {
      component.inputText.set('{not json');
      component.openSaveInputDialog();
      expect(dialogOpen).not.toHaveBeenCalled();
      expect(component.inputError()).toContain('JSON');
    });

    it('is a no-op on a cancelled dialog', () => {
      dialogOpen.mockReturnValue({ afterClosed: () => of(undefined) });
      component.openSaveInputDialog();
      expect(runnerApi.createSavedInput).not.toHaveBeenCalled();
    });

    it('creates and selects the saved input on confirm', () => {
      dialogOpen.mockReturnValue({ afterClosed: () => of({ name: 'New save', description: null }) });
      runnerApi.createSavedInput.mockReturnValue(of(savedInput));

      component.openSaveInputDialog();

      expect(runnerApi.createSavedInput).toHaveBeenCalledWith('blogging.writer', {
        name: 'New save',
        input_data: {},
        description: null,
      });
      expect(component.savedInputs()).toEqual([savedInput]);
      expect(component.selectedPickerValue()).toBe(`saved:${savedInput.id}`);
    });

    it('sets lastError when creating the saved input fails', () => {
      dialogOpen.mockReturnValue({ afterClosed: () => of({ name: 'New save', description: null }) });
      runnerApi.createSavedInput.mockReturnValue(throwError(() => ({ error: { detail: 'name taken' } })));

      component.openSaveInputDialog();

      expect(component.lastError()).toBe('name taken');
    });

    it('clears lastError on a successful save after a prior failure', () => {
      // Simulate a prior error state.
      component.lastError.set('prior failure');
      dialogOpen.mockReturnValue({ afterClosed: () => of({ name: 'New save', description: null }) });
      runnerApi.createSavedInput.mockReturnValue(of(savedInput));

      component.openSaveInputDialog();

      expect(component.lastError()).toBeNull();
    });
  });

  describe('saved inputs — delete', () => {
    beforeEach(() => selectWriter());

    it('does nothing for an id with no match', () => {
      component.deleteSavedInput('missing', new Event('click'));
      expect(confirmServiceConfirm).not.toHaveBeenCalled();
    });

    it('does nothing when the user cancels the confirm dialog', () => {
      component.savedInputs.set([savedInput]);
      confirmServiceConfirm.mockReturnValue(of(false));

      component.deleteSavedInput(savedInput.id, new Event('click'));

      expect(confirmServiceConfirm).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'danger', confirmLabel: 'Delete' }),
      );
      expect(runnerApi.deleteSavedInput).not.toHaveBeenCalled();
      expect(component.savedInputs()).toEqual([savedInput]);
    });

    it('removes the row and clears the picker when it was selected', () => {
      component.savedInputs.set([savedInput]);
      component.selectedPickerValue.set(`saved:${savedInput.id}`);
      confirmServiceConfirm.mockReturnValue(of(true));
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: savedInput.id, status: 'deleted' }));
      const event = new Event('click');
      const stopSpy = vi.spyOn(event, 'stopPropagation');

      component.deleteSavedInput(savedInput.id, event);

      expect(stopSpy).toHaveBeenCalled();
      expect(component.savedInputs()).toEqual([]);
      expect(component.selectedPickerValue()).toBeNull();
    });

    it('sets lastError when the delete API call fails', () => {
      component.savedInputs.set([savedInput]);
      confirmServiceConfirm.mockReturnValue(of(true));
      runnerApi.deleteSavedInput.mockReturnValue(
        throwError(() => ({ error: { detail: 'still referenced' } })),
      );

      component.deleteSavedInput(savedInput.id, new Event('click'));

      expect(component.lastError()).toBe('still referenced');
      // The row is NOT removed because the API call failed.
      expect(component.savedInputs()).toEqual([savedInput]);
    });

    it('clears lastError on a successful delete after a prior failure', () => {
      component.savedInputs.set([savedInput]);
      component.lastError.set('prior failure');
      confirmServiceConfirm.mockReturnValue(of(true));
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: savedInput.id, status: 'deleted' }));

      component.deleteSavedInput(savedInput.id, new Event('click'));

      expect(component.lastError()).toBeNull();
    });
  });

  describe('sandbox lifecycle', () => {
    it('seeds sandbox status via the initial poll', () => {
      selectWriter();
      expect(component.sandbox()).toEqual(coldHandle);
    });

    it('sets sandbox to null when the initial poll fails', () => {
      runnerApi.getSandbox.mockReturnValue(throwError(() => new Error('down')));
      selectWriter();
      expect(component.sandbox()).toBeNull();
    });

    it('re-polls sandbox status every 5 seconds', () => {
      vi.useFakeTimers();
      runnerApi.getSandbox.mockReturnValueOnce(of(coldHandle)).mockReturnValue(of(warmHandle));

      selectWriter();
      expect(component.sandbox()).toEqual(coldHandle);

      vi.advanceTimersByTime(5000);

      expect(runnerApi.getSandbox).toHaveBeenCalledTimes(2);
      expect(component.sandbox()).toEqual(warmHandle);
      vi.useRealTimers();
    });

    it('stops polling the sandbox after destroy', () => {
      vi.useFakeTimers();
      selectWriter();
      expect(runnerApi.getSandbox).toHaveBeenCalledTimes(1);

      fixture.destroy();
      vi.advanceTimersByTime(5000);

      expect(runnerApi.getSandbox).toHaveBeenCalledTimes(1);
      vi.useRealTimers();
    });

    it('is a no-op to warm/teardown when no agent is selected', () => {
      component.warmSandbox();
      component.tearDownSandbox();
      expect(runnerApi.ensureWarm).not.toHaveBeenCalled();
      expect(runnerApi.teardown).not.toHaveBeenCalled();
    });

    it('warmSandbox sets the sandbox handle and clears the polling flag', () => {
      selectWriter();
      component.warmSandbox();
      expect(component.sandbox()).toEqual(warmHandle);
      expect(component.sandboxPolling()).toBe(false);
    });

    it('warmSandbox logs and clears the polling flag on failure', () => {
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
      selectWriter();
      runnerApi.ensureWarm.mockReturnValue(throwError(() => new Error('warm failed')));

      component.warmSandbox();

      expect(consoleSpy).toHaveBeenCalledWith('ensureWarm failed', expect.any(Error));
      expect(component.sandboxPolling()).toBe(false);
    });

    it('tearDownSandbox does nothing when the user cancels the confirm dialog', () => {
      selectWriter();
      confirmServiceConfirm.mockReturnValue(of(false));
      component.tearDownSandbox();
      expect(confirmServiceConfirm).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'danger', confirmLabel: 'Tear Down' }),
      );
      expect(runnerApi.teardown).not.toHaveBeenCalled();
    });

    it('tearDownSandbox marks the sandbox cold on confirm', () => {
      selectWriter();
      confirmServiceConfirm.mockReturnValue(of(true));
      component.tearDownSandbox();
      expect(component.sandbox()).toEqual({ ...coldHandle, status: 'cold', url: null });
    });

    it('tearDownSandbox sets lastError on failure', () => {
      selectWriter();
      confirmServiceConfirm.mockReturnValue(of(true));
      runnerApi.teardown.mockReturnValue(throwError(() => ({ error: { detail: 'sandbox busy' } })));

      component.tearDownSandbox();

      expect(component.lastError()).toBe('sandbox busy');
    });
  });

  describe('run', () => {
    beforeEach(() => selectWriter());

    it('short-circuits on invalid JSON input', () => {
      component.inputText.set('{not json');
      component.run();
      expect(runnerApi.invoke).not.toHaveBeenCalled();
      expect(component.inputError()).toContain('JSON');
    });

    it('is a no-op when no agent is selected', () => {
      component.onAgentChange(null);
      component.run();
      expect(runnerApi.invoke).not.toHaveBeenCalled();
    });

    it('passes the saved input id through when a saved input is the active selection', () => {
      component.selectedPickerValue.set(`saved:${savedInput.id}`);
      runnerApi.invoke.mockReturnValue(
        of(new HttpResponse({ status: 200, body: { output: {}, duration_ms: 1, trace_id: 't', logs_tail: [] } })),
      );

      component.run();

      expect(runnerApi.invoke).toHaveBeenCalledWith('blogging.writer', {}, savedInput.id);
    });

    it('handles a successful invoke and refreshes history', () => {
      const envelope: InvokeEnvelope = { output: { ok: true }, duration_ms: 42, trace_id: 'trace-1234', logs_tail: [] };
      runnerApi.invoke.mockReturnValue(of(new HttpResponse({ status: 200, body: envelope })));
      expect(component.historyPanel).toBeTruthy();
      const refreshSpy = vi.spyOn(component.historyPanel!, 'refresh');

      component.run();

      expect(component.running()).toBe(false);
      expect(component.lastResponse()).toEqual(envelope);
      expect(refreshSpy).toHaveBeenCalled();
    });

    it('treats a 202 response as a warming notice, not a result', () => {
      runnerApi.invoke.mockReturnValue(
        of(new HttpResponse({ status: 202, body: { status: 'warming', message: 'still warming', sandbox: { agent_id: 'blogging.writer', status: 'warming' } } })),
      );

      component.run();

      expect(component.lastError()).toContain('warming');
      expect(component.lastResponse()).toBeNull();
    });

    it('surfaces a 409 conflict message', () => {
      runnerApi.invoke.mockReturnValue(
        throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'Agent not runnable in sandbox.' } })),
      );

      component.run();

      expect(component.lastError()).toBe('Agent not runnable in sandbox.');
    });

    it('unwraps a 422 envelope from the error detail', () => {
      const envelope: InvokeEnvelope = { output: null, duration_ms: 5, trace_id: 'trace-err', logs_tail: ['boom'], error: 'raised' };
      runnerApi.invoke.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 422, error: { detail: envelope } })));

      component.run();

      expect(component.lastResponse()).toEqual(envelope);
    });

    it('falls back to a generic invocation-failed message', () => {
      // A bare error object (not a real HttpErrorResponse) has neither
      // `error.detail` nor `message` — HttpErrorResponse always synthesises a
      // `message`, so this is the only way to reach the final `??` fallback.
      runnerApi.invoke.mockReturnValue(throwError(() => ({})));

      component.run();

      expect(component.lastError()).toBe('Invocation failed.');
    });
  });

  describe('run history interaction', () => {
    beforeEach(() => selectWriter());

    it('loads a past run into the input/output panes, including its sandbox url', () => {
      const record: RunRecord = {
        ...runSummary,
        input_data: { topic: 'x' },
        output_data: { ok: true },
        error: null,
        logs_tail: ['line 1'],
        sandbox_url: 'http://sandbox/run-1',
      };
      runnerApi.getRun.mockReturnValue(of(record));

      component.onHistoryLoadRun('run-1');

      expect(component.activeRunId()).toBe('run-1');
      expect(component.inputText()).toBe(JSON.stringify({ topic: 'x' }, null, 2));
      expect(component.lastResponse()).toEqual({
        output: { ok: true },
        duration_ms: 120,
        trace_id: 'trace-1234567890',
        logs_tail: ['line 1'],
        error: null,
        sandbox: { agent_id: 'blogging.writer', url: 'http://sandbox/run-1' },
      });
    });

    it('omits the sandbox field when the run has no sandbox url', () => {
      const record: RunRecord = { ...runSummary, input_data: {}, logs_tail: [], sandbox_url: null };
      runnerApi.getRun.mockReturnValue(of(record));

      component.onHistoryLoadRun('run-1');

      expect(component.lastResponse()?.sandbox).toBeUndefined();
    });

    it('logs a failure to load a past run', () => {
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
      runnerApi.getRun.mockReturnValue(throwError(() => new Error('missing run')));

      component.onHistoryLoadRun('run-1');

      expect(consoleSpy).toHaveBeenCalledWith('Failed to load run', expect.any(Error));
    });

    it('onHistoryCompare opens the diff dialog against the run output', () => {
      component.onHistoryCompare(runSummary);

      expect(dialogOpen).toHaveBeenCalledWith(AgentDiffDialogComponent, {
        data: {
          agentId: 'blogging.writer',
          initialLeft: { kind: 'run', ref: 'run-1', side: 'output' },
          initialLeftLabel: 'run:trace-12:output',
        },
        width: '800px',
        maxWidth: '95vw',
      });
    });

    it('onHistoryCompare is a no-op when no agent is selected', () => {
      component.onAgentChange(null);
      component.onHistoryCompare(runSummary);
      expect(dialogOpen).not.toHaveBeenCalled();
    });

    it('compareCurrentOutput opens the diff dialog against the current output', () => {
      const envelope: InvokeEnvelope = { output: { ok: true }, duration_ms: 1, trace_id: 't', logs_tail: [] };
      component.lastResponse.set(envelope);

      component.compareCurrentOutput();

      expect(dialogOpen).toHaveBeenCalledWith(AgentDiffDialogComponent, {
        data: { agentId: 'blogging.writer', initialLeft: { kind: 'inline', data: { ok: true } }, initialLeftLabel: 'left:current-output' },
        width: '800px',
        maxWidth: '95vw',
      });
    });

    it('compareCurrentOutput is a no-op without a current output', () => {
      component.compareCurrentOutput();
      expect(dialogOpen).not.toHaveBeenCalled();
    });
  });

  it('returnToCatalog emits requestCatalogReturn', () => {
    const emitted: void[] = [];
    component.requestCatalogReturn.subscribe(() => emitted.push(undefined));
    component.returnToCatalog();
    expect(emitted.length).toBe(1);
  });

  describe('view helpers', () => {
    it('prettyOutput/prettySchema render empty strings with nothing loaded', () => {
      expect(component.prettyOutput()).toBe('');
      expect(component.prettySchema()).toBe('');
    });

    it('prettyOutput/prettySchema render pretty JSON once populated', () => {
      component.lastResponse.set({ output: { a: 1 }, duration_ms: 1, trace_id: 't', logs_tail: [] });
      component.inputSchema.set({ type: 'object' });
      expect(component.prettyOutput()).toBe(JSON.stringify({ a: 1 }, null, 2));
      expect(component.prettySchema()).toBe(JSON.stringify({ type: 'object' }, null, 2));
    });

    it('trackAgent keys by id', () => {
      expect(component.trackAgent(0, writerSummary)).toBe('blogging.writer');
    });
  });

  describe('computed signals', () => {
    it('canRun is false with no agent selected', () => {
      expect(component.canRun()).toBe(false);
    });

    it('canRun is false when the agent requires live integrations', () => {
      catalogApi.getAgent.mockReturnValue(of(liveIntegrationDetail));
      component.onAgentChange('soc2.auditor');
      expect(component.requiresLiveIntegration()).toBe(true);
      expect(component.canRun()).toBe(false);
    });

    it('canRun is false with an input error and false while running', () => {
      selectWriter();
      component.inputError.set('bad');
      expect(component.canRun()).toBe(false);

      component.inputError.set(null);
      component.running.set(true);
      expect(component.canRun()).toBe(false);
    });

    it('canRun is true once an agent is selected with no blockers', () => {
      selectWriter();
      expect(component.canRun()).toBe(true);
    });

    it('sandboxStatusLabel defaults to cold with no sandbox handle', () => {
      expect(component.sandboxStatusLabel()).toBe('cold');
    });

    it('sandboxStatusLabel reflects the current sandbox status', () => {
      selectWriter();
      expect(component.sandboxStatusLabel()).toBe('cold');
    });

    it('parsedInput parses valid JSON and falls back to {} on invalid JSON', () => {
      component.inputText.set('{"a":1}');
      expect(component.parsedInput()).toEqual({ a: 1 });

      component.inputText.set('{not json');
      expect(component.parsedInput()).toEqual({});
    });
  });
});
