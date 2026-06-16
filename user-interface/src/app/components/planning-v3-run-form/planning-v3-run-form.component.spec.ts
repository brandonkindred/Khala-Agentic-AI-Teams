import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PlanningV3RunFormComponent } from './planning-v3-run-form.component';

describe('PlanningV3RunFormComponent', () => {
  let component: PlanningV3RunFormComponent;
  let fixture: ComponentFixture<PlanningV3RunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PlanningV3RunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PlanningV3RunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('canSubmit requires initialBrief, not repoPath', () => {
    expect(component.canSubmit).toBe(false);
    component.repoPath = '/some/path';
    expect(component.canSubmit).toBe(false);
    component.initialBrief = 'Build a home maintenance tracker';
    expect(component.canSubmit).toBe(true);
  });

  it('onSubmit omits repo_path when blank', () => {
    component.initialBrief = 'Greenfield app';
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.repo_path).toBeUndefined();
    expect(emitted.initial_brief).toBe('Greenfield app');
    expect(emitted.use_product_analysis).toBe(true);
  });

  it('onSubmit forwards a provided output folder', () => {
    component.initialBrief = 'App';
    component.repoPath = '  /out/dir  ';
    component.clientName = 'Acme';
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.repo_path).toBe('/out/dir');
    expect(emitted.client_name).toBe('Acme');
  });

  it('onSubmit does not emit when canSubmit is false', () => {
    let emitted = false;
    component.submitRequest.subscribe(() => (emitted = true));
    component.onSubmit();
    expect(emitted).toBe(false);
  });

  it('onSubmit does not emit while a submission is in flight', () => {
    component.initialBrief = 'App';
    component.submitting = true;
    let count = 0;
    component.submitRequest.subscribe(() => count++);
    component.onSubmit();
    expect(count).toBe(0);
  });

  it('submit button is disabled while submitting', () => {
    component.initialBrief = 'App';
    component.submitting = true;
    fixture.detectChanges();
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('button');
    expect(button.disabled).toBe(true);
  });
});
