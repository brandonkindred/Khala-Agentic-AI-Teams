import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentChatComponent } from './investment-chat.component';
import { expectNoAxeViolations } from '../../testing/a11y';

describe('InvestmentChatComponent a11y', () => {
  const setup = async () => {
    const api = {
      startAdvisorSession: vi.fn().mockReturnValue(of({ session_id: 's1', session_status: 'active' })),
      sendAdvisorMessage: vi
        .fn()
        .mockReturnValue(
          of({ session_status: 'active', advisor_message: 'hi', current_topic: 'profile', missing_fields: [] }),
        ),
      completeAdvisorSession: vi.fn().mockReturnValue(of({ message: 'done', ips: { profile: {} } })),
    };
    await TestBed.configureTestingModule({
      imports: [InvestmentChatComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: api }],
    }).compileComponents();
  };

  it('has no axe violations in the advisor chat', async () => {
    await setup();

    const fixture = TestBed.createComponent(InvestmentChatComponent);
    fixture.detectChanges();

    // Guard: the greeting message rendered, so axe audits a populated thread.
    expect(fixture.nativeElement.querySelector('.chat-container')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.message')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement);
  });
});
