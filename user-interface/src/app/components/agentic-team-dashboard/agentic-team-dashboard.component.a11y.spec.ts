import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { AgenticTeamDashboardComponent } from './agentic-team-dashboard.component';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('AgenticTeamDashboardComponent a11y', () => {
  const summary = {
    team_id: 't1',
    name: 'Growth',
    description: 'Growth experiments team',
    process_count: 2,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };

  const setup = async (teams: (typeof summary)[]) => {
    const api = { listTeams: vi.fn().mockReturnValue(of(teams)), createTeam: vi.fn() };
    await TestBed.configureTestingModule({
      imports: [AgenticTeamDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: AgenticTeamApiService, useValue: api }],
    }).compileComponents();
  };

  it('has no axe violations with the team list rendered', async () => {
    await setup([summary]);

    const fixture = TestBed.createComponent(AgenticTeamDashboardComponent);
    fixture.detectChanges();

    // Guard: a team card rendered, so axe audits the populated grid.
    expect(fixture.nativeElement.querySelector('.team-card')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });

  it('has no axe violations in the empty state', async () => {
    await setup([]);

    const fixture = TestBed.createComponent(AgenticTeamDashboardComponent);
    fixture.detectChanges();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
