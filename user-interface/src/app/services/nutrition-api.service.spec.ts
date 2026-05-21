import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { vi } from 'vitest';
import { NutritionApiService } from './nutrition-api.service';
import { environment } from '../../environments/environment';

describe('NutritionApiService', () => {
  let service: NutritionApiService;
  let httpMock: HttpTestingController;
  const baseUrl = environment.nutritionApiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [NutritionApiService],
    });
    service = TestBed.inject(NutritionApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    vi.useRealTimers();
  });

  it('healthCheck', () => {
    service.healthCheck().subscribe();
    httpMock.expectOne(`${baseUrl}/health`).flush({});
  });

  it('getProfile', () => {
    service.getProfile('c1').subscribe();
    httpMock.expectOne(`${baseUrl}/profile/c1`).flush({});
  });

  it('upsertProfile', () => {
    service.upsertProfile('c1', {} as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/profile/c1`);
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });

  it('submitFeedback (all params)', () => {
    service.submitFeedback('c1', 'r1', 4, true, 'great').subscribe();
    const req = httpMock.expectOne(`${baseUrl}/feedback`);
    expect(req.request.body.client_id).toBe('c1');
    expect(req.request.body.rating).toBe(4);
    req.flush({});
  });

  it('getMealHistory', () => {
    service.getMealHistory('c1').subscribe();
    httpMock.expectOne(`${baseUrl}/history/meals?client_id=c1`).flush({});
  });

  it('sendChatMessage', () => {
    service.sendChatMessage({ client_id: 'c1', message: 'hi' } as never).subscribe();
    const req = httpMock.expectOne(`${baseUrl}/chat`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('getChatHistory', () => {
    service.getChatHistory('c1').subscribe();
    httpMock.expectOne(`${baseUrl}/chat/history/c1`).flush({});
  });

  it('getJob direct', () => {
    service.getJob<unknown>('j1').subscribe();
    httpMock.expectOne(`${baseUrl}/jobs/j1`).flush({});
  });

  it('generateNutritionPlan submits and completes immediately', () => {
    const out: unknown[] = [];
    service.generateNutritionPlan('c1').subscribe((r) => out.push(r));
    httpMock.expectOne(`${baseUrl}/plan/nutrition`).flush({ job_id: 'j1', status: 'pending' });
    httpMock.expectOne(`${baseUrl}/jobs/j1`).flush({
      job_id: 'j1',
      status: 'completed',
      result: { plan: 'OK' },
    });
    expect(out).toEqual([{ plan: 'OK' }]);
  });

  it('generateNutritionPlan errors on failed status', () => {
    let err: Error | undefined;
    service.generateNutritionPlan('c1').subscribe({ error: (e) => (err = e as Error) });
    httpMock.expectOne(`${baseUrl}/plan/nutrition`).flush({ job_id: 'j1', status: 'pending' });
    httpMock.expectOne(`${baseUrl}/jobs/j1`).flush({
      job_id: 'j1',
      status: 'failed',
      error: 'boom',
    });
    expect(err).toBeDefined();
    expect(err!.message).toContain('boom');
  });

  it('generateNutritionPlan errors when cancelled with no result', () => {
    let err: Error | undefined;
    service.generateNutritionPlan('c1').subscribe({ error: (e) => (err = e as Error) });
    httpMock.expectOne(`${baseUrl}/plan/nutrition`).flush({ job_id: 'j1', status: 'pending' });
    httpMock.expectOne(`${baseUrl}/jobs/j1`).flush({
      job_id: 'j1',
      status: 'cancelled',
    });
    expect(err).toBeDefined();
  });

  it('regenerateNutritionPlan polls and completes', () => {
    const out: unknown[] = [];
    service.regenerateNutritionPlan('c1').subscribe((r) => out.push(r));
    httpMock
      .expectOne(`${baseUrl}/plan/nutrition/c1/regenerate`)
      .flush({ job_id: 'j2', status: 'pending' });
    httpMock
      .expectOne(`${baseUrl}/jobs/j2`)
      .flush({ job_id: 'j2', status: 'completed', result: { plan: 'X' } });
    expect(out).toEqual([{ plan: 'X' }]);
  });

  it('generateMealPlan polls and completes', () => {
    const out: unknown[] = [];
    service.generateMealPlan('c1', 7, ['breakfast']).subscribe((r) => out.push(r));
    const req = httpMock.expectOne(`${baseUrl}/plan/meals`);
    expect(req.request.body.period_days).toBe(7);
    req.flush({ job_id: 'j3', status: 'pending' });
    httpMock
      .expectOne(`${baseUrl}/jobs/j3`)
      .flush({ job_id: 'j3', status: 'completed', result: { meals: [] } });
    expect(out).toEqual([{ meals: [] }]);
  });
});
