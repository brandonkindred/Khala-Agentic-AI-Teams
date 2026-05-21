import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach } from 'vitest';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentDashboardComponent } from './investment-dashboard.component';
import type { IPS, PortfolioProposal, PromotionDecision, StrategySpec } from '../../models';

describe('InvestmentDashboardComponent (extra coverage)', () => {
  let component: InvestmentDashboardComponent;
  let fixture: ComponentFixture<InvestmentDashboardComponent>;
  let api: {
    healthCheck: ReturnType<typeof vi.fn>;
    getProfile: ReturnType<typeof vi.fn>;
  };

  const setup = async (routeData: Record<string, unknown>) => {
    api = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getProfile: vi.fn().mockReturnValue(of({ found: true, ips: { profile_id: 'p1' } as IPS })),
    };
    // Add additional stubs for child components
    Object.assign(api, {
      getStrategyLabConfig: vi.fn().mockReturnValue(of({})),
      listStrategyLabJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      runStrategyLab: vi.fn(),
      getStrategyLabResults: vi.fn().mockReturnValue(of({ results: [], total: 0 })),
      getStrategyLabJob: vi.fn().mockReturnValue(of({})),
      cancelStrategyLabJob: vi.fn().mockReturnValue(of({})),
    });
    await TestBed.configureTestingModule({
      imports: [InvestmentDashboardComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { snapshot: { data: routeData } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(InvestmentDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  };

  beforeEach(() => TestBed.resetTestingModule());

  it('initial route focus advisor switches to forms (state only)', async () => {
    // Tested via internal state — skipping detectChanges path because
    // strategy-lab child triggers many additional API calls in forms view.
    api = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      getProfile: vi.fn().mockReturnValue(of({ found: false })),
    };
    Object.assign(api, {
      getStrategyLabConfig: vi.fn().mockReturnValue(of({})),
      listStrategyLabJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      runStrategyLab: vi.fn(),
      getStrategyLabResults: vi.fn().mockReturnValue(of({ results: [], total: 0 })),
      getStrategyLabJob: vi.fn().mockReturnValue(of({})),
      cancelStrategyLabJob: vi.fn().mockReturnValue(of({})),
      getPaperTradingResults: vi.fn().mockReturnValue(of({})),
      listPaperTradingJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
    });
    await TestBed.configureTestingModule({
      imports: [InvestmentDashboardComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { snapshot: { data: { investmentFocus: 'advisor' } } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(InvestmentDashboardComponent);
    component = fixture.componentInstance;
    // Run ngOnInit without detectChanges for child renders
    component.ngOnInit();
    expect(component.routeFocus).toBe('advisor');
    expect(component.viewMode).toBe('forms');
  });

  it('default route focus uses chat mode', async () => {
    await setup({});
    expect(component.routeFocus).toBe('default');
    expect(component.viewMode).toBe('chat');
  });

  it('onProposalCreated stores proposal', async () => {
    await setup({});
    const p = { id: 'p1' } as PortfolioProposal;
    component.onProposalCreated(p);
    expect(component.currentProposal).toBe(p);
  });

  it('onStrategyCreated stores strategy', async () => {
    await setup({});
    const s = { id: 's1' } as StrategySpec;
    component.onStrategyCreated(s);
    expect(component.currentStrategy).toBe(s);
  });

  it('onDecisionMade stores decision', async () => {
    await setup({});
    const d = { id: 'd1' } as PromotionDecision;
    component.onDecisionMade(d);
    expect(component.lastDecision).toBe(d);
  });

  it('loadProfile sets currentIPS when found', async () => {
    await setup({});
    component.loadProfile('user-1');
    expect(api.getProfile).toHaveBeenCalledWith('user-1');
    expect(component.currentIPS?.profile_id).toBe('p1');
  });

  it('loadProfile skips when not found', async () => {
    await setup({});
    api.getProfile.mockReturnValue(of({ found: false }));
    component.currentIPS = null;
    component.loadProfile('user-2');
    expect(component.currentIPS).toBeNull();
  });

  it('clearProfile resets state', async () => {
    await setup({});
    component.currentIPS = { profile_id: 'p1' } as IPS;
    component.currentProposal = { id: 'p1' } as PortfolioProposal;
    component.currentStrategy = { id: 's1' } as StrategySpec;
    component.lastDecision = { id: 'd1' } as PromotionDecision;
    component.clearProfile();
    expect(component.currentIPS).toBeNull();
    expect(component.currentProposal).toBeNull();
    expect(component.currentStrategy).toBeNull();
    expect(component.lastDecision).toBeNull();
  });
});
