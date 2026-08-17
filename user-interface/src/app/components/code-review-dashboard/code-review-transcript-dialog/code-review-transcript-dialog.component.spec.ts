import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { CodeReviewTranscriptDialogComponent } from './code-review-transcript-dialog.component';
import { IntegrationsApiService } from '../../../services/integrations-api.service';
import type { CodeReviewTranscript } from '../../../models/integrations.model';

describe('CodeReviewTranscriptDialogComponent', () => {
  let api: { getGitHubReviewTranscript: ReturnType<typeof vi.fn> };
  let dialogRef: { close: ReturnType<typeof vi.fn> };

  const transcript: CodeReviewTranscript = {
    job_id: 'j1',
    entries: [
      {
        stage: 'chunk_review',
        target: 'a.py',
        model: 'claude-x',
        prompt: 'review this chunk',
        response: '{"approved": true}',
        started_at: '2024-01-01T00:00:00Z',
        duration_ms: 1200,
      },
      {
        stage: 'synthesis',
        target: '',
        model: 'claude-x',
        prompt: 'synthesize',
        response: 'summary text',
        started_at: '2024-01-01T00:00:02Z',
        duration_ms: 300,
      },
    ],
  };

  function build() {
    TestBed.configureTestingModule({
      imports: [CodeReviewTranscriptDialogComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        { provide: IntegrationsApiService, useValue: api },
        { provide: MatDialogRef, useValue: dialogRef },
        { provide: MAT_DIALOG_DATA, useValue: { owner: 'acme', repo: 'widget', jobId: 'j1' } },
      ],
    });
    return TestBed.createComponent(CodeReviewTranscriptDialogComponent);
  }

  beforeEach(() => {
    api = { getGitHubReviewTranscript: vi.fn().mockReturnValue(of(transcript)) };
    dialogRef = { close: vi.fn() };
  });

  it('fetches the transcript for the dialog data on open', () => {
    const fixture = build();
    fixture.detectChanges();
    expect(api.getGitHubReviewTranscript).toHaveBeenCalledWith('acme', 'widget', 'j1');
    expect(fixture.componentInstance.entries()).toEqual(transcript.entries);
    expect(fixture.componentInstance.loading()).toBe(false);
    expect(fixture.componentInstance.error()).toBeNull();
  });

  it('sets an error message when the fetch fails', () => {
    api.getGitHubReviewTranscript.mockReturnValue(
      throwError(() => ({ error: { detail: 'no transcript recorded for job j1' } })),
    );
    const fixture = build();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('no transcript recorded for job j1');
    expect(fixture.componentInstance.loading()).toBe(false);
    expect(fixture.componentInstance.entries()).toEqual([]);
  });

  it('closes the dialog via the ref', () => {
    const fixture = build();
    fixture.detectChanges();
    fixture.componentInstance.close();
    expect(dialogRef.close).toHaveBeenCalled();
  });

  it('renders each entry (stage, target, prompt, response)', () => {
    const fixture = build();
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('chunk_review');
    expect(text).toContain('a.py');
    expect(text).toContain('review this chunk');
    expect(text).toContain('summary text');
  });

  it('renders an empty-state message when no entries were recorded', () => {
    api.getGitHubReviewTranscript.mockReturnValue(of({ job_id: 'j1', entries: [] }));
    const fixture = build();
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('no recordable LLM calls');
  });
});
