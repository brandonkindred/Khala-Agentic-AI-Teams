import { Component, inject, OnInit } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { marked } from 'marked';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';

@Component({
  selector: 'app-planning-artifact-detail',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatButtonModule,
    MatIconModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
  ],
  templateUrl: './planning-artifact-detail.component.html',
  styleUrl: './planning-artifact-detail.component.scss',
})
export class PlanningArtifactDetailComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly api = inject(SoftwareEngineeringApiService);
  private readonly sanitizer = inject(DomSanitizer);

  jobId: string | null = null;
  artifactName: string | null = null;
  content: string | null = null;
  loading = true;
  error: string | null = null;

  ngOnInit(): void {
    this.jobId = this.route.snapshot.paramMap.get('jobId');
    this.artifactName = this.route.snapshot.paramMap.get('artifactName');
    if (this.jobId && this.artifactName) {
      this.api.getPlanningV2ArtifactContent(this.jobId, this.artifactName).subscribe({
        next: (res) => {
          this.content = typeof res.content === 'string' ? res.content : JSON.stringify(res.content, null, 2);
          this.loading = false;
        },
        error: (err) => {
          this.error = err?.error?.detail ?? err?.message ?? 'Failed to load artifact';
          this.loading = false;
        },
      });
    } else {
      this.error = 'Missing job or artifact';
      this.loading = false;
    }
  }

  isMarkdown(): boolean {
    return !!this.artifactName?.endsWith('.md');
  }

  isJson(): boolean {
    return !!this.artifactName?.endsWith('.json');
  }

  getMarkdownHtml(): SafeHtml {
    if (!this.content || !this.isMarkdown()) return this.sanitizer.bypassSecurityTrustHtml('');
    try {
      const result = marked.parse(this.content);
      const html = typeof result === 'string' ? result : '';
      return this.sanitizer.bypassSecurityTrustHtml(html);
    } catch {
      return this.sanitizer.bypassSecurityTrustHtml(`<pre>${this.escapeHtml(this.content)}</pre>`);
    }
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  artifactLabel(): string {
    if (!this.artifactName) return 'Artifact';
    return this.artifactName.replace(/_/g, ' ').replace(/\.\w+$/, '').replace(/\b\w/g, c => c.toUpperCase());
  }
}
