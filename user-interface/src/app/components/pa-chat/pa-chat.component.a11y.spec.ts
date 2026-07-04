import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { expectNoAxeViolations } from '../../testing/a11y';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import { PaChatComponent } from './pa-chat.component';

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

    await expectNoAxeViolations(fixture.nativeElement);
  });
});
