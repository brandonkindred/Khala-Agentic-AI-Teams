import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { JobActionsService } from './job-actions.service';
import { SoftwareEngineeringApiService } from './software-engineering-api.service';
import { BloggingApiService } from './blogging-api.service';
import { AISystemsApiService } from './ai-systems-api.service';
import { AgentProvisioningApiService } from './agent-provisioning-api.service';
import { SocialMarketingApiService } from './social-marketing-api.service';
import { InvestmentApiService } from './investment-api.service';
import { PersonaTestingApiService } from './persona-testing-api.service';
import { SalesApiService } from './sales-api.service';
import { GenericJobsApiService } from './generic-jobs-api.service';

describe('JobActionsService', () => {
  let svc: JobActionsService;
  let se: SoftwareEngineeringApiService;
  let blogging: BloggingApiService;
  let ai: AISystemsApiService;
  let prov: AgentProvisioningApiService;
  let social: SocialMarketingApiService;
  let investment: InvestmentApiService;
  let persona: PersonaTestingApiService;
  let sales: SalesApiService;
  let generic: GenericJobsApiService;

  beforeEach(() => {
    const stub = () => ({
      cancelJob: vi.fn(() => of({})),
      deleteJob: vi.fn(() => of({})),
      resumeJob: vi.fn(() => of({})),
      restartJob: vi.fn(() => of({})),
      resumeRunTeamJob: vi.fn(() => of({})),
      restartRunTeamJob: vi.fn(() => of({})),
      resumeRun: vi.fn(() => of({})),
      restartRun: vi.fn(() => of({})),
      cancel: vi.fn(() => of({})),
      delete: vi.fn(() => of({})),
      resume: vi.fn(() => of({})),
      restart: vi.fn(() => of({})),
    });
    TestBed.configureTestingModule({
      providers: [
        JobActionsService,
        { provide: SoftwareEngineeringApiService, useValue: stub() },
        { provide: BloggingApiService, useValue: stub() },
        { provide: AISystemsApiService, useValue: stub() },
        { provide: AgentProvisioningApiService, useValue: stub() },
        { provide: SocialMarketingApiService, useValue: stub() },
        { provide: InvestmentApiService, useValue: stub() },
        { provide: PersonaTestingApiService, useValue: stub() },
        { provide: SalesApiService, useValue: stub() },
        { provide: GenericJobsApiService, useValue: stub() },
      ],
    });
    svc = TestBed.inject(JobActionsService);
    se = TestBed.inject(SoftwareEngineeringApiService);
    blogging = TestBed.inject(BloggingApiService);
    ai = TestBed.inject(AISystemsApiService);
    prov = TestBed.inject(AgentProvisioningApiService);
    social = TestBed.inject(SocialMarketingApiService);
    investment = TestBed.inject(InvestmentApiService);
    persona = TestBed.inject(PersonaTestingApiService);
    sales = TestBed.inject(SalesApiService);
    generic = TestBed.inject(GenericJobsApiService);
  });

  it.each([
    ['software_engineering', 'se'],
    ['blogging', 'blogging'],
    ['ai_systems', 'ai'],
    ['agent_provisioning', 'prov'],
    ['social_marketing', 'social'],
    ['user_agent_founder', 'persona'],
    ['sales', 'sales'],
  ])('stop routes %s', (source, _team) => {
    svc.stop(source as never, 'j1').subscribe();
    const targets = { se, blogging, ai, prov, social, persona, sales };
    expect((targets[_team as keyof typeof targets].cancelJob as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('j1');
  });

  it('stop falls back to generic for unknown source', () => {
    svc.stop('investment' as never, 'j1').subscribe();
    expect((generic.cancel as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('investment_strategy_lab_runs', 'j1');
  });

  it('stop generic passes through unmapped source verbatim', () => {
    svc.stop('mystery' as never, 'j1').subscribe();
    expect((generic.cancel as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('mystery', 'j1');
  });

  it.each([
    ['software_engineering', 'se', 'resumeRunTeamJob'],
    ['blogging', 'blogging', 'resumeJob'],
    ['ai_systems', 'ai', 'resumeJob'],
    ['agent_provisioning', 'prov', 'resumeJob'],
    ['social_marketing', 'social', 'resumeJob'],
    ['investment', 'investment', 'resumeRun'],
    ['user_agent_founder', 'persona', 'resumeJob'],
  ])('resume routes %s', (source, team, method) => {
    svc.resume(source as never, 'j1').subscribe();
    const targets = { se, blogging, ai, prov, social, investment, persona };
    expect((targets[team as keyof typeof targets][method as 'resumeJob'] as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('j1');
  });

  it('resume falls back to generic for unmapped source', () => {
    svc.resume('mystery' as never, 'j1').subscribe();
    expect((generic.resume as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('mystery', 'j1');
  });

  it.each([
    ['software_engineering', 'se', 'restartRunTeamJob'],
    ['blogging', 'blogging', 'restartJob'],
    ['ai_systems', 'ai', 'restartJob'],
    ['agent_provisioning', 'prov', 'restartJob'],
    ['social_marketing', 'social', 'restartJob'],
    ['investment', 'investment', 'restartRun'],
    ['user_agent_founder', 'persona', 'restartJob'],
  ])('restart routes %s', (source, team, method) => {
    svc.restart(source as never, 'j1').subscribe();
    const targets = { se, blogging, ai, prov, social, investment, persona };
    expect((targets[team as keyof typeof targets][method as 'restartJob'] as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('j1');
  });

  it('restart falls back to generic', () => {
    svc.restart('mystery' as never, 'j1').subscribe();
    expect((generic.restart as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('mystery', 'j1');
  });

  it.each([
    ['software_engineering', 'se'],
    ['blogging', 'blogging'],
    ['ai_systems', 'ai'],
    ['agent_provisioning', 'prov'],
    ['social_marketing', 'social'],
    ['investment', 'investment'],
    ['user_agent_founder', 'persona'],
    ['sales', 'sales'],
  ])('delete routes %s', (source, _team) => {
    svc.delete(source as never, 'j1').subscribe();
    const targets = { se, blogging, ai, prov, social, investment, persona, sales };
    expect((targets[_team as keyof typeof targets].deleteJob as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('j1');
  });

  it('delete falls back to generic', () => {
    svc.delete('mystery' as never, 'j1').subscribe();
    expect((generic.delete as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith('mystery', 'j1');
  });
});
