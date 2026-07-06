import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PlanningRunFormComponent } from './planning-run-form.component';

describe('PlanningRunFormComponent', () => {
  let component: PlanningRunFormComponent;
  let fixture: ComponentFixture<PlanningRunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PlanningRunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PlanningRunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('canSubmit requires brief or spec, not repoPath', () => {
    expect(component.canSubmit).toBe(false);
    component.repoPath = '/some/path';
    expect(component.canSubmit).toBe(false);
    component.initialBrief = 'Build a home maintenance tracker';
    expect(component.canSubmit).toBe(true);
  });

  it('canSubmit is true with only specContent (no initial brief)', () => {
    expect(component.canSubmit).toBe(false);
    component.specContent = '# Full spec';
    expect(component.canSubmit).toBe(true);
  });

  it('onSubmit emits a spec-only request when no brief is given', () => {
    component.specContent = '# Full spec';
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.initial_brief).toBeUndefined();
    expect(emitted.spec_content).toBe('# Full spec');
  });

  it('onSubmit omits repo_path when blank', () => {
    component.initialBrief = 'Greenfield app';
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.repo_path).toBeUndefined();
    expect(emitted.client_name).toBeUndefined();
    expect(emitted.initial_brief).toBe('Greenfield app');
    expect(emitted.use_product_analysis).toBe(true);
  });

  it('onSubmit emits the toggle fields with their set values', () => {
    component.initialBrief = 'App';
    component.useProductAnalysis = false;
    component.useMarketResearch = true;
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted.use_product_analysis).toBe(false);
    expect(emitted.use_market_research).toBe(true);
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
