import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { MarketResearchApiService } from '../../services/market-research-api.service';
import { TeamAssistantApiService } from '../../services/team-assistant-api.service';
import { MarketResearchDashboardComponent } from './market-research-dashboard.component';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

// The dashboard shell embeds <app-team-assistant-chat [teamApiUrl]>, which loads
// a conversation on init — stub the service so the chat renders deterministically.
const teamAssistantMock = () => {
  const state = {
    conversation_id: 'c1',
    messages: [{ role: 'assistant', content: 'hi', timestamp: '2025-01-01T00:00:00Z' }],
    context: {},
    suggested_questions: [],
  };
  return {
    getConversation: vi.fn().mockReturnValue(of(state)),
    sendMessage: vi.fn().mockReturnValue(of(state)),
    updateContext: vi.fn().mockReturnValue(of(state)),
    getReadiness: vi.fn().mockReturnValue(of({ ready: false, missing_fields: [] })),
    launch: vi.fn().mockReturnValue(of({ job_id: 'j1', conversation_id: 'c1', upstream_status: 200, upstream_body: {} })),
    resetConversation: vi.fn().mockReturnValue(of(state)),
  };
};

describe('MarketResearchDashboardComponent a11y', () => {
  it('has no axe violations in the dashboard shell', async () => {
    await TestBed.configureTestingModule({
      imports: [MarketResearchDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: MarketResearchApiService, useValue: { health: vi.fn().mockReturnValue(of({ status: 'ok' })) } },
        { provide: TeamAssistantApiService, useValue: teamAssistantMock() },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(MarketResearchDashboardComponent);
    fixture.detectChanges();

    // Guard: the shell title + embedded assistant rendered.
    expect(fixture.nativeElement.querySelector('app-dashboard-shell h1')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-team-assistant-chat')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
