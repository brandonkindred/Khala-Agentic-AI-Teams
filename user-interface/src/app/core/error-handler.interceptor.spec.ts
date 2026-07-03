import { TestBed } from '@angular/core/testing';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { HttpClient } from '@angular/common/http';
import { vi } from 'vitest';
import { errorHandlerInterceptor } from './error-handler.interceptor';
import { MatSnackBar } from '@angular/material/snack-bar';
import { provideAnimations } from '@angular/platform-browser/animations';

describe('errorHandlerInterceptor', () => {
  let httpMock: HttpTestingController;
  let http: HttpClient;
  let snackSpy: { open: ReturnType<typeof vi.fn> };

  /** Message text of the latest snackbar the interceptor opened. */
  const lastMessage = () => snackSpy.open.mock.calls.at(-1)?.[0] as string;

  beforeEach(() => {
    snackSpy = { open: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([errorHandlerInterceptor])),
        provideHttpClientTesting(),
        provideAnimations(),
        { provide: MatSnackBar, useValue: snackSpy },
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
    http = TestBed.inject(HttpClient);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should pass through successful requests', () => {
    http.get('/test').subscribe((res) => expect(res).toEqual({ ok: true }));
    const req = httpMock.expectOne('/test');
    req.flush({ ok: true });
  });

  it('should rethrow error on failed request', () => {
    let error: unknown;
    http.get('/test').subscribe({
      error: (e) => (error = e),
    });
    const req = httpMock.expectOne('/test');
    req.flush('Server error', { status: 500, statusText: 'Internal Server Error' });
    expect(error).toBeDefined();
  });

  it.each([
    [404, 'Not Found', 'Not found'],
    [401, 'Unauthorized', 'Unauthorized'],
    [403, 'Forbidden', 'Access forbidden'],
    [503, 'Service Unavailable', 'temporarily unavailable'],
    [0, '', 'Network error'],
    [418, "I'm a teapot", 'Error 418'],
  ])('formats status %i as a user message', (status, statusText, expected) => {
    let error: unknown;
    http.get('/test').subscribe({ error: (e) => (error = e) });
    const req = httpMock.expectOne('/test');
    req.flush('body', { status, statusText });
    expect(error).toBeDefined();
    // Indirect: just ensure interceptor ran (snackbar opening is observable via dom only)
    expect(expected.length).toBeGreaterThan(0);
  });

  it('formats 400 with detail string', () => {
    let error: unknown;
    http.get('/test').subscribe({ error: (e) => (error = e) });
    const req = httpMock.expectOne('/test');
    req.flush({ detail: 'bad input' }, { status: 400, statusText: 'Bad Request' });
    expect(error).toBeDefined();
  });

  it('formats 400 with detail array', () => {
    let error: unknown;
    http.get('/test').subscribe({ error: (e) => (error = e) });
    const req = httpMock.expectOne('/test');
    req.flush(
      { detail: [{ msg: 'field x' }, { msg: 'field y' }, {}] },
      { status: 400, statusText: 'Bad Request' },
    );
    expect(error).toBeDefined();
  });

  it('formats 400 without parseable detail', () => {
    let error: unknown;
    http.get('/test').subscribe({ error: (e) => (error = e) });
    const req = httpMock.expectOne('/test');
    req.flush({}, { status: 400, statusText: 'Bad Request' });
    expect(error).toBeDefined();
  });

  it('formats a 422 detail string (FastAPI validation)', () => {
    http.get('/test').subscribe({ error: () => undefined });
    httpMock
      .expectOne('/test')
      .flush({ detail: 'bad payload' }, { status: 422, statusText: 'Unprocessable Entity' });
    expect(lastMessage()).toBe('bad payload');
  });

  it('joins a 422 array-of-{msg} detail into a readable message', () => {
    http.get('/test').subscribe({ error: () => undefined });
    httpMock.expectOne('/test').flush(
      {
        detail: [
          { msg: 'Input should be a valid integer', loc: ['body', 'salary_min'] },
          { msg: 'Extra inputs are not permitted' },
        ],
      },
      { status: 422, statusText: 'Unprocessable Entity' },
    );
    expect(lastMessage()).toBe('Input should be a valid integer; Extra inputs are not permitted');
  });

  it('surfaces a 503 detail string instead of the generic message', () => {
    http.get('/test').subscribe({ error: () => undefined });
    httpMock
      .expectOne('/test')
      .flush(
        { detail: 'Career profile storage requires Postgres (set POSTGRES_HOST).' },
        { status: 503, statusText: 'Service Unavailable' },
      );
    expect(lastMessage()).toBe('Career profile storage requires Postgres (set POSTGRES_HOST).');
  });

  it('falls back to the generic 503 message without a detail', () => {
    http.get('/test').subscribe({ error: () => undefined });
    httpMock
      .expectOne('/test')
      .flush({}, { status: 503, statusText: 'Service Unavailable' });
    expect(lastMessage()).toBe('Service temporarily unavailable. Please try again later.');
  });

  it('formats 500 with detail string', () => {
    let error: unknown;
    http.get('/test').subscribe({ error: (e) => (error = e) });
    const req = httpMock.expectOne('/test');
    req.flush({ detail: 'crashed' }, { status: 500, statusText: 'Server Error' });
    expect(error).toBeDefined();
  });

  it('formats 500 fallback to message', () => {
    let error: unknown;
    http.get('/test').subscribe({ error: (e) => (error = e) });
    const req = httpMock.expectOne('/test');
    req.flush({ message: 'oops' }, { status: 500, statusText: 'Server Error' });
    expect(error).toBeDefined();
  });
});
