import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { BrandingApiService } from '../../services/branding-api.service';
import { BrandingChatComponent } from './branding-chat.component';
import type { ConversationStateResponse } from '../../models';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('BrandingChatComponent a11y', () => {
  const mockResponse: ConversationStateResponse = {
    conversation_id: 'conv-1',
    messages: [
      { role: 'assistant', content: 'Hi! What is your company name?', timestamp: new Date().toISOString() },
    ],
    mission: { company_name: 'TBD', company_description: 'To be discussed.', target_audience: 'TBD' },
    latest_output: null,
    suggested_questions: ['What is your company name?', 'Who is your audience?'],
  } as never;

  const setup = async () => {
    const api = {
      createConversation: vi.fn().mockReturnValue(of(mockResponse)),
      getConversation: vi.fn().mockReturnValue(of(mockResponse)),
      sendConversationMessage: vi.fn().mockReturnValue(of(mockResponse)),
    };
    await TestBed.configureTestingModule({
      imports: [BrandingChatComponent, NoopAnimationsModule],
      providers: [{ provide: BrandingApiService, useValue: api }],
    }).compileComponents();
  };

  it('has no axe violations in the branding chat', async () => {
    await setup();

    const fixture = TestBed.createComponent(BrandingChatComponent);
    fixture.detectChanges();

    // Guard: the bootstrapped conversation + suggested questions rendered.
    expect(fixture.nativeElement.querySelector('.chat-card')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.suggested-questions')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
