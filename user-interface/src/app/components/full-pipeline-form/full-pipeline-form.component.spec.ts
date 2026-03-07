import { ComponentFixture, TestBed } from '@angular/core/testing';
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
});
