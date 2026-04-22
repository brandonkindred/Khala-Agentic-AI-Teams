import { Injectable } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';
import { map } from 'rxjs/operators';
import type {
  BrandActivity,
  BrandActivityKind,
  BrandActivityStatus,
} from '../models';
import type { BrandJobListItem, BrandJobStatus } from './branding-api.service';

/**
 * Client-side activity store for the per-brand activity strip. Holds a flat
 * list of {@link BrandActivity} entries; callers filter per brand at the
 * render edge. State is in-memory only — a page reload rehydrates running
 * entries from `GET /branding/jobs?running_only=true` and drops finished
 * chips.
 */
@Injectable({ providedIn: 'root' })
export class BrandActivityService {
  private readonly subject = new BehaviorSubject<BrandActivity[]>([]);

  /** Reactive view of activities for a single brand, sorted newest first. */
  forBrand(brandId: string): Observable<BrandActivity[]> {
    return this.subject.pipe(
      map((list) =>
        list
          .filter((a) => a.brandId === brandId)
          .sort((a, b) => b.startedAt.localeCompare(a.startedAt))
      )
    );
  }

  /** Current snapshot; used by the dashboard for imperative retry/open flows. */
  snapshot(): BrandActivity[] {
    return this.subject.getValue();
  }

  /** Create and return a new activity in `queued` state. */
  start(kind: BrandActivityKind, brandId: string, jobId?: string | null): BrandActivity {
    const activity: BrandActivity = {
      id: crypto.randomUUID(),
      brandId,
      kind,
      status: 'queued',
      jobId: jobId ?? null,
      startedAt: new Date().toISOString(),
    };
    this.subject.next([...this.subject.getValue(), activity]);
    return activity;
  }

  /** Merge a partial patch into an existing activity. No-op if the id is gone. */
  update(id: string, patch: Partial<Omit<BrandActivity, 'id' | 'brandId' | 'kind'>>): void {
    const list = this.subject.getValue();
    const idx = list.findIndex((a) => a.id === id);
    if (idx === -1) return;
    const merged: BrandActivity = { ...list[idx], ...patch };
    const next = list.slice();
    next[idx] = merged;
    this.subject.next(next);
  }

  remove(id: string): void {
    this.subject.next(this.subject.getValue().filter((a) => a.id !== id));
  }

  /**
   * Seed the store with running jobs fetched from the backend on page load,
   * so a refresh during an in-flight run does not hide the chip. Jobs whose
   * brand is not in `knownBrandIds` are ignored — they belong to a different
   * workspace.
   */
  hydrateFromJobs(jobs: BrandJobListItem[], knownBrandIds: Set<string>): void {
    const runningLike: BrandActivityStatus[] = ['queued', 'running'];
    const existing = new Set(this.subject.getValue().map((a) => a.jobId).filter((j): j is string => !!j));
    const additions: BrandActivity[] = [];
    for (const job of jobs) {
      if (!job.brand_id || !knownBrandIds.has(job.brand_id)) continue;
      if (existing.has(job.job_id)) continue;
      const status = mapJobStatus(job.status);
      if (!runningLike.includes(status)) continue;
      additions.push({
        id: crypto.randomUUID(),
        brandId: job.brand_id,
        kind: 'run',
        status,
        jobId: job.job_id,
        startedAt: job.created_at ?? new Date().toISOString(),
      });
    }
    if (additions.length) {
      this.subject.next([...this.subject.getValue(), ...additions]);
    }
  }

  /** Apply a polled job status to the matching activity chip. */
  applyJobStatus(activityId: string, status: BrandJobStatus): void {
    const mapped = mapJobStatus(status.status);
    const isTerminal = mapped === 'completed' || mapped === 'failed' || mapped === 'cancelled';
    this.update(activityId, {
      status: mapped,
      phase: status.current_phase ?? null,
      progress: status.progress ?? null,
      error: status.error ?? null,
      completedAt: isTerminal ? status.updated_at ?? new Date().toISOString() : null,
    });
  }
}

function mapJobStatus(status: string): BrandActivityStatus {
  switch (status) {
    case 'completed':
      return 'completed';
    case 'failed':
      return 'failed';
    case 'cancelled':
      return 'cancelled';
    case 'running':
      return 'running';
    default:
      return 'queued';
  }
}
