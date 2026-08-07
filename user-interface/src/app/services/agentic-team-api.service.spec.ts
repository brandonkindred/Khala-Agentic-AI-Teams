import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { AgenticTeamApiService } from './agentic-team-api.service';
import { environment } from '../../environments/environment';

describe('AgenticTeamApiService', () => {
  let service: AgenticTeamApiService;
  let httpMock: HttpTestingController;
  const base = environment.agenticTeamProvisioningApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AgenticTeamApiService],
    });
    service = TestBed.inject(AgenticTeamApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('health', () => {
    service.health().subscribe();
    httpMock.expectOne(`${base}/health`).flush({ status: 'ok' });
  });

  it('createTeam', () => {
    service.createTeam({} as never).subscribe();
    const req = httpMock.expectOne(`${base}/teams`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('listTeams', () => {
    service.listTeams().subscribe();
    httpMock.expectOne(`${base}/teams`).flush([]);
  });

  it('getTeam', () => {
    service.getTeam('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1`).flush({});
  });

  it('listTeamAgents', () => {
    service.listTeamAgents('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/agents`).flush([]);
  });

  it('validateRoster', () => {
    service.validateRoster('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/roster/validation`).flush({});
  });

  it('addAgentFromRegistry', () => {
    service.addAgentFromRegistry('t1', 'blogging.planner').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/agents/from-registry`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ manifest_id: 'blogging.planner' });
    req.flush({});
  });

  it('removeTeamAgent', () => {
    service.removeTeamAgent('t1', 'agent/x').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/agents/${encodeURIComponent('agent/x')}`);
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  it('listProcesses', () => {
    service.listProcesses('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/processes`).flush([]);
  });

  it('getProcess', () => {
    service.getProcess('p1').subscribe();
    httpMock.expectOne(`${base}/processes/p1`).flush({});
  });

  it('createConversation without initial message', () => {
    service.createConversation('t1').subscribe();
    const req = httpMock.expectOne(`${base}/conversations`);
    expect(req.request.body.initial_message).toBeNull();
    req.flush({});
  });

  it('createConversation with initial message', () => {
    service.createConversation('t1', 'hi').subscribe();
    const req = httpMock.expectOne(`${base}/conversations`);
    expect(req.request.body.initial_message).toBe('hi');
    req.flush({});
  });

  it('sendMessage', () => {
    service.sendMessage('c1', 'hi').subscribe();
    const req = httpMock.expectOne(`${base}/conversations/c1/messages`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getConversation', () => {
    service.getConversation('c1').subscribe();
    httpMock.expectOne(`${base}/conversations/c1`).flush({});
  });

  it('listConversations', () => {
    service.listConversations('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/conversations`).flush([]);
  });

  it('setConversationProcess', () => {
    service.setConversationProcess('c1', 'p1').subscribe();
    const req = httpMock.expectOne(`${base}/conversations/c1/process`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('createProcess', () => {
    service.createProcess('t1').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/processes`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('updateProcess', () => {
    service.updateProcess('p1', { id: 'p1' } as never).subscribe();
    const req = httpMock.expectOne(`${base}/processes/p1`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('recommendAgentsForStep', () => {
    service.recommendAgentsForStep('p1', 's1').subscribe();
    const req = httpMock.expectOne(`${base}/processes/p1/steps/s1/recommend-agents`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('listAgentEnvironments', () => {
    service.listAgentEnvironments('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/agent-environments`).flush([]);
  });

  it('setTeamMode', () => {
    service.setTeamMode('t1', 'test' as never).subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/mode`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('createTestChatSession', () => {
    service.createTestChatSession('t1', 'agent.x').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-chat/sessions`);
    expect(req.request.body.agent_name).toBe('agent.x');
    req.flush({});
  });

  it('listTestChatSessions no filter', () => {
    service.listTestChatSessions('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/test-chat/sessions`).flush([]);
  });

  it('listTestChatSessions with agent filter', () => {
    service.listTestChatSessions('t1', 'agent/x').subscribe();
    httpMock.expectOne(`${base}/teams/t1/test-chat/sessions?agent_name=${encodeURIComponent('agent/x')}`).flush([]);
  });

  it('getTestChatSession', () => {
    service.getTestChatSession('t1', 's1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/test-chat/sessions/s1`).flush({});
  });

  it('renameTestChatSession', () => {
    service.renameTestChatSession('t1', 's1', 'Renamed').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-chat/sessions/s1/name`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('deleteTestChatSession', () => {
    service.deleteTestChatSession('t1', 's1').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-chat/sessions/s1`);
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  it('sendTestChatMessage', () => {
    service.sendTestChatMessage('t1', 's1', 'hi').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-chat/sessions/s1/messages`);
    expect(req.request.body.content).toBe('hi');
    req.flush({});
  });

  it('exportTestChatSession', () => {
    service.exportTestChatSession('t1', 's1').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-chat/sessions/s1/export`);
    expect(req.request.responseType).toBe('blob');
    req.flush(new Blob([]));
  });

  it('rateTestChatMessage', () => {
    service.rateTestChatMessage('t1', 'm1', 'up').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-chat/messages/m1/rating`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('getAgentQualityScores', () => {
    service.getAgentQualityScores('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/test-chat/quality-scores`).flush([]);
  });

  it('startPipelineRun', () => {
    service.startPipelineRun('t1', 'p1').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-pipeline/runs`);
    expect(req.request.body.process_id).toBe('p1');
    expect(req.request.body.initial_input).toBeNull();
    req.flush({});
  });

  it('startPipelineRun with initial input', () => {
    service.startPipelineRun('t1', 'p1', 'go').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-pipeline/runs`);
    expect(req.request.body.initial_input).toBe('go');
    req.flush({});
  });

  it('listPipelineRuns', () => {
    service.listPipelineRuns('t1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/test-pipeline/runs`).flush([]);
  });

  it('getPipelineRun', () => {
    service.getPipelineRun('t1', 'r1').subscribe();
    httpMock.expectOne(`${base}/teams/t1/test-pipeline/runs/r1`).flush({});
  });

  it('submitPipelineInput', () => {
    service.submitPipelineInput('t1', 'r1', 'go').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-pipeline/runs/r1/input`);
    expect(req.request.body.input).toBe('go');
    req.flush({});
  });

  it('cancelPipelineRun', () => {
    service.cancelPipelineRun('t1', 'r1').subscribe();
    const req = httpMock.expectOne(`${base}/teams/t1/test-pipeline/runs/r1/cancel`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });
});
