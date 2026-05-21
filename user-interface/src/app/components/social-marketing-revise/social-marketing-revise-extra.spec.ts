import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach } from 'vitest';
import { SocialMarketingReviseComponent } from './social-marketing-revise.component';
import type { ReviseMarketingTeamRequest } from '../../models';

describe('SocialMarketingReviseComponent (extra coverage)', () => {
  let component: SocialMarketingReviseComponent;
  let fixture: ComponentFixture<SocialMarketingReviseComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SocialMarketingReviseComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(SocialMarketingReviseComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('onSubmit emits when form valid and jobId set', () => {
    let emitted: ReviseMarketingTeamRequest | undefined;
    component.submitRequest.subscribe((v) => { emitted = v; });
    component.jobId = 'j1';
    component.form.setValue({ feedback: 'good work', approved_for_testing: true });
    component.onSubmit();
    expect(emitted?.feedback).toBe('good work');
    expect(emitted?.approved_for_testing).toBe(true);
  });

  it('onSubmit no-ops when form invalid', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.jobId = 'j1';
    component.form.setValue({ feedback: '', approved_for_testing: false });
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });

  it('onSubmit no-ops without jobId', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.jobId = null;
    component.form.setValue({ feedback: 'feedback', approved_for_testing: true });
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });
});
