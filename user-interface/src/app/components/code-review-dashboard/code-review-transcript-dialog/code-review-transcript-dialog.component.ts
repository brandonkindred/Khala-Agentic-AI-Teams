import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatDialogModule, MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { IntegrationsApiService } from '../../../services/integrations-api.service';
import { InlineBannerComponent } from '../../../shared/inline-banner/inline-banner.component';
import type { CodeReviewTranscriptEntry } from '../../../models/integrations.model';
import { extractErrorDetail } from '../../../shared/extract-error-detail';

/** Data handed to the dialog: identifies the review whose transcript to load. */
export interface CodeReviewTranscriptDialogData {
  owner: string;
  repo: string;
  jobId: string;
}

/**
 * Read-only view of one code review's durable transcript: renders whatever
 * entries the transcript endpoint returns, in call order, with the full
 * prompt and response text for each — the reviewer's "thinking process" for
 * that run. Which pipeline stages appear depends entirely on what the backend
 * recorded (e.g. only the in-process coordinator path records a transcript
 * today); this component makes no assumption about which stages ran. Fetched
 * once on open; there is no live/streaming view, matching a completed
 * review's transcript being immutable.
 */
@Component({
  selector: 'app-code-review-transcript-dialog',
  standalone: true,
  imports: [CommonModule, MatDialogModule, MatButtonModule, MatIconModule, MatProgressSpinnerModule, InlineBannerComponent],
  templateUrl: './code-review-transcript-dialog.component.html',
  styleUrl: './code-review-transcript-dialog.component.scss',
})
export class CodeReviewTranscriptDialogComponent implements OnInit {
  readonly data = inject<CodeReviewTranscriptDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<CodeReviewTranscriptDialogComponent>>(MatDialogRef);
  private readonly api = inject(IntegrationsApiService);

  readonly entries = signal<CodeReviewTranscriptEntry[]>([]);
  readonly loading = signal<boolean>(true);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.load();
  }

  private load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.getGitHubReviewTranscript(this.data.owner, this.data.repo, this.data.jobId).subscribe({
      next: (transcript) => {
        this.entries.set(transcript.entries);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(extractErrorDetail(err, 'Failed to load transcript'));
        this.loading.set(false);
      },
    });
  }

  close(): void {
    this.ref.close();
  }
}
