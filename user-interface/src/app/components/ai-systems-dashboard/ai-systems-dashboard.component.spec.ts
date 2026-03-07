import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { AISystemsApiService } from '../../services/ai-systems-api.service';
import { AISystemsDashboardComponent } from './ai-systems-dashboard.component';

describe('AISystemsDashboardComponent', () => {
  let component: AISystemsDashboardComponent;
  let fixture: ComponentFixture<AISystemsDashboardComponent>;
  let apiSpy: {
    healthCheck: ReturnType<typeof vi.fn>;
    startBuild: ReturnType<typeof vi.fn>;
    listJobs: ReturnType<typeof vi.fn>;
    listBlueprints: ReturnType<typeof vi.fn>;
  };

  beforeEach(async () => {
    apiSpy = {
      healthCheck: vi.fn().mockReturnValue(of({ status: 'ok' })),
      startBuild: vi.fn().mockReturnValue(of({ job_id: 'j1' })),
      listJobs: vi.fn().mockReturnValue(of({ jobs: [] })),
      listBlueprints: vi.fn().mockReturnValue(of({ blueprints: [] })),
    };
    await TestBed.configureTestingModule({
      imports: [AISystemsDashboardComponent, NoopAnimationsModule],
      providers: [{ provide: AISystemsApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(AISystemsDashboardComponent);
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

  it('onSubmitBuild should call startBuild when form valid', () => {
    component.buildForm.patchValue({ project_name: 'proj1', spec_path: '/spec.yaml', output_dir: '' });
    component.onSubmitBuild();
    expect(apiSpy.startBuild).toHaveBeenCalled();
    expect(component.currentJobId).toBe('j1');
  });
});
