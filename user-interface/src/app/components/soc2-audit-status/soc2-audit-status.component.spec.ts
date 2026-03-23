import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { Soc2ComplianceApiService } from '../../services/soc2-compliance-api.service';
import { Soc2AuditStatusComponent } from './soc2-audit-status.component';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

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
  });

  it('should create', () => {
    component.jobId = 'j1';
    fixture.detectChanges();
    expect(component).toBeTruthy();
  });

  it('should poll and set status when jobId set', () => {
    component.jobId = 'j1';
    fixture.detectChanges();
    expect(component.status).toBeTruthy();
    expect(component.status?.job_id).toBe('j1');
  });
});
