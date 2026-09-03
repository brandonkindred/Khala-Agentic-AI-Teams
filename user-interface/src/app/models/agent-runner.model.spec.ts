import { describe, it, expect } from 'vitest';
import { isInvokeEnvelope, type InvokeEnvelope } from './agent-runner.model';

describe('isInvokeEnvelope', () => {
  it('returns true for a valid envelope shape', () => {
    const value: InvokeEnvelope = { output: { ok: true }, duration_ms: 1, trace_id: 't', logs_tail: [] };
    expect(isInvokeEnvelope(value)).toBe(true);
  });

  it('returns false when trace_id is missing', () => {
    expect(isInvokeEnvelope({ logs_tail: [] })).toBe(false);
  });

  it('returns false when logs_tail is missing', () => {
    expect(isInvokeEnvelope({ trace_id: 't' })).toBe(false);
  });

  it('returns false when trace_id has the wrong type', () => {
    expect(isInvokeEnvelope({ trace_id: 123, logs_tail: [] })).toBe(false);
  });

  it('returns false when logs_tail is not an array', () => {
    expect(isInvokeEnvelope({ trace_id: 't', logs_tail: 'not-an-array' })).toBe(false);
  });

  it('returns false for null, primitives, and arrays', () => {
    expect(isInvokeEnvelope(null)).toBe(false);
    expect(isInvokeEnvelope(undefined)).toBe(false);
    expect(isInvokeEnvelope('a string')).toBe(false);
    expect(isInvokeEnvelope(42)).toBe(false);
    expect(isInvokeEnvelope(['t', []])).toBe(false);
  });
});
