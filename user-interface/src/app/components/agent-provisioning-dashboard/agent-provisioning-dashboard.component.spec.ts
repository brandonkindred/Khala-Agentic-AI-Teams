import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { AgentProvisioningApiService } from '../../services/agent-provisioning-api.service';
import { AgentProvisioningDashboardComponent } from './agent-provisioning-dashboard.component';

describe('AgentProvisioningDashboardComponent', () => {
  let component: AgentProvisioningDashboardComponent;
  let fixture: ComponentFixture<AgentProvisioningDashboardComponent>;
  let apiSpy: {
    healthCheck: ReturnType<typeof vi.fn>;
    startProvisioning: ReturnType<typeof vi.fn>;
    listJobs: ReturnType<typeof vi.fn>;
    listAgents: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      startProvisioning: vi.fn().mockReturnValue(of({ job_id: 'j1' })),
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      listAgents: vi.fn().mockReturnValue(of({ agents: [] })),
    };
    await TestBed.configureTestingModule({
      imports: [AgentProvisioningDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: AgentProvisioningApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AgentProvisioningDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('healthCheck should call api.healthCheck', () => {
    component.healthCheck().subscribe((r) => expect(r).toEqual({ status: 'ok' }));
    expect(apiSpy.healthCheck).toHaveBeenCalled();
  });

  it('onTabChange should set activeTab', () => {
    component.onTabChange(1);
    expect(component.selectedTabIndex).toBe(1);
    expect(component.activeTab).toBe('jobs');
  });

  it('onSubmitProvision should call startProvisioning when form valid', () => {
    component.provisionForm.patchValue({ agent_id: 'agent1', manifest_path: 'default.yaml', access_tier: 'standard' });
    component.onSubmitProvision();
    expect(apiSpy.startProvisioning).toHaveBeenCalled();
    expect(component.currentJobId).toBe('j1');
  });
});
