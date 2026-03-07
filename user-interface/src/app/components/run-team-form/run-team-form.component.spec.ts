import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RunTeamFormComponent } from './run-team-form.component';

describe('RunTeamFormComponent', () => {
  let component: RunTeamFormComponent;
  let fixture: ComponentFixture<RunTeamFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [RunTeamFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(RunTeamFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('form should be invalid when repo_path is empty', () => {
    expect(component.form.valid).toBe(false);
    expect(component.form.get('repo_path')?.errors?.['required']).toBeTruthy();
  });

  it('onSubmit should emit submitRequest when form is valid', () => {
    let emitted: { repo_path: string } | undefined;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.form.patchValue({ repo_path: '/tmp/repo' });
    component.onSubmit();
    expect(emitted).toEqual({ repo_path: '/tmp/repo' });
  });

  it('onSubmit should not emit when form is invalid', () => {
    let emitted = false;
    component.submitRequest.subscribe(() => (emitted = true));
    component.form.patchValue({ repo_path: '' });
    component.onSubmit();
    expect(emitted).toBe(false);
  });
});
