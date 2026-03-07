import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { Soc2AuditStatusComponent } from './soc2-audit-status.component';

describe('Soc2AuditStatusComponent', () => {
  let component: Soc2AuditStatusComponent;
  let fixture: ComponentFixture<Soc2AuditStatusComponent>;

  beforeEach(async () => {
    const apiSpy = { getStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'completed' })) };
    await TestBed.configureTestingModule({
      imports: [Soc2AuditStatusComponent],
      providers: [{ provide: Soc2ComplianceApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(Soc2AuditStatusComponent);
    component = fixture.componentInstance;
    component.jobId = 'j1';
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should poll and set status when jobId set', () => {
    expect(component.status).toBeTruthy();
    expect(component.status?.job_id).toBe('j1');
  });
});
