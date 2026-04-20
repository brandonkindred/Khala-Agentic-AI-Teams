import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { PersonalAssistantApiService } from './personal-assistant-api.service';
import { environment } from '../../environments/environment';

describe('PersonalAssistantApiService', () => {
  let service: PersonalAssistantApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.personalAssistantApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [PersonalAssistantApiService],
    });
    service = TestBed.inject(PersonalAssistantApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should call GET /users/{userId}/tasks for getTasks', () => {
    const userId = 'u1';
    const mockLists = [{ list_id: 'l1', name: 'Tasks', items: [] }];
    service.getTasks(userId).subscribe((res) => expect(res).toEqual(mockLists));
    const req = httpMock.expectOne(`${baseUrl}/users/${userId}/tasks`);
    expect(req.request.method).toBe('GET');
    req.flush(mockLists);
  });

  it('should call POST /users/{userId}/tasks/from-text for addTasksFromText', () => {
    const userId = 'u1';
    const body = { text: 'Buy milk' };
    service.addTasksFromText(userId, body).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/users/${userId}/tasks/from-text`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ lists: [] });
  });

  it('should call PATCH for updateTaskItem', () => {
    const userId = 'u1';
    const listId = 'l1';
    const itemId = 'i1';
    const updates = { status: 'completed' };
    service.updateTaskItem(userId, listId, itemId, updates).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/users/${userId}/tasks/${listId}/items/${itemId}`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual(updates);
    req.flush({ item_id: itemId, status: 'completed' });
  });

  it('should submit an assistant job and poll until completed', () => {
    const userId = 'u1';
    const body = { message: 'Hello', context: {} };
    const jobId = 'job-123';
    const assistantResponse = {
      request_id: 'r1',
      message: 'Hi',
      actions_taken: [],
      data: {},
      timestamp: new Date().toISOString(),
    };

    let received: any = null;
    service.sendMessage(userId, body).subscribe((res) => (received = res));

    const submitReq = httpMock.expectOne((r) => r.url === `${baseUrl}/assistant/jobs`);
    expect(submitReq.request.method).toBe('POST');
    expect(submitReq.request.body).toEqual(body);
    expect(submitReq.request.params.get('user_id')).toBe(userId);
    submitReq.flush({ job_id: jobId, status: 'running' });

    const pollReq = httpMock.expectOne(`${baseUrl}/assistant/jobs/${jobId}`);
    expect(pollReq.request.method).toBe('GET');
    pollReq.flush({
      job_id: jobId,
      user_id: userId,
      status: 'completed',
      response: assistantResponse,
    });

    expect(received).toEqual(assistantResponse);
  });

  it('should call GET /health for health', () => {
    service.health().subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/health`);
    expect(req.request.method).toBe('GET');
    req.flush({ status: 'ok' });
  });

  it('should call GET /users/{userId}/profile for getProfile', () => {
    const userId = 'u1';
    service.getProfile(userId).subscribe((res) => expect(res).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/users/${userId}/profile`);
    expect(req.request.method).toBe('GET');
    req.flush({ identity: {}, preferences: {} });
  });
});
