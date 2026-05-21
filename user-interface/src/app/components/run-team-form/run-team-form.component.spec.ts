import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { RunTeamFormComponent } from './run-team-form.component';

describe('RunTeamFormComponent', () => {
  let component: RunTeamFormComponent;
  let fixture: ComponentFixture<RunTeamFormComponent>;
  let apiSpy: { runTeamFromUpload: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = { runTeamFromUpload: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running', message: '' })) };
    await TestBed.configureTestingModule({
      imports: [RunTeamFormComponent, NoopAnimationsModule],
      providers: [{ provide: SoftwareEngineeringApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(RunTeamFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('form should be invalid when project_name is empty', () => {
    expect(component.form.valid).toBe(false);
    expect(component.form.get('project_name')?.errors?.['required']).toBeTruthy();
  });

  it('onSubmit should not emit when form is invalid', () => {
    let emitted = false;
    component.submitRequest.subscribe(() => (emitted = true));
    component.form.patchValue({ project_name: '' });
    component.onSubmit();
    expect(emitted).toBe(false);
  });

  it('onSubmit should call api.runTeamFromUpload when form valid and file selected', () => {
    component.form.patchValue({ project_name: 'my-project' });
    component.selectedFile = new File([''], 'spec.zip');
    component.onSubmit();
    expect(apiSpy.runTeamFromUpload).toHaveBeenCalledWith('my-project', expect.any(File));
  });

  it('onFileSelected updates state from input change', () => {
    const file = new File(['x'], 'spec.zip');
    const evt = { target: { files: [file] } } as unknown as Event;
    component.onFileSelected(evt);
    expect(component.selectedFile).toBe(file);
    expect(component.selectedFileName()).toBe('spec.zip');
  });

  it('onFileSelected handles empty files array', () => {
    const evt = { target: { files: [] } } as unknown as Event;
    component.onFileSelected(evt);
    expect(component.selectedFile).toBeNull();
    expect(component.selectedFileName()).toBe('');
  });

  it('canSubmit requires file', () => {
    component.form.patchValue({ project_name: 'p' });
    expect(component.canSubmit).toBe(false);
    component.selectedFile = new File([''], 'x.zip');
    expect(component.canSubmit).toBe(true);
  });

  it('onSubmit error path with detail string', () => {
    apiSpy.runTeamFromUpload.mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    component.form.patchValue({ project_name: 'p' });
    component.selectedFile = new File([''], 'spec.zip');
    component.onSubmit();
    expect(component.uploadError()).toBe('boom');
    expect(component.isSubmitting()).toBe(false);
  });

  it('onSubmit error path with object detail', () => {
    apiSpy.runTeamFromUpload.mockReturnValue(throwError(() => ({ error: { detail: { a: 1 } } })));
    component.form.patchValue({ project_name: 'p' });
    component.selectedFile = new File([''], 'spec.zip');
    component.onSubmit();
    expect(component.uploadError()).toContain('a');
  });

  it('onSubmit success emits and resets isSubmitting', () => {
    const spy = vi.fn();
    component.submitRequest.subscribe(spy);
    component.form.patchValue({ project_name: 'p' });
    component.selectedFile = new File([''], 'spec.zip');
    component.onSubmit();
    expect(spy).toHaveBeenCalled();
    expect(component.isSubmitting()).toBe(false);
  });
});
