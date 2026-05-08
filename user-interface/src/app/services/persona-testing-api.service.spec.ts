import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { PersonaTestingApiService } from './persona-testing-api.service';
import { environment } from '../../environments/environment';

describe('PersonaTestingApiService', () => {
  let service: PersonaTestingApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.personaTestingApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [PersonaTestingApiService],
    });
    service = TestBed.inject(PersonaTestingApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('GET /personas via getPersonas', () => {
    service.getPersonas().subscribe((res) => expect(res.personas).toEqual([]));
    const req = httpMock.expectOne(`${baseUrl}/personas`);
    expect(req.request.method).toBe('GET');
    req.flush({ personas: [] });
  });

  it('GET /personas/{id} via getPersona', () => {
    service.getPersona('p-1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/personas/p-1`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('POST /personas via createPersona', () => {
    const payload = {
      name: 'QA',
      description: 'd',
      icon: 'bug_report',
      system_prompt: 's',
      spec_generation_prompt: 'g',
    };
    service.createPersona(payload).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/personas`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(payload);
    req.flush({});
  });

  it('PUT /personas/{id} via updatePersona', () => {
    service.updatePersona('p-1', { name: 'New' }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/personas/p-1`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ name: 'New' });
    req.flush({});
  });

  it('DELETE /personas/{id} via deletePersona', () => {
    service.deletePersona('p-1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/personas/p-1`);
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  it('encodes persona ids with URI-unsafe chars', () => {
    service.deletePersona('weird id/with/slash').subscribe();
    const req = httpMock.expectOne(
      `${baseUrl}/personas/weird%20id%2Fwith%2Fslash`,
    );
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  it('GET /testable-teams via getTestableTeams', () => {
    service.getTestableTeams().subscribe((res) => expect(res.teams).toEqual([]));
    const req = httpMock.expectOne(`${baseUrl}/testable-teams`);
    expect(req.request.method).toBe('GET');
    req.flush({ teams: [] });
  });

  it('POST /start sends the full persona/team/project payload', () => {
    const payload = {
      persona_id: 'p-1',
      target_team_key: 'software_engineering',
      project_name: 'taskflow-mvp',
    };
    service.startTest(payload).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/start`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(payload);
    req.flush({ job_id: 'r1', status: 'running', message: '' });
  });
});
