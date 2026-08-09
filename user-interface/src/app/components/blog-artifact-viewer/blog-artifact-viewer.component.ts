import { Component, inject, OnInit } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CommonModule } from '@angular/common';
import { SafeHtml } from '@angular/platform-browser';
import { BloggingApiService } from '../../services/blogging-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { MarkdownRendererService } from '../../shared/markdown-renderer.service';
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
export class BlogArtifactViewerComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly api = inject(BloggingApiService);
  private readonly markdownRenderer = inject(MarkdownRendererService);

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
    if (this.content == null || !this.isMarkdown()) return this.markdownRenderer.renderToSafeHtml('');
    const text = typeof this.content === 'string' ? this.content : JSON.stringify(this.content, null, 2);
    return this.markdownRenderer.renderToSafeHtml(text);
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
