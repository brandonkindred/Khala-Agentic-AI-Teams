import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import type {
  AgentEvent,
  DeepthoughtRequest,
  DeepthoughtResponse,
} from '../models/deepthought.model';

/** Discriminated union for SSE stream emissions. */
export type StreamEvent =
  | { type: 'agent_event'; payload: AgentEvent }
  | { type: 'result'; payload: DeepthoughtResponse }
  | { type: 'error'; payload: string }
  | { type: 'done' };

@Injectable({ providedIn: 'root' })
export class DeepthoughtApiService {
  private readonly baseUrl = environment.deepthoughtApiUrl;

  /**
   * Stream agent events via SSE from the POST endpoint.
   *
   * Uses `fetch()` + `ReadableStream` because `EventSource` only supports GET.
   * Returns an Observable that emits typed `StreamEvent` items and completes
   * when the server sends the `done` event or the connection closes.
   */
  askStream(request: DeepthoughtRequest): Observable<StreamEvent> {
    const url = `${this.baseUrl}/deepthought/ask/stream`;

    return new Observable<StreamEvent>((subscriber) => {
      const controller = new AbortController();

      fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
        signal: controller.signal,
      })
        .then((res) => {
          if (!res.ok) {
            subscriber.next({ type: 'error', payload: `HTTP ${res.status}: ${res.statusText}` });
            subscriber.complete();
            return;
          }
          const reader = res.body!.getReader();
          const decoder = new TextDecoder();
          let buffer = '';

          const pump = (): Promise<void> =>
            reader.read().then(({ done, value }) => {
              if (done) {
                subscriber.next({ type: 'done' });
                subscriber.complete();
                return;
              }

              buffer += decoder.decode(value, { stream: true });
              const blocks = buffer.split('\n\n');
              // Keep last incomplete block in buffer
              buffer = blocks.pop() ?? '';

              for (const block of blocks) {
                const parsed = parseSSEBlock(block);
                if (parsed) {
                  subscriber.next(parsed);
                  if (parsed.type === 'done') {
                    subscriber.complete();
                    return;
                  }
                }
              }

              return pump();
            });

          pump().catch((err) => {
            if (err.name !== 'AbortError') {
              subscriber.next({ type: 'error', payload: String(err) });
              subscriber.complete();
            }
          });
        })
        .catch((err) => {
          if (err.name !== 'AbortError') {
            subscriber.next({ type: 'error', payload: String(err) });
            subscriber.complete();
          }
        });

      // Teardown: abort the fetch when unsubscribed
      return () => controller.abort();
    });
  }
}

/** Parse a single SSE text block into a typed StreamEvent. */
function parseSSEBlock(block: string): StreamEvent | null {
  let eventType = '';
  let data = '';

  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) {
      eventType = line.slice(7).trim();
    } else if (line.startsWith('data: ')) {
      data = line.slice(6);
    }
  }

  if (!eventType) return null;

  try {
    switch (eventType) {
      case 'agent_event':
        return { type: 'agent_event', payload: JSON.parse(data) as AgentEvent };
      case 'result':
        return { type: 'result', payload: JSON.parse(data) as DeepthoughtResponse };
      case 'error':
        return { type: 'error', payload: JSON.parse(data).error ?? data };
      case 'done':
        return { type: 'done' };
      default:
        return null;
    }
  } catch {
    return null;
  }
}
