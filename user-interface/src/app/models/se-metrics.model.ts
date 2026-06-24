/**
 * Software Engineering DORA metrics + cost.
 * Mirrors the backend `DoraMetrics.to_dict()` returned by
 * `GET /api/software-engineering/dora` (and the `/api/se/metrics` alias).
 */
export interface SeMetrics {
  window_days: number;
  /** ISO-8601 timestamp of when the metrics were computed. */
  computed_at: string;

  /** Merges-to-main in the window (deployment frequency numerator). */
  deployment_count: number;
  deployment_frequency_per_day: number;

  /** Median seconds from task creation to merge; `null` when no samples. */
  lead_time_seconds_median: number | null;
  lead_time_sample_count: number;

  merged_count: number;
  gate_reentry_count: number;
  /** gate re-entries / merged tasks, in [0, 1]. */
  change_failure_rate: number;

  /** Median seconds from agent crash to resolution; `null` when no samples. */
  mttr_seconds_median: number | null;
  crash_resolved_count: number;

  total_cost_usd: number;
  /** Per-job cost breakdown, keyed by job id. */
  cost_by_job: Record<string, number>;
}
