import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { HttpErrorResponse } from '@angular/common/http';
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

  it('listProviders issues GET /api/llm-config/providers', () => {
    const mock = { providers: [], storage_available: true, storage_status: 'available' };
    service.listProviders().subscribe((res) => expect(res).toEqual(mock));
    const req = httpMock.expectOne(`${baseUrl}/providers`);
    expect(req.request.method).toBe('GET');
    req.flush(mock);
  });

  it('createProvider issues POST /api/llm-config/providers with body', () => {
    const body = { label: 'A', provider: 'claude' as const, api_key: 'sk' };
    service.createProvider(body).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/providers`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush({ providers: [], storage_available: true, storage_status: 'available' });
  });

  it('updateProvider issues PUT /api/llm-config/providers/{id}', () => {
    service.updateProvider(7, { label: 'B' }).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/providers/7`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ label: 'B' });
    req.flush({ providers: [], storage_available: true, storage_status: 'available' });
  });

  it('deleteProvider issues DELETE /api/llm-config/providers/{id}', () => {
    service.deleteProvider(3).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/providers/3`);
    expect(req.request.method).toBe('DELETE');
    req.flush({ providers: [], storage_available: true, storage_status: 'available' });
  });

  it('reorderProviders issues PUT /api/llm-config/providers/order with ids', () => {
    service.reorderProviders([3, 1, 2]).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/providers/order`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ ids: [3, 1, 2] });
    req.flush({ providers: [], storage_available: true, storage_status: 'available' });
  });

  // Error propagation for the provider-list methods — each must surface an
  // HttpErrorResponse to the subscriber rather than swallowing the failure.
  const providerErrorCases: readonly {
    name: string;
    call: () => void;
    url: string;
    status: number;
  }[] = [
    { name: 'listProviders', call: () => service.listProviders().subscribe({ error: capture }), url: `${baseUrl}/providers`, status: 500 },
    { name: 'createProvider', call: () => service.createProvider({ label: 'A', provider: 'claude', api_key: 'sk' }).subscribe({ error: capture }), url: `${baseUrl}/providers`, status: 400 },
    { name: 'updateProvider', call: () => service.updateProvider(7, { label: 'B' }).subscribe({ error: capture }), url: `${baseUrl}/providers/7`, status: 404 },
    { name: 'deleteProvider', call: () => service.deleteProvider(3).subscribe({ error: capture }), url: `${baseUrl}/providers/3`, status: 503 },
    { name: 'reorderProviders', call: () => service.reorderProviders([1, 2]).subscribe({ error: capture }), url: `${baseUrl}/providers/order`, status: 429 },
  ];

  let captured: HttpErrorResponse | undefined;
  const capture = (err: HttpErrorResponse) => (captured = err);

  for (const tc of providerErrorCases) {
    it(`${tc.name} propagates an HTTP error to the observable`, () => {
      captured = undefined;
      tc.call();
      const req = httpMock.expectOne(tc.url);
      req.flush('error', { status: tc.status, statusText: 'Error' });
      expect(captured).toBeInstanceOf(HttpErrorResponse);
      expect(captured?.status).toBe(tc.status);
    });
  }
});
