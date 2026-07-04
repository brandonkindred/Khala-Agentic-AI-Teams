import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { axe } from 'vitest-axe';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import { createTeamAssistantApiMock } from '../../testing/team-assistant.mock';
import { SocialMarketingDashboardComponent } from './social-marketing-dashboard.component';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('SocialMarketingDashboardComponent a11y', () => {
  it('has no axe violations in the dashboard shell', async () => {
    await TestBed.configureTestingModule({
      imports: [SocialMarketingDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: TeamAssistantApiService, useValue: createTeamAssistantApiMock() },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(SocialMarketingDashboardComponent);
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
