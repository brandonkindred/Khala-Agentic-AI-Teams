import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatIconModule } from '@angular/material/icon';
import type { Subscription } from 'rxjs';
import { AgentProvisioningApiService } from '../../../services/agent-provisioning-api.service';
import { TeamAssistantChatComponent } from '../../team-assistant-chat/team-assistant-chat.component';
import { pollWhile } from '../../../shared/poll-while';
import type { ProvisionStatusResponse } from '../../../models';

/** Poll interval for job status while the slide-out is open — matches the
 *  interval `AgentProvisioningDashboardComponent.startJobPolling` used. */
const JOB_POLL_INTERVAL_MS = 20000;

/**
 * Thin, route-agnostic wrapper mounted inside `AgentStudioBuildAgentComponent`'s
 * Stage-1 "Provision an agent" slide-out. Renders the same `app-team-assistant-chat`
 * config `AgentProvisioningDashboardComponent` uses, plus a minimal job-status
 * readout scoped to the panel — without that component's `DashboardShellComponent`
 * chrome (page `<h1>`, subtitle, health indicator, sub-team nav) or its
 * `ActivatedRoute`-based `?jobId=` deep link, neither of which applies on the
 * unrelated `/agent-studio` route. The full dashboard stays mounted, unchanged,
 * inside `AgentConsoleComponent`'s own tab strip — this component does not
 * replace it there.
 */
@Component({
  selector: 'app-agent-provision-slide-out',
  standalone: true,
  imports: [MatIconModule, TeamAssistantChatComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './agent-provision-slide-out.component.html',
  styleUrl: './agent-provision-slide-out.component.scss',
})
export class AgentProvisionSlideOutComponent {
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
   * `AgentProvisioningDashboardComponent.onAssistantLaunched` used
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
