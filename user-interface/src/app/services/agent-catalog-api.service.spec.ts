import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { AgentCatalogApiService } from './agent-catalog-api.service';
import { environment } from '../../environments/environment';

describe('AgentCatalogApiService', () => {
  let service: AgentCatalogApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.agentRegistryApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AgentCatalogApiService],
    });
    service = TestBed.inject(AgentCatalogApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('lists agents without filters', () => {
    service.listAgents().subscribe();
    const req = httpMock.expectOne((r) => r.url === baseUrl);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.keys().length).toBe(0);
    req.flush([]);
  });

  it('lists agents with team/tag/q filters', () => {
    service.listAgents({ team: 'blogging', tag: 'qa', q: 'writer' }).subscribe();
    const req = httpMock.expectOne((r) => r.url === baseUrl);
    expect(req.request.params.get('team')).toBe('blogging');
    expect(req.request.params.get('tag')).toBe('qa');
    expect(req.request.params.get('q')).toBe('writer');
    req.flush([]);
  });

  it('lists teams', () => {
    service.listTeams().subscribe();
    const req = httpMock.expectOne(`${baseUrl}/teams`);
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('gets agent detail with encoded id', () => {
    service.getAgent('blog/writer').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/${encodeURIComponent('blog/writer')}`);
    expect(req.request.method).toBe('GET');
    req.flush({ id: 'blog/writer' });
  });

  it('gets input schema', () => {
    service.getInputSchema('id-1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/id-1/schema/input`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('gets output schema', () => {
    service.getOutputSchema('id-1').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/id-1/schema/output`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });
});
