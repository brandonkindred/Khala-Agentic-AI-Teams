import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { LlmConfigApiService } from './llm-config-api.service';
import { environment } from '../../environments/environment';

describe('LlmConfigApiService', () => {
  let service: LlmConfigApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.llmConfigApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [LlmConfigApiService],
    });
    service = TestBed.inject(LlmConfigApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('getConfig issues GET /api/llm-config', () => {
    const mock = { provider: 'ollama', model: 'm', ollama_base_url: 'https://ollama.com' };
    service.getConfig().subscribe((res) => expect(res).toEqual(mock as never));
    const req = httpMock.expectOne(baseUrl);
    expect(req.request.method).toBe('GET');
    req.flush(mock);
  });

  it('updateConfig issues PUT /api/llm-config with body', () => {
    const body = { provider: 'claude' as const, model: 'claude-opus-4-8', claude_api_key: 'sk' };
    service.updateConfig(body).subscribe();
    const req = httpMock.expectOne(baseUrl);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual(body);
    req.flush({ provider: 'claude', model: 'claude-opus-4-8' });
  });
});
