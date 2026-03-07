import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { AccessibilityAuditFormComponent } from './accessibility-audit-form.component';

describe('AccessibilityAuditFormComponent', () => {
  let component: AccessibilityAuditFormComponent;
  let fixture: ComponentFixture<AccessibilityAuditFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AccessibilityAuditFormComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(AccessibilityAuditFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have auditType default', () => {
    expect(component.auditType).toBe('webpage');
  });
});
