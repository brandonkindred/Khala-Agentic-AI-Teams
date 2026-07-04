import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import { createTeamAssistantApiMock } from '../../testing/team-assistant.mock';
import { MarketResearchDashboardComponent } from './market-research-dashboard.component';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('MarketResearchDashboardComponent a11y', () => {
  it('has no axe violations in the dashboard shell', async () => {
    await TestBed.configureTestingModule({
      imports: [MarketResearchDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: MarketResearchApiService, useValue: { health: vi.fn().mockReturnValue(of({ status: 'ok' })) } },
        { provide: TeamAssistantApiService, useValue: createTeamAssistantApiMock() },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(MarketResearchDashboardComponent);
    fixture.detectChanges();

    // Guard: the shell title rendered and the embedded assistant actually loaded
    // a conversation (an assistant message painted) — so axe audits the real,
    // populated surface, not a bare-mounted or errored chat.
    expect(fixture.nativeElement.querySelector('app-dashboard-shell h1')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-team-assistant-chat .message.assistant')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
