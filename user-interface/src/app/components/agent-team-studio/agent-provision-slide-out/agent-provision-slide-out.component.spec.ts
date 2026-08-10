import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentProvisioningApiService } from '../../../services/agent-provisioning-api.service';
import { TeamAssistantApiService } from '../../../services/team-assistant-api.service';
import { createTeamAssistantApiMock } from '../../../testing/team-assistant.mock';
import { TeamAssistantChatComponent } from '../../team-assistant-chat/team-assistant-chat.component';
import { AgentProvisionSlideOutComponent } from './agent-provision-slide-out.component';

describe('AgentProvisionSlideOutComponent', () => {
  let fixture: ComponentFixture<AgentProvisionSlideOutComponent>;
  let component: AgentProvisionSlideOutComponent;
  let apiSpy: { getJobStatus: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    vi.useFakeTimers();
    apiSpy = { getJobStatus: vi.fn() };

    await TestBed.configureTestingModule({
      imports: [AgentProvisionSlideOutComponent, NoopAnimationsModule],
      providers: [
        { provide: AgentProvisioningApiService, useValue: apiSpy },
        { provide: TeamAssistantApiService, useValue: createTeamAssistantApiMock() },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(AgentProvisionSlideOutComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  it('renders the assistant chat with the provisioning config and no status section before launch', () => {
    const chat = fixture.debugElement.query(By.directive(TeamAssistantChatComponent));
    expect(chat).toBeTruthy();
    const chatInstance = chat.componentInstance as TeamAssistantChatComponent;
    expect(chatInstance.teamApiUrl).toBe('/api/agent-provisioning/assistant');
    expect(chatInstance.teamName).toBe('Agent Provisioning');
    expect(chatInstance.fields.map((f) => f.key)).toEqual(['agent_id', 'manifest_path']);
    expect(chatInstance.fields[0].required).toBe(true);

    expect(fixture.nativeElement.querySelector('.provision-slide-out__status')).toBeNull();
    const liveRegion = fixture.nativeElement.querySelector('[role="status"]');
    expect(liveRegion.textContent.trim()).toBe('');
  });

  it('does nothing when the launch event carries no job_id (sync-team contract)', () => {
    component.onAssistantLaunched({ job_id: null, conversation_id: 'c1' });
    expect(component.jobId()).toBeNull();
    expect(apiSpy.getJobStatus).not.toHaveBeenCalled();
  });

  it('polls job status on launch and renders it, stopping once terminal', () => {
    apiSpy.getJobStatus
      .mockReturnValueOnce(
        of({ job_id: 'j1', status: 'running', progress: 10, tools_completed: 0, tools_total: 3, completed_phases: [] }),
      )
      .mockReturnValueOnce(
        of({ job_id: 'j1', status: 'completed', progress: 100, tools_completed: 3, tools_total: 3, completed_phases: [] }),
      );

    component.onAssistantLaunched({ job_id: 'j1', conversation_id: 'c1' });
    vi.advanceTimersByTime(0);
    fixture.detectChanges();
    expect(apiSpy.getJobStatus).toHaveBeenCalledTimes(1);
    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('j1');
    expect(component.jobStatus()?.status).toBe('running');
    expect(fixture.nativeElement.querySelector('.provision-slide-out__status-grid dd').textContent).toContain('j1');

    vi.advanceTimersByTime(20000);
    fixture.detectChanges();
    expect(apiSpy.getJobStatus).toHaveBeenCalledTimes(2);
    expect(component.jobStatus()?.status).toBe('completed');

    vi.advanceTimersByTime(20000);
    expect(apiSpy.getJobStatus).toHaveBeenCalledTimes(2);
  });

  it('re-launching replaces the previous poll instead of running both', () => {
    apiSpy.getJobStatus.mockReturnValue(
      of({ job_id: 'j1', status: 'running', progress: 10, tools_completed: 0, tools_total: 3, completed_phases: [] }),
    );
    component.onAssistantLaunched({ job_id: 'j1', conversation_id: 'c1' });
    vi.advanceTimersByTime(0);
    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('j1');

    apiSpy.getJobStatus.mockReset();
    apiSpy.getJobStatus.mockReturnValue(
      of({ job_id: 'j2', status: 'running', progress: 5, tools_completed: 0, tools_total: 3, completed_phases: [] }),
    );
    component.onAssistantLaunched({ job_id: 'j2', conversation_id: 'c2' });
    vi.advanceTimersByTime(0);
    expect(component.jobId()).toBe('j2');
    expect(apiSpy.getJobStatus).toHaveBeenCalledWith('j2');
    expect(apiSpy.getJobStatus).not.toHaveBeenCalledWith('j1');

    const callsForJ1 = apiSpy.getJobStatus.mock.calls.filter((c: unknown[]) => c[0] === 'j1').length;
    vi.advanceTimersByTime(20000);
    expect(apiSpy.getJobStatus.mock.calls.filter((c: unknown[]) => c[0] === 'j1').length).toBe(callsForJ1);
  });

  it('renders the job error message when present', () => {
    apiSpy.getJobStatus.mockReturnValue(
      of({
        job_id: 'j1',
        status: 'failed',
        progress: 0,
        tools_completed: 0,
        tools_total: 3,
        completed_phases: [],
        error: 'boom',
      }),
    );
    component.onAssistantLaunched({ job_id: 'j1', conversation_id: 'c1' });
    vi.advanceTimersByTime(0);
    fixture.detectChanges();
    const errorEl = fixture.nativeElement.querySelector('.provision-slide-out__status-error');
    expect(errorEl.textContent).toContain('boom');
  });

  it('updates the aria-live announcement text as status changes', () => {
    apiSpy.getJobStatus
      .mockReturnValueOnce(
        of({ job_id: 'j1', status: 'running', progress: 10, tools_completed: 0, tools_total: 3, completed_phases: [] }),
      )
      .mockReturnValueOnce(
        of({ job_id: 'j1', status: 'completed', progress: 100, tools_completed: 3, tools_total: 3, completed_phases: [] }),
      );

    component.onAssistantLaunched({ job_id: 'j1', conversation_id: 'c1' });
    vi.advanceTimersByTime(0);
    expect(component.statusAnnouncement()).toBe('Provisioning job j1: running');

    vi.advanceTimersByTime(20000);
    expect(component.statusAnnouncement()).toBe('Provisioning job j1: completed');
  });

  it('stops polling once the component is destroyed', () => {
    apiSpy.getJobStatus.mockReturnValue(
      of({ job_id: 'j1', status: 'running', progress: 10, tools_completed: 0, tools_total: 3, completed_phases: [] }),
    );
    component.onAssistantLaunched({ job_id: 'j1', conversation_id: 'c1' });
    vi.advanceTimersByTime(0);
    expect(apiSpy.getJobStatus).toHaveBeenCalledTimes(1);

    fixture.destroy();
    vi.advanceTimersByTime(60000);
    expect(apiSpy.getJobStatus).toHaveBeenCalledTimes(1);
  });
});
