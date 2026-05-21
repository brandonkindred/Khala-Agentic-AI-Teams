import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach } from 'vitest';
import { SocialMarketingPerformanceComponent } from './social-marketing-performance.component';
import type { PostPerformanceObservation } from '../../models';

describe('SocialMarketingPerformanceComponent (extra coverage)', () => {
  let component: SocialMarketingPerformanceComponent;
  let fixture: ComponentFixture<SocialMarketingPerformanceComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SocialMarketingPerformanceComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(SocialMarketingPerformanceComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('onSubmit emits parsed observations when valid', () => {
    let emitted: PostPerformanceObservation[] | undefined;
    component.submitObservations.subscribe((v) => { emitted = v; });
    component.jobId = 'j1';
    component.form.setValue({ observationsJson: '[{"post_id":"p1","engagement_score":0.8}]' });
    component.onSubmit();
    expect(emitted?.length).toBe(1);
    expect(emitted?.[0].post_id).toBe('p1');
  });

  it('onSubmit emits empty array when JSON is not array', () => {
    let emitted: PostPerformanceObservation[] | undefined;
    component.submitObservations.subscribe((v) => { emitted = v; });
    component.jobId = 'j1';
    component.form.setValue({ observationsJson: '{"not":"array"}' });
    component.onSubmit();
    expect(emitted).toEqual([]);
  });

  it('onSubmit silently ignores invalid JSON', () => {
    const spy = vi.fn();
    component.submitObservations.subscribe(spy);
    component.jobId = 'j1';
    component.form.setValue({ observationsJson: 'not-json' });
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });

  it('onSubmit no-ops without jobId', () => {
    const spy = vi.fn();
    component.submitObservations.subscribe(spy);
    component.jobId = null;
    component.form.setValue({ observationsJson: '[]' });
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });
});
