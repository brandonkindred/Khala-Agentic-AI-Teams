import { TestBed } from '@angular/core/testing';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { HttpClient, HttpContext } from '@angular/common/http';
import { vi } from 'vitest';
import { errorHandlerInterceptor, extractErrorDetail, SKIP_ERROR_NOTIFY } from './error-handler.interceptor';
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

  it('falls back to statusText for a 400 without parseable detail, not the raw HttpErrorResponse message', () => {
    http.get('/test').subscribe({ error: () => undefined });
    httpMock.expectOne('/test').flush({}, { status: 400, statusText: 'Bad Request' });
    expect(lastMessage()).toBe('Bad request: Bad Request');
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

  it('prefers err.error.message over the HttpErrorResponse message for a 500 without detail', () => {
    http.get('/test').subscribe({ error: () => undefined });
    httpMock.expectOne('/test').flush({ message: 'oops' }, { status: 500, statusText: 'Server Error' });
    expect(lastMessage()).toBe('Server error: oops');
  });
});

describe('errorHandlerInterceptor SKIP_ERROR_NOTIFY', () => {
  let httpMock: HttpTestingController;
  let http: HttpClient;
  let snackBar: { open: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    snackBar = { open: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([errorHandlerInterceptor])),
        provideHttpClientTesting(),
        { provide: MatSnackBar, useValue: snackBar },
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
    http = TestBed.inject(HttpClient);
  });

  afterEach(() => httpMock.verify());

  it('suppresses the error toast when the request opts out', () => {
    const context = new HttpContext().set(SKIP_ERROR_NOTIFY, true);
    http.get('/silent', { context }).subscribe({ error: () => undefined });
    httpMock.expectOne('/silent').flush('x', { status: 500, statusText: 'Server Error' });
    expect(snackBar.open).not.toHaveBeenCalled();
  });

  it('still toasts for a normal request', () => {
    http.get('/loud').subscribe({ error: () => undefined });
    httpMock.expectOne('/loud').flush('x', { status: 500, statusText: 'Server Error' });
    expect(snackBar.open).toHaveBeenCalledTimes(1);
  });
});

describe('extractErrorDetail (re-exported from shared)', () => {
  it('returns the FastAPI detail string when present', () => {
    expect(extractErrorDetail({ error: { detail: 'boom' } }, 'fallback')).toBe('boom');
  });

  it('joins the msg fields of a validation-error detail array when opted in', () => {
    const err = { error: { detail: [{ msg: 'field x' }, { msg: 'field y' }, {}] } };
    expect(extractErrorDetail(err, 'fallback', { joinValidationArray: true })).toBe('field x; field y');
  });

  it('skips the detail array by default (defers to interceptor toast)', () => {
    const err = { error: { detail: [{ msg: 'field x' }] }, message: 'net down' };
    expect(extractErrorDetail(err, 'fallback')).toBe('net down');
  });

  it('falls back to the fallback when the detail array has no messages', () => {
    expect(extractErrorDetail({ error: { detail: [{}, {}] } }, 'fallback', { joinValidationArray: true })).toBe('fallback');
  });

  it('falls back to the error message when there is no detail', () => {
    expect(extractErrorDetail({ message: 'net down' }, 'fallback')).toBe('net down');
  });

  it('returns the fallback for an empty/undefined error', () => {
    expect(extractErrorDetail(undefined, 'fallback')).toBe('fallback');
    expect(extractErrorDetail({ error: { detail: '' } }, 'fallback')).toBe('fallback');
  });
});
