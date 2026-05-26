import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { FrontendCodeV2RunFormComponent } from './frontend-code-v2-run-form.component';

describe('FrontendCodeV2RunFormComponent', () => {
  let component: FrontendCodeV2RunFormComponent;
  let fixture: ComponentFixture<FrontendCodeV2RunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FrontendCodeV2RunFormComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(FrontendCodeV2RunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('canSubmit + addCriterion + removeCriterion', () => {
    expect(component.canSubmit).toBe(false);
    component.title = 'T';
    component.description = 'D';
    component.repoPath = '/repo';
    expect(component.canSubmit).toBe(true);
    component.criterionInput = 'AC';
    component.addCriterion();
    expect(component.acceptanceCriteria).toEqual(['AC']);
    component.criterionInput = '';
    component.addCriterion();
    expect(component.acceptanceCriteria).toEqual(['AC']);
    component.removeCriterion(0);
    expect(component.acceptanceCriteria).toEqual([]);
  });

  it('onSubmit emits payload', () => {
    component.title = 'T';
    component.description = 'D';
    component.repoPath = '/repo';
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy).toHaveBeenCalled();
  });

  it('onSubmit skipped when invalid', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });

  it('onSubmit emits with full extras', () => {
    component.title = 'T';
    component.description = 'D';
    component.repoPath = '/repo';
    component.requirements = 'req';
    component.specContent = 'spec';
    component.architecture = 'arch';
    component.acceptanceCriteria = ['AC1'];
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    const req = spy.mock.calls[0][0];
    expect(req.task.requirements).toBe('req');
    expect(req.spec_content).toBe('spec');
    expect(req.architecture).toBe('arch');
    expect(req.task.acceptance_criteria).toEqual(['AC1']);
  });
});
