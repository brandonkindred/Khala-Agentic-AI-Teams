import { ComponentFixture, TestBed } from '@angular/core/testing';

import { STALL_THRESHOLD_MS } from '../staleness.util';
import { StallWarningComponent } from './stall-warning.component';

describe('StallWarningComponent', () => {
  let fixture: ComponentFixture<StallWarningComponent>;
  let component: StallWarningComponent;

  const at = (msAgo: number): string => new Date(Date.now() - msAgo).toISOString();

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [StallWarningComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(StallWarningComponent);
    component = fixture.componentInstance;
  });

  it('renders nothing for a null status', () => {
    component.status = null;
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent.trim()).toBe('');
  });

  it('shows the last-activity line for an active job', () => {
    component.status = { status: 'running', last_activity_at: at(42_000) };
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Last activity: 42s ago');
    expect(fixture.nativeElement.querySelector('.stalled-warning')).toBeNull();
  });

  it('hides the last-activity line on terminal jobs (history, not health)', () => {
    component.status = { status: 'completed', last_activity_at: at(42_000) };
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.last-activity-section')).toBeNull();
  });

  it('renders a grammatical banner with a suffix-free duration when stalled', () => {
    component.status = { status: 'running', last_activity_at: at(STALL_THRESHOLD_MS + 2 * 60_000) };
    fixture.detectChanges();
    const banner = fixture.nativeElement.querySelector('.stalled-warning');
    expect(banner).not.toBeNull();
    expect(banner.textContent).toContain('No agent activity for 12m — the job may be stalled.');
    // The double-relative bug this component fixed: "for 12m ago" must not regress.
    expect(banner.textContent).not.toContain('ago —');
  });

  it('suppresses the banner while waiting for answers', () => {
    component.status = {
      status: 'running',
      waiting_for_answers: true,
      last_activity_at: at(STALL_THRESHOLD_MS + 60_000),
    };
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.stalled-warning')).toBeNull();
  });
});
