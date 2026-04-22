import { firstValueFrom } from 'rxjs';
import { first } from 'rxjs/operators';
import { BrandActivityService } from './brand-activity.service';
import type { BrandJobListItem, BrandJobStatus } from './branding-api.service';

describe('BrandActivityService', () => {
  let service: BrandActivityService;

  beforeEach(() => {
    service = new BrandActivityService();
  });

  it('start() creates an activity in queued state and emits it on forBrand()', async () => {
    const activity = service.start('run', 'b1');
    expect(activity.status).toBe('queued');
    expect(activity.brandId).toBe('b1');
    const list = await firstValueFrom(service.forBrand('b1').pipe(first()));
    expect(list.map((a) => a.id)).toEqual([activity.id]);
  });

  it('update() merges patches without touching identity fields', () => {
    const a = service.start('run', 'b1');
    service.update(a.id, { status: 'running', phase: 'Visual Identity' });
    const [after] = service.snapshot();
    expect(after.status).toBe('running');
    expect(after.phase).toBe('Visual Identity');
    expect(after.id).toBe(a.id);
    expect(after.brandId).toBe('b1');
    expect(after.kind).toBe('run');
  });

  it('remove() drops the activity', () => {
    const a = service.start('run', 'b1');
    service.remove(a.id);
    expect(service.snapshot()).toHaveLength(0);
  });

  it('forBrand() isolates activities by brand and sorts newest first', async () => {
    const a1 = service.start('run', 'b1');
    // Force distinct timestamps so sort is deterministic.
    service.update(a1.id, { startedAt: '2020-01-01T00:00:00Z' });
    const a2 = service.start('research', 'b1');
    service.update(a2.id, { startedAt: '2020-01-02T00:00:00Z' });
    service.start('run', 'b2');
    const list = await firstValueFrom(service.forBrand('b1').pipe(first()));
    expect(list.map((a) => a.id)).toEqual([a2.id, a1.id]);
  });

  it('applyJobStatus() maps running -> running and preserves phase/progress', () => {
    const a = service.start('run', 'b1');
    const poll: BrandJobStatus = {
      job_id: 'j1',
      status: 'running',
      current_phase: 'Visual Identity',
      progress: 42,
    };
    service.applyJobStatus(a.id, poll);
    const [after] = service.snapshot();
    expect(after.status).toBe('running');
    expect(after.phase).toBe('Visual Identity');
    expect(after.progress).toBe(42);
    expect(after.completedAt).toBeNull();
  });

  it('applyJobStatus() marks completed with a completedAt timestamp', () => {
    const a = service.start('run', 'b1');
    service.applyJobStatus(a.id, {
      job_id: 'j1',
      status: 'completed',
      updated_at: '2026-04-22T12:00:00Z',
    });
    const [after] = service.snapshot();
    expect(after.status).toBe('completed');
    expect(after.completedAt).toBe('2026-04-22T12:00:00Z');
  });

  it('hydrateFromJobs() adopts running jobs for known brands', () => {
    const jobs: BrandJobListItem[] = [
      { job_id: 'j1', status: 'running', brand_id: 'b1', created_at: '2026-04-22T10:00:00Z' },
      { job_id: 'j2', status: 'running', brand_id: 'b-other' }, // wrong workspace
      { job_id: 'j3', status: 'completed', brand_id: 'b1' }, // terminal
    ];
    service.hydrateFromJobs(jobs, new Set(['b1']));
    const snap = service.snapshot();
    expect(snap).toHaveLength(1);
    expect(snap[0].jobId).toBe('j1');
    expect(snap[0].kind).toBe('run');
    expect(snap[0].status).toBe('running');
  });

  it('hydrateFromJobs() does not duplicate an already-tracked job', () => {
    const a = service.start('run', 'b1', 'j1');
    service.update(a.id, { status: 'running' });
    service.hydrateFromJobs(
      [{ job_id: 'j1', status: 'running', brand_id: 'b1' }],
      new Set(['b1'])
    );
    expect(service.snapshot()).toHaveLength(1);
  });
});
