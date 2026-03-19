import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FullPipelineFormComponent } from './full-pipeline-form.component';

describe('FullPipelineFormComponent', () => {
  let component: FullPipelineFormComponent;
  let fixture: ComponentFixture<FullPipelineFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FullPipelineFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(FullPipelineFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onSubmit should emit when form valid', () => {
    component.form.patchValue({ brief: 'Test brief', max_results: 20 });
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted).toBeDefined();
    expect(emitted.brief).toBe('Test brief');
  });

  it('hides series fields unless writing format is Series instalment', () => {
    fixture.detectChanges();
    expect(fixture.debugElement.query(By.css('[formControlName="series_title"]'))).toBeNull();

    component.form.patchValue({ content_profile: 'series_instalment' });
    fixture.detectChanges();
    expect(fixture.debugElement.query(By.css('[formControlName="series_title"]'))).not.toBeNull();
  });

  it('does not emit series_context for non-series profiles even if values linger', () => {
    component.form.patchValue({
      brief: 'Test brief',
      max_results: 20,
      content_profile: 'series_instalment',
      series_title: 'My Series',
      part_number: 2,
    });
    component.form.patchValue({ content_profile: 'standard_article' });
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.series_context).toBeUndefined();
  });

  it('hides audience and tone when brand spec is configured on the server', () => {
    fixture.componentRef.setInput('brandSpecConfigured', true);
    fixture.detectChanges();
    expect(fixture.debugElement.query(By.css('[formControlName="audience"]'))).toBeNull();
    expect(fixture.debugElement.query(By.css('.brand-spec-note'))).not.toBeNull();
  });

  it('omits audience and tone from payload when brand spec is configured', () => {
    fixture.componentRef.setInput('brandSpecConfigured', true);
    component.form.patchValue({
      brief: 'Test brief',
      max_results: 20,
      audience: 'CTOs',
      tone_or_purpose: 'technical',
    });
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.audience).toBeUndefined();
    expect(emitted.tone_or_purpose).toBeUndefined();
  });

  it('emits series_context for series_instalment when series fields are set', () => {
    component.form.patchValue({
      brief: 'Test brief',
      max_results: 20,
      content_profile: 'series_instalment',
      series_title: 'My Series',
      part_number: 2,
      instalment_scope: 'Topic A',
    });
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.series_context).toEqual({
      series_title: 'My Series',
      part_number: 2,
      instalment_scope: 'Topic A',
    });
  });
});
