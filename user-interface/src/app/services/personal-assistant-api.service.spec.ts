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

  it('should call POST /users/{userId}/assistant for sendMessage', () => {
    const userId = 'u1';
    const body = { message: 'Hello', context: {} };
    service.sendMessage(userId, body).subscribe((res) => expect(res.message).toBeDefined());
    const req = httpMock.expectOne(`${baseUrl}/users/${userId}/assistant`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ request_id: 'r1', message: 'Hi', actions_taken: [], data: {} });
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
