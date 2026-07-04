import { expectNoAxeViolations } from '../../testing/a11y';
import { renderDashboardShellA11y } from '../../testing/dashboard-a11y';
import { SocialMarketingDashboardComponent } from './social-marketing-dashboard.component';

describe('SocialMarketingDashboardComponent a11y', () => {
  it('has no axe violations in the dashboard shell', async () => {
    const fixture = await renderDashboardShellA11y(SocialMarketingDashboardComponent);

    // Guard: the shell title rendered and the embedded assistant actually loaded
    // a conversation (an assistant message painted) — so axe audits the real,
    // populated surface, not a bare-mounted or errored chat.
    expect(fixture.nativeElement.querySelector('app-dashboard-shell h1')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-team-assistant-chat .message.assistant')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement);
  });
});
