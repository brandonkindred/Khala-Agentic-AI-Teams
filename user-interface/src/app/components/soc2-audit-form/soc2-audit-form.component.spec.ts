import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Soc2AuditFormComponent } from './soc2-audit-form.component';

describe('Soc2AuditFormComponent', () => {
  let component: Soc2AuditFormComponent;
  let fixture: ComponentFixture<Soc2AuditFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Soc2AuditFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(Soc2AuditFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onSubmit should emit when form valid', () => {
    component.form.patchValue({ repo_path: '/tmp/repo' });
    let emitted: any;
    component.submitRequest.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted).toEqual({ repo_path: '/tmp/repo' });
  });
});
