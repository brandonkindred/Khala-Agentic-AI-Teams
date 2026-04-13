/**
 * Unified job action dispatcher.
 *
 * Routes stop, resume, restart, and delete through per-team API services
 * when the team exposes dedicated endpoints.  Falls back to the generic
 * job management proxy (/api/jobs/{team}/{id}) for teams that don't.
 */
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import type { JobSource } from '../models';
import { SoftwareEngineeringApiService } from './software-engineering-api.service';
import { BloggingApiService } from './blogging-api.service';
import { AISystemsApiService } from './ai-systems-api.service';
import { AgentProvisioningApiService } from './agent-provisioning-api.service';
import { SocialMarketingApiService } from './social-marketing-api.service';
import { InvestmentApiService } from './investment-api.service';
import { PersonaTestingApiService } from './persona-testing-api.service';
import { GenericJobsApiService } from './generic-jobs-api.service';

/** Map JobSource values to the job-service team name used in /api/jobs/{team}. */
const SOURCE_TO_TEAM: Record<string, string> = {
  software_engineering: 'software_engineering_team',
  blogging: 'blogging_team',
  ai_systems: 'ai_systems_team',
  agent_provisioning: 'agent_provisioning_team',
  social_marketing: 'social_media_marketing_team',
  investment: 'investment_team',
  investment_strategy_lab_runs: 'investment_strategy_lab_runs',
  user_agent_founder: 'user_agent_founder',
};

@Injectable({ providedIn: 'root' })
export class JobActionsService {
  private readonly se = inject(SoftwareEngineeringApiService);
  private readonly blogging = inject(BloggingApiService);
  private readonly ai = inject(AISystemsApiService);
  private readonly prov = inject(AgentProvisioningApiService);
  private readonly social = inject(SocialMarketingApiService);
  private readonly investment = inject(InvestmentApiService);
  private readonly persona = inject(PersonaTestingApiService);
  private readonly generic = inject(GenericJobsApiService);

  stop(source: JobSource, jobId: string): Observable<unknown> {
    switch (source) {
      case 'software_engineering': return this.se.cancelJob(jobId);
      case 'blogging': return this.blogging.cancelJob(jobId);
      case 'ai_systems': return this.ai.cancelJob(jobId);
      case 'agent_provisioning': return this.prov.cancelJob(jobId);
      case 'social_marketing': return this.social.cancelJob(jobId);
      case 'user_agent_founder': return this.persona.cancelJob(jobId);
      default: return this.generic.cancel(SOURCE_TO_TEAM[source] ?? source, jobId);
    }
  }

  resume(source: JobSource, jobId: string): Observable<unknown> {
    switch (source) {
      case 'software_engineering': return this.se.resumeRunTeamJob(jobId);
      case 'blogging': return this.blogging.resumeJob(jobId);
      case 'ai_systems': return this.ai.resumeJob(jobId);
      case 'agent_provisioning': return this.prov.resumeJob(jobId);
      case 'social_marketing': return this.social.resumeJob(jobId);
      case 'investment': return this.investment.resumeRun(jobId);
      default: return this.generic.cancel(SOURCE_TO_TEAM[source] ?? source, jobId);
    }
  }

  restart(source: JobSource, jobId: string): Observable<unknown> {
    switch (source) {
      case 'software_engineering': return this.se.restartRunTeamJob(jobId);
      case 'blogging': return this.blogging.restartJob(jobId);
      case 'ai_systems': return this.ai.restartJob(jobId);
      case 'agent_provisioning': return this.prov.restartJob(jobId);
      case 'social_marketing': return this.social.restartJob(jobId);
      case 'investment': return this.investment.restartRun(jobId);
      default: return this.generic.cancel(SOURCE_TO_TEAM[source] ?? source, jobId);
    }
  }

  delete(source: JobSource, jobId: string): Observable<unknown> {
    switch (source) {
      case 'software_engineering': return this.se.deleteJob(jobId);
      case 'blogging': return this.blogging.deleteJob(jobId);
      case 'ai_systems': return this.ai.deleteJob(jobId);
      case 'agent_provisioning': return this.prov.deleteJob(jobId);
      case 'social_marketing': return this.social.deleteJob(jobId);
      case 'investment': return this.investment.deleteJob(jobId);
      case 'user_agent_founder': return this.persona.deleteJob(jobId);
      default: return this.generic.delete(SOURCE_TO_TEAM[source] ?? source, jobId);
    }
  }
}
