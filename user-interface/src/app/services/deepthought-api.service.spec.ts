import { TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { DeepthoughtApiService, StreamEvent } from './deepthought-api.service';

function makeReader(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(enc.encode(chunks[i++]));
      } else {
        controller.close();
      }
    },
  });
}

describe('DeepthoughtApiService', () => {
  let service: DeepthoughtApiService;
  let originalFetch: typeof fetch;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [DeepthoughtApiService] });
    service = TestBed.inject(DeepthoughtApiService);
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('streams agent_event, result, and done events', async () => {
    const sse = [
      'event: agent_event\ndata: {"agent":"x"}\n\n',
      'event: result\ndata: {"answer":"42"}\n\n',
      'event: done\n\n',
    ];
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: makeReader(sse),
    } as never);
    const events: StreamEvent[] = [];
    await new Promise<void>((resolve) => {
      service.askStream({ message: 'q' } as never).subscribe({
        next: (e) => events.push(e),
        complete: resolve,
      });
    });
    expect(events.some((e) => e.type === 'agent_event')).toBe(true);
    expect(events.some((e) => e.type === 'result')).toBe(true);
    expect(events.some((e) => e.type === 'done')).toBe(true);
  });

  it('emits error event when response is not ok', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
    } as never);
    const events: StreamEvent[] = [];
    await new Promise<void>((resolve) => {
      service.askStream({} as never).subscribe({
        next: (e) => events.push(e),
        complete: resolve,
      });
    });
    expect(events[0]).toMatchObject({ type: 'error' });
  });

  it('emits error event when fetch throws non-abort', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('network'));
    const events: StreamEvent[] = [];
    await new Promise<void>((resolve) => {
      service.askStream({} as never).subscribe({
        next: (e) => events.push(e),
        complete: resolve,
      });
    });
    expect(events.find((e) => e.type === 'error')).toBeDefined();
  });

  it('handles error event with parsed body', async () => {
    const sse = ['event: error\ndata: {"error":"oops"}\n\n', 'event: done\n\n'];
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: makeReader(sse),
    } as never);
    const events: StreamEvent[] = [];
    await new Promise<void>((resolve) => {
      service.askStream({} as never).subscribe({ next: (e) => events.push(e), complete: resolve });
    });
    const errEvent = events.find((e): e is { type: 'error'; payload: string } => e.type === 'error');
    expect(errEvent?.payload).toBe('oops');
  });

  it('ignores blocks without event type', async () => {
    const sse = ['data: ignored\n\n', 'event: done\n\n'];
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: makeReader(sse),
    } as never);
    const events: StreamEvent[] = [];
    await new Promise<void>((resolve) => {
      service.askStream({} as never).subscribe({ next: (e) => events.push(e), complete: resolve });
    });
    expect(events.find((e) => e.type === 'done')).toBeDefined();
  });

  it('handles parse errors gracefully (returns null block)', async () => {
    const sse = ['event: agent_event\ndata: not-json\n\n', 'event: done\n\n'];
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: makeReader(sse),
    } as never);
    const events: StreamEvent[] = [];
    await new Promise<void>((resolve) => {
      service.askStream({} as never).subscribe({ next: (e) => events.push(e), complete: resolve });
    });
    expect(events.find((e) => e.type === 'agent_event')).toBeUndefined();
  });

  it('handles unknown event types', async () => {
    const sse = ['event: bizarre\ndata: {}\n\n', 'event: done\n\n'];
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: makeReader(sse),
    } as never);
    const events: StreamEvent[] = [];
    await new Promise<void>((resolve) => {
      service.askStream({} as never).subscribe({ next: (e) => events.push(e), complete: resolve });
    });
    expect(events.find((e) => (e as { type: string }).type === 'bizarre')).toBeUndefined();
  });

  it('aborts on unsubscribe', async () => {
    let aborted = false;
    globalThis.fetch = vi.fn().mockImplementation((_url, init) => {
      (init as { signal?: AbortSignal }).signal?.addEventListener('abort', () => {
        aborted = true;
      });
      return new Promise(() => { /* never resolve */ });
    });
    const sub = service.askStream({} as never).subscribe();
    sub.unsubscribe();
    expect(aborted).toBe(true);
  });
});
