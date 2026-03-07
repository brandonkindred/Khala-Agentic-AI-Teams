import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PlanningV2RunFormComponent } from './planning-v2-run-form.component';

describe('PlanningV2RunFormComponent', () => {
  let component: PlanningV2RunFormComponent;
  let fixture: ComponentFixture<PlanningV2RunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PlanningV2RunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PlanningV2RunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('canSubmit should be false when specContent or repoPath empty', () => {
    expect(component.canSubmit).toBe(false);
    component.specContent = 'spec';
    expect(component.canSubmit).toBe(false);
    component.repoPath = '/repo';
    expect(component.canSubmit).toBe(true);
  });

  it('onSubmit should emit submitRequest when canSubmit', () => {
    component.specContent = 'spec';
    component.repoPath = '/repo';
    component.inspirationContent = 'insp';
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted).toEqual({ spec_content: 'spec', repo_path: '/repo', inspiration_content: 'insp' });
  });

  it('onSubmit should not emit when canSubmit is false', () => {
    let emitted = false;
    component.submitRequest.subscribe(() => (emitted = true));
    component.onSubmit();
    expect(emitted).toBe(false);
  });
});
