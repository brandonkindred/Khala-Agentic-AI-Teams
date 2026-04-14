/**
 * Unified job model for the Jobs dashboard across all teams.
 * Each team's list response is mapped to this shape for a single table.
 */
import type { RunningJobSummary } from './software-engineering.model';
import type { BlogJobListItem } from './blogging.model';
import type { AISystemJobSummary } from './ai-systems.model';
import type { ProvisionJobSummary } from './agent-provisioning.model';
import type { MarketingJobListItem } from './social-marketing.model';
import type { InvestmentJobSummary } from './investment.model';
import type { SalesPipelineJobListItem } from './sales-team.model';
import type { PlanningV3JobSummary } from './planning-v3.model';
import type { GenericJobRecord } from '../services/generic-jobs-api.service';

export type JobSource =
  | 'software_engineering'
  | 'blogging'
  | 'ai_systems'
  | 'agent_provisioning'
  | 'social_marketing'
  | 'investment'
  | 'user_agent_founder'
  | 'soc2_compliance'
  | 'personal_assistant'
  | 'planning_v3'
  | 'road_trip_planning'
  | 'nutrition_meal_planning'
  | 'coding_team'
  | 'sales';

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
    jobType: s.job_type ?? undefined,
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

export function fromInvestmentJobSummary(s: InvestmentJobSummary): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'investment',
    label: s.label || 'Strategy Lab',
    createdAt: s.created_at,
    progress: s.progress,
    phase: s.current_phase,
  };
}

export interface FounderJobSummary {
  job_id: string;
  status: string;
  label?: string;
  current_phase?: string;
  created_at?: string;
  error?: string;
}

export function fromFounderJobSummary(s: FounderJobSummary): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'user_agent_founder',
    label: s.label ?? 'Persona: founder workflow',
    createdAt: s.created_at,
    phase: s.current_phase,
  };
}

export function fromSalesJobListItem(s: SalesPipelineJobListItem): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'sales',
    label: s.product_name || 'Sales pipeline',
    createdAt: s.created_at,
    progress: s.progress,
    phase: s.current_stage,
  };
}

export function fromPlanningV3JobSummary(s: PlanningV3JobSummary): UnifiedJobSummary {
  return {
    jobId: s.job_id,
    status: s.status,
    source: 'planning_v3',
    label: getRepoName(s.repo_path) || 'Planning V3',
    createdAt: undefined,
    repoPath: s.repo_path,
    phase: s.current_phase,
  };
}

export function fromGenericJobRecord(source: JobSource, s: GenericJobRecord): UnifiedJobSummary {
  // The job service flattens `data` into the top-level object, so fields like
  // progress, current_phase, label, repo_path are top-level — not nested under `data`.
  const r = s as unknown as Record<string, unknown>;
  return {
    jobId: s.job_id,
    status: s.status,
    source,
    label: (r['label'] as string) ?? (r['repo_path'] as string) ?? s.job_id,
    createdAt: s.created_at,
    progress: r['progress'] as number | undefined,
    phase: (r['current_phase'] as string) ?? (r['current_stage'] as string),
    repoPath: r['repo_path'] as string | undefined,
  };
}

/** Team display metadata for the Team column and navigation. */
export const SOURCE_DISPLAY: Record<
  JobSource,
  { label: string; icon: string; route: string }
> = {
  software_engineering: { label: 'Software Engineering', icon: 'code', route: '/software-engineering' },
  blogging: { label: 'Blogging', icon: 'article', route: '/blogging/dashboard' },
  ai_systems: { label: 'AI Systems', icon: 'smart_toy', route: '/ai-systems' },
  agent_provisioning: { label: 'Agent Provisioning', icon: 'settings', route: '/agent-provisioning' },
  social_marketing: { label: 'Social Marketing', icon: 'campaign', route: '/social-marketing' },
  investment: { label: 'Investment', icon: 'trending_up', route: '/investment/strategy-lab' },
  user_agent_founder: { label: 'Persona Testing', icon: 'person_search', route: '/persona-testing' },
  soc2_compliance: { label: 'SOC2 Compliance', icon: 'verified_user', route: '/soc2-compliance' },
  personal_assistant: { label: 'Personal Assistant', icon: 'assistant', route: '/personal-assistant' },
  planning_v3: { label: 'Planning V3', icon: 'description', route: '/software-engineering/planning-v3' },
  road_trip_planning: { label: 'Road Trip', icon: 'directions_car', route: '/dashboard' },
  nutrition_meal_planning: { label: 'Nutrition', icon: 'restaurant', route: '/nutrition' },
  coding_team: { label: 'Coding Team', icon: 'terminal', route: '/software-engineering/coding-team' },
  sales: { label: 'Sales', icon: 'storefront', route: '/sales' },
};
