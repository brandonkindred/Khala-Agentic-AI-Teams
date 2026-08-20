import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { AgentRunnerApiService } from './agent-runner-api.service';
import { SKIP_ERROR_NOTIFY } from '../core/error-handler.interceptor';
import { environment } from '../../environments/environment';

describe('AgentRunnerApiService', () => {
  let service: AgentRunnerApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.agentRegistryApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AgentRunnerApiService],
    });
    service = TestBed.inject(AgentRunnerApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('lists warm sandboxes', () => {
    service.listWarmSandboxes().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sandboxes`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('ensures warm sandbox', () => {
    service.ensureWarm('agent.foo').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sandboxes/agent.foo/warm`);
    expect(req.request.method).toBe('POST');
    req.flush({ agent_id: 'agent.foo', status: 'ready' });
  });

  it('gets sandbox details', () => {
    service.getSandbox('agent.foo').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sandboxes/agent.foo`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('tears down sandbox', () => {
    service.teardown('agent.foo').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/sandboxes/agent.foo`);
    expect(req.request.method).toBe('DELETE');
    req.flush({ agent_id: 'agent.foo', status: 'gone' });
  });

  it('invokes an agent', () => {
    service.invoke('agent.foo', { a: 1 }).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/agent.foo/invoke`);
    expect(req.request.method).toBe('POST');
    expect(req.request.params.keys().length).toBe(0);
    expect(req.request.context.get(SKIP_ERROR_NOTIFY)).toBe(true);
    req.flush({});
  });

  it('invokes an agent with saved_input_id param', () => {
    service.invoke('agent.foo', { a: 1 }, 'saved-1').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/agent.foo/invoke`);
    expect(req.request.params.get('saved_input_id')).toBe('saved-1');
    req.flush({});
  });

  it('lists samples', () => {
    service.listSamples('agent.foo').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/agent.foo/samples`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('gets a sample', () => {
    service.getSample('agent.foo', 'sample.json').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/agent.foo/samples/sample.json`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('lists saved inputs', () => {
    service.listSavedInputs('agent.foo').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/agent.foo/saved-inputs`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('creates a saved input', () => {
    service.createSavedInput('agent.foo', { name: 'x', payload: {} }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/agent.foo/saved-inputs`);
    expect(req.request.method).toBe('POST');
    expect(req.request.context.get(SKIP_ERROR_NOTIFY)).toBe(true);
    req.flush({});
  });

  it('updates a saved input', () => {
    service.updateSavedInput('id1', { name: 'rename', payload: {} }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/saved-inputs/id1`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('deletes a saved input', () => {
    service.deleteSavedInput('id1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/saved-inputs/id1`);
    expect(req.request.method).toBe('DELETE');
    expect(req.request.context.get(SKIP_ERROR_NOTIFY)).toBe(true);
    req.flush({ id: 'id1', status: 'deleted' });
  });

  it('lists runs without cursor', () => {
    service.listRuns('agent.foo').subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/agent.foo/runs`);
    expect(req.request.params.get('limit')).toBe('20');
    expect(req.request.params.get('cursor')).toBeNull();
    req.flush([]);
  });

  it('lists runs with cursor + limit', () => {
    service.listRuns('agent.foo', 'cur-1', 50).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${baseUrl}/agent.foo/runs`);
    expect(req.request.params.get('limit')).toBe('50');
    expect(req.request.params.get('cursor')).toBe('cur-1');
    req.flush([]);
  });

  it('gets a run', () => {
    service.getRun('r1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/runs/r1`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('deletes a run', () => {
    service.deleteRun('r1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/runs/r1`);
    expect(req.request.method).toBe('DELETE');
    req.flush({ id: 'r1', status: 'deleted' });
  });

  it('posts diff', () => {
    service.diff({ left: { a: 1 }, right: { a: 2 } } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/diff`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });
});
