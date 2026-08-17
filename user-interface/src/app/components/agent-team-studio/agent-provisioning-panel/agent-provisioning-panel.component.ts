import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatIconModule } from '@angular/material/icon';
import type { Subscription } from 'rxjs';
import { AgentProvisioningApiService } from '../../../services/agent-provisioning-api.service';
import { TeamAssistantChatComponent } from '../../team-assistant-chat/team-assistant-chat.component';
import { pollWhile } from '../../../shared/poll-while';
import type { ProvisionStatusResponse } from '../../../models';

/** Poll interval for job status after a chat-launched provisioning run. */
const JOB_POLL_INTERVAL_MS = 20000;

/**
 * The single source of truth for the "provision an agent via chat" surface:
 * the `app-team-assistant-chat` configuration for the Agent Provisioning
 * team, plus the job-status polling that starts once the chat launches a
 * job. Route-agnostic and free of `DashboardShellComponent` chrome, so it
 * can be mounted directly wherever the provisioning affordance is needed —
 * today that's both `AgentProvisioningDashboardComponent`'s "Provision" tab
 * and `AgentStudioBuildAgentComponent`'s Stage-1 slide-out (spec §4.1's
 * Stage 1 adaptation caveat). Keeping this logic in one component is what
 * keeps the chat contract (endpoint, labels, fields) and its polling
 * behavior from drifting between those two call sites.
 */
@Component({
  selector: 'app-agent-provisioning-panel',
  standalone: true,
  imports: [MatIconModule, TeamAssistantChatComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-provisioning-panel.component.html',
  styleUrl: './agent-provisioning-panel.component.scss',
})
export class AgentProvisioningPanelComponent {
  private readonly api = inject(AgentProvisioningApiService);
  private readonly destroyRef = inject(DestroyRef);

  private pollSub: Subscription | null = null;

  /** Job id from the most recent assistant-launched provisioning run; null until launched. */
  readonly jobId = signal<string | null>(null);
  /** Latest polled status for that job; null until the first poll resolves. */
  readonly jobStatus = signal<ProvisionStatusResponse | null>(null);

  /** aria-live announcement text; empty until a job is launched. */
  readonly statusAnnouncement = computed(() => {
    const id = this.jobId();
    if (!id) return '';
    const status = this.jobStatus();
    return status ? `Provisioning job ${id}: ${status.status}` : `Provisioning job ${id}: starting…`;
  });

  /**
   * Handle a launch triggered from the assistant chat. Same event contract
   * `AgentProvisioningDashboardComponent`'s prior inline handler used
   * (`{ job_id: string | null; conversation_id: string }`); `job_id` is
   * `null` for synchronous teams, which provisioning is not, but the guard
   * is kept for parity/defensiveness.
   */
  onAssistantLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (!event.job_id) return;
    const jobId = event.job_id;

    this.pollSub?.unsubscribe();
    this.jobId.set(jobId);
    this.jobStatus.set(null);

    this.pollSub = pollWhile(
      () => this.api.getJobStatus(jobId),
      (status) => status.status !== 'pending' && status.status !== 'running',
      { intervalMs: JOB_POLL_INTERVAL_MS },
    )
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((status) => this.jobStatus.set(status));
  }
}
