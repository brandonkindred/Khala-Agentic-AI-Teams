import { Component, inject, OnInit, OnDestroy } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { marked } from 'marked';
import { BloggingApiService } from '../../services/blogging-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { artifactLabel } from '../blogging-dashboard/blogging-dashboard.component';

@Component({
  selector: 'app-blog-artifact-viewer',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatButtonModule,
    MatIconModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
  ],
  templateUrl: './blog-artifact-viewer.component.html',
  styleUrl: './blog-artifact-viewer.component.scss',
})
export class BlogArtifactViewerComponent implements OnInit, OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly api = inject(BloggingApiService);
  private readonly sanitizer = inject(DomSanitizer);

  jobId: string | null = null;
  artifactName: string | null = null;
  content: string | object | null = null;
  loading = true;
  error: string | null = null;

  readonly artifactLabel = artifactLabel;

  ngOnInit(): void {
    this.jobId = this.route.snapshot.paramMap.get('jobId');
    this.artifactName = this.route.snapshot.paramMap.get('artifactName');
    if (this.jobId && this.artifactName) {
      this.api.getJobArtifactContent(this.jobId, this.artifactName).subscribe({
        next: (res) => {
          this.content = res.content;
          this.loading = false;
          this.updateTitle();
        },
        error: (err) => {
          this.error = err?.error?.detail ?? err?.message ?? 'Failed to load artifact';
          this.loading = false;
          this.updateTitle();
        },
      });
    } else {
      this.error = 'Missing job or artifact';
      this.loading = false;
    }
  }

  ngOnDestroy(): void {}

  private updateTitle(): void {
    const label = this.artifactName ? this.artifactLabel(this.artifactName) : 'Artifact';
    const job = this.jobId ?? '';
    document.title = `${label} · ${job} · Blogging`;
  }

  getDisplayContent(): string {
    if (this.content == null) return '';
    if (typeof this.content === 'string') return this.content;
    return JSON.stringify(this.content, null, 2);
  }

  getMarkdownHtml(): SafeHtml {
    if (this.content == null || !this.isMarkdown()) return this.sanitizer.bypassSecurityTrustHtml('');
    const text = typeof this.content === 'string' ? this.content : JSON.stringify(this.content, null, 2);
    if (!text?.trim()) return this.sanitizer.bypassSecurityTrustHtml('');
    try {
      const result = marked.parse(text);
      const html = typeof result === 'string' ? result : '';
      return this.sanitizer.bypassSecurityTrustHtml(html || `<pre>${this.escapeHtml(text)}</pre>`);
    } catch {
      return this.sanitizer.bypassSecurityTrustHtml(`<pre>${this.escapeHtml(text)}</pre>`);
    }
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  isMarkdown(): boolean {
    return !!this.artifactName?.endsWith('.md');
  }

  isJson(): boolean {
    return !!this.artifactName?.endsWith('.json');
  }

  getDownloadUrl(): string {
    if (!this.jobId || !this.artifactName) return '#';
    return this.api.getJobArtifactDownloadUrl(this.jobId, this.artifactName);
  }
}
