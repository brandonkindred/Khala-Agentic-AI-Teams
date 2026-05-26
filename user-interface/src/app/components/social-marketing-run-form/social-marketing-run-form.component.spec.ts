import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SocialMarketingRunFormComponent } from './social-marketing-run-form.component';

describe('SocialMarketingRunFormComponent', () => {
  let component: SocialMarketingRunFormComponent;
  let fixture: ComponentFixture<SocialMarketingRunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SocialMarketingRunFormComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(SocialMarketingRunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('onSubmit skipped when invalid', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });

  it('onSubmit splits goals string into list', () => {
    component.form.patchValue({
      brand_guidelines_path: '/g',
      brand_objectives_path: '/o',
      llm_model_name: 'gpt-4',
      goals: 'a, b , c',
    });
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy).toHaveBeenCalled();
    expect(spy.mock.calls[0][0].goals).toEqual(['a', 'b', 'c']);
  });

  it('onSubmit defaults goals if empty after split', () => {
    component.form.patchValue({
      brand_guidelines_path: '/g',
      brand_objectives_path: '/o',
      llm_model_name: 'gpt-4',
      goals: ',',
    });
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy.mock.calls[0][0].goals).toEqual(['engagement', 'follower growth']);
  });

  it('onSubmit sets human_feedback fallback to empty string', () => {
    component.form.patchValue({
      brand_guidelines_path: '/g',
      brand_objectives_path: '/o',
      llm_model_name: 'gpt-4',
      goals: 'a',
      human_feedback: null,
    });
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy.mock.calls[0][0].human_feedback).toBe('');
  });
});
