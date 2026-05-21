import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { BackendCodeV2RunFormComponent } from './backend-code-v2-run-form.component';

describe('BackendCodeV2RunFormComponent', () => {
  let component: BackendCodeV2RunFormComponent;
  let fixture: ComponentFixture<BackendCodeV2RunFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BackendCodeV2RunFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(BackendCodeV2RunFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('canSubmit false without title/description/repoPath', () => {
    expect(component.canSubmit).toBe(false);
    component.title = 'T';
    component.description = 'D';
    expect(component.canSubmit).toBe(false);
    component.repoPath = '/repo';
    expect(component.canSubmit).toBe(true);
  });

  it('addCriterion + removeCriterion', () => {
    component.criterionInput = '  AC1  ';
    component.addCriterion();
    expect(component.acceptanceCriteria).toEqual(['AC1']);
    expect(component.criterionInput).toBe('');
    component.criterionInput = '';
    component.addCriterion();
    expect(component.acceptanceCriteria).toEqual(['AC1']);
    component.removeCriterion(0);
    expect(component.acceptanceCriteria).toEqual([]);
  });

  it('onSubmit skipped when not valid', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    expect(spy).not.toHaveBeenCalled();
  });

  it('onSubmit emits payload with criteria + extras', () => {
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
    expect(spy).toHaveBeenCalled();
    const req = spy.mock.calls[0][0];
    expect(req.task.title).toBe('T');
    expect(req.task.requirements).toBe('req');
    expect(req.task.acceptance_criteria).toEqual(['AC1']);
    expect(req.repo_path).toBe('/repo');
    expect(req.spec_content).toBe('spec');
    expect(req.architecture).toBe('arch');
  });

  it('onSubmit omits empty optional fields', () => {
    component.title = 'T';
    component.description = 'D';
    component.repoPath = '/repo';
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.onSubmit();
    const req = spy.mock.calls[0][0];
    expect(req.task.requirements).toBeUndefined();
    expect(req.task.acceptance_criteria).toBeUndefined();
    expect(req.spec_content).toBeUndefined();
    expect(req.architecture).toBeUndefined();
  });
});
