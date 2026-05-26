import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { StartupAdvisorApiService } from './startup-advisor-api.service';
import { environment } from '../../environments/environment';

describe('StartupAdvisorApiService', () => {
  let service: StartupAdvisorApiService;
  let httpMock: HttpTestingController;
  const base = environment.startupAdvisorApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [StartupAdvisorApiService],
    });
    service = TestBed.inject(StartupAdvisorApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('getConversation', () => {
    service.getConversation().subscribe();
    httpMock.expectOne(`${base}/conversation`).flush({});
  });

  it('getArtifacts', () => {
    service.getArtifacts().subscribe();
    httpMock.expectOne(`${base}/conversation/artifacts`).flush([]);
  });

  it('updateContext', () => {
    service.updateContext({ founder: 'me' }).subscribe();
    const req = httpMock.expectOne(`${base}/conversation/context`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('sendMessage completes immediately when first poll returns completed', () => {
    const out: unknown[] = [];
    service.sendMessage('hello').subscribe((r) => out.push(r));
    const req = httpMock.expectOne(`${base}/conversation/messages`);
    expect(req.request.body.message).toBe('hello');
    req.flush({ job_id: 'j1', status: 'pending' });
    httpMock.expectOne(`${base}/conversation/messages/status/j1`).flush({
      job_id: 'j1',
      status: 'completed',
      result: { state: 'updated' },
    });
    expect(out).toEqual([{ state: 'updated' }]);
  });

  it('sendMessage errors when job fails', () => {
    let err: Error | undefined;
    service.sendMessage('hello').subscribe({ error: (e) => (err = e as Error) });
    httpMock.expectOne(`${base}/conversation/messages`).flush({ job_id: 'j2', status: 'pending' });
    httpMock.expectOne(`${base}/conversation/messages/status/j2`).flush({
      job_id: 'j2',
      status: 'failed',
      error: 'boom',
    });
    expect(err!.message).toContain('boom');
  });

  it('sendMessage errors when cancelled without result', () => {
    let err: Error | undefined;
    service.sendMessage('hello').subscribe({ error: (e) => (err = e as Error) });
    httpMock.expectOne(`${base}/conversation/messages`).flush({ job_id: 'j3', status: 'pending' });
    httpMock.expectOne(`${base}/conversation/messages/status/j3`).flush({
      job_id: 'j3',
      status: 'cancelled',
    });
    expect(err).toBeDefined();
  });
});
