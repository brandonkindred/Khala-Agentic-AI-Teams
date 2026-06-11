import { Component, Input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';

import {
  ActivityTimestamps,
  isStalled,
  lastActivityDurationLabel,
  lastActivityLabel,
} from '../staleness.util';

/**
 * Shared "Last activity: Ns ago" line + stalled-job warning banner.
 *
 * One component for every job view so the warning copy, suppression rules, and
 * styling cannot drift between copies (the two original inline versions had
 * already diverged in SCSS within a single PR).
 */
@Component({
  selector: 'app-stall-warning',
  standalone: true,
  imports: [MatIconModule],
  templateUrl: './stall-warning.component.html',
  styleUrl: './stall-warning.component.scss',
})
export class StallWarningComponent {
  /** The job status the staleness helpers read; null/undefined renders nothing. */
  @Input() status: ActivityTimestamps | null | undefined;

  /** Last-activity line shown only for active jobs — a terminal job's age is history, not health. */
  showLastActivity(): boolean {
    const s = this.status?.status;
    return (s === 'running' || s === 'pending') && this.lastActivityLabel() !== '';
  }

  lastActivityLabel(): string {
    return lastActivityLabel(this.status);
  }

  /** Suffix-free duration for the banner sentence ("No agent activity for 12m"). */
  stalledDurationLabel(): string {
    return lastActivityDurationLabel(this.status);
  }

  isStalled(): boolean {
    return isStalled(this.status);
  }
}
