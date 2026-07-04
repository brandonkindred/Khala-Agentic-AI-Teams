import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaChatComponent } from './pa-chat.component';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('PaChatComponent a11y', () => {
  const setup = async () => {
    const api = {
      sendMessage: vi.fn().mockReturnValue(of({ response: 'hi there', timestamp: '2025-01-01T00:00:00Z' })),
    };
    await TestBed.configureTestingModule({
      imports: [PaChatComponent, NoopAnimationsModule],
      providers: [{ provide: PersonalAssistantApiService, useValue: api }],
    }).compileComponents();
  };

  it('has no axe violations in the chat surface', async () => {
    await setup();

    const fixture = TestBed.createComponent(PaChatComponent);
    fixture.componentInstance.userId = 'u1';
    fixture.detectChanges();

    // Guard: the greeting message rendered, so axe audits a populated thread.
    expect(fixture.nativeElement.querySelector('.chat-card')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.message.assistant')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
