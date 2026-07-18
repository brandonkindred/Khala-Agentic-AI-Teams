import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NEVER, of, throwError } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { InvestmentStrategyLabPageComponent } from './investment-strategy-lab-page.component';

/**
 * `InvestmentStrategyLabPageComponent` renders `<app-strategy-lab />` in its
 * own template, so `StrategyLabComponent` (and its component-provided
 * `StrategyLabRunService`, backed by a real instance here) is constructed
 * too — this mock covers both components' `InvestmentApiService` usage.
 */
function createApiSpy() {
  return {
    healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
    runStrategyLab: vi.fn().mockReturnValue(NEVER),
    streamRunStatus: vi.fn().mockReturnValue(NEVER),
    getStrategyLabConfig: vi.fn().mockReturnValue(
      of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
    ),
    getStrategyLabResults: vi.fn().mockReturnValue(
      of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
    ),
    getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
    getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
  };
}

describe('InvestmentStrategyLabPageComponent', () => {
  let fixture: ComponentFixture<InvestmentStrategyLabPageComponent>;
  let apiSpy: ReturnType<typeof createApiSpy>;

  async function createFixture(): Promise<ComponentFixture<InvestmentStrategyLabPageComponent>> {
    await TestBed.configureTestingModule({
      imports: [InvestmentStrategyLabPageComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        {
          provide: IntegrationsApiService,
          useValue: {
            getTradingViewConfig: vi.fn().mockReturnValue(
              of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
            ),
          },
        },
      ],
    }).compileComponents();

    const f = TestBed.createComponent(InvestmentStrategyLabPageComponent);
    f.detectChanges(); // runs ngOnInit
    return f;
  }

  beforeEach(() => {
    apiSpy = createApiSpy();
  });

  it('starts in the "checking" state before the health check resolves', async () => {
    apiSpy.healthCheck.mockReturnValue(NEVER);
    fixture = await createFixture();

    expect(fixture.componentInstance.healthStatus()).toBe('checking');
    const row: HTMLElement = fixture.nativeElement.querySelector('.health-row');
    expect(row.textContent).toContain('Checking API…');
  });

  it('renders "healthy" once the service reports ok, under OnPush', async () => {
    fixture = await createFixture();

    expect(fixture.componentInstance.healthStatus()).toBe('healthy');
    const row: HTMLElement = fixture.nativeElement.querySelector('.health-row');
    expect(row.getAttribute('class')).toContain('healthy');
    expect(row.textContent).toContain('Investment API online');
  });

  it('renders "unhealthy" when the health check fails, under OnPush', async () => {
    apiSpy.healthCheck.mockReturnValue(throwError(() => new Error('offline')));
    fixture = await createFixture();

    expect(fixture.componentInstance.healthStatus()).toBe('unhealthy');
    const row: HTMLElement = fixture.nativeElement.querySelector('.health-row');
    expect(row.getAttribute('class')).toContain('unhealthy');
    expect(row.textContent).toContain('Investment API offline');
  });

  it('renders the nested Strategy Lab component', async () => {
    fixture = await createFixture();
    expect(fixture.nativeElement.querySelector('app-strategy-lab')).toBeTruthy();
  });

  it('shows the "Strategy Lab" title exactly once — the wrapper\'s <h1>, with the nested component\'s own <h2> suppressed', async () => {
    fixture = await createFixture();
    const h1: HTMLElement = fixture.nativeElement.querySelector('h1');
    expect(h1.textContent?.trim()).toBe('Strategy Lab');
    expect(fixture.nativeElement.querySelector('h2')).toBeNull();

    const region: HTMLElement = fixture.nativeElement.querySelector('.strategy-lab');
    expect(region.getAttribute('role')).toBe('region');
    expect(region.getAttribute('aria-label')).toBe('Strategy Lab');
  });
});
