/**
 * Unified job model for the Jobs dashboard across all teams.
 * Each team's list response is mapped to this shape for a single table.
 */
import type { RunningJobSummary } from './software-engineering.model';
import type { BlogJobListItem } from './blogging.model';
import type { AISystemJobSummary } from './ai-systems.model';
import type { ProvisionJobSummary } from './agent-provisioning.model';
import type { MarketingJobListItem } from './social-marketing.model';

export type JobSource =
  | 'software_engineering'
  | 'blogging'
  | 'ai_systems'
  | 'agent_provisioning'
  | 'social_marketing';

export interface UnifiedJobSummary {
  jobId: string;
  status: string;
  source: JobSource;
  label: string;
  createdAt?: string;
  progress?: number;
  phase?: string;
  repoPath?: string;
  jobType?: string;
}

/** Per-team status for SE jobs (dashboard display). */
export interface TeamStatus {
  teamId: string;
  label: string;
  icon: string;
  phase: string;
  phaseLabel: string;
  isActive: boolean;
}

/** Extended detail for software-engineering jobs only (from detail API). */
export interface SEDetail {
  progress?: number;
  statusText?: string;
  currentPhase?: string;
  waitingForAnswers?: boolean;
  teamStatuses?: TeamStatus[];
}

/** One row in the Jobs dashboard: unified summary + optional SE-only detail. */
export interface DashboardRow {
  unified: UnifiedJobSummary;
  seDetail?: SEDetail;
}

function getRepoName(repoPath?: string): string {
  if (!repoPath) return 'Unknown';
  const parts = repoPath.split('/');
  return parts[parts.length - 1] || repoPath;
}

export function fromRunningJobSummary(s: RunningJobSummary): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'software_engineering',
    label: getRepoName(s.repo_path),
    createdAt: s.created_at,
    repoPath: s.repo_path,
    jobType: s.job_type,
  };
}

export function fromBlogJobListItem(s: BlogJobListItem): UnifiedJobSummary {
  const brief = (s.brief ?? '').slice(0, 80);
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'blogging',
    label: brief || 'Blog pipeline',
    createdAt: s.created_at,
    progress: s.progress,
    phase: s.phase,
  };
}

export function fromAISystemJobSummary(s: AISystemJobSummary): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'ai_systems',
    label: s.project_name ?? 'Build',
    createdAt: s.created_at,
    progress: s.progress,
    phase: s.current_phase,
  };
}

export function fromProvisionJobSummary(s: ProvisionJobSummary): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'agent_provisioning',
    label: s.agent_id ?? 'Provisioning',
    createdAt: s.created_at,
    progress: s.progress,
    phase: s.current_phase,
  };
}

export function fromSocialMarketingJobListItem(s: MarketingJobListItem): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'social_marketing',
    label: s.current_stage ? s.current_stage.replace(/_/g, ' ') : 'Campaign',
    createdAt: s.created_at ?? s.last_updated_at,
    progress: s.progress,
    phase: s.current_stage,
  };
}

/** Team display metadata for the Team column and navigation. */
export const SOURCE_DISPLAY: Record<
  JobSource,
  { label: string; icon: string; route: string }
> = {
  software_engineering: { label: 'Software Engineering', icon: 'code', route: '/software-engineering' },
  blogging: { label: 'Blogging', icon: 'article', route: '/blogging' },
  ai_systems: { label: 'AI Systems', icon: 'smart_toy', route: '/ai-systems' },
  agent_provisioning: { label: 'Agent Provisioning', icon: 'settings', route: '/agent-provisioning' },
  social_marketing: { label: 'Social Marketing', icon: 'campaign', route: '/social-marketing' },
};
