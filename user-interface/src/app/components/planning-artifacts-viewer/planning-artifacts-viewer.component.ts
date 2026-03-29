import { Component, Input, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { MatCardModule } from '@angular/material/card';
import { MatListModule } from '@angular/material/list';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { marked } from 'marked';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { PlanningArtifactMeta } from '../../models';

@Component({
  selector: 'app-planning-artifacts-viewer',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatCardModule,
    MatListModule,
    MatIconModule,
    MatChipsModule,
    MatButtonModule,
    MatProgressBarModule,
  ],
  templateUrl: './planning-artifacts-viewer.component.html',
  styleUrl: './planning-artifacts-viewer.component.scss',
})
export class PlanningArtifactsViewerComponent implements OnInit, OnDestroy {
  @Input() jobId!: string;

  private readonly api = inject(SoftwareEngineeringApiService);
  private readonly sanitizer = inject(DomSanitizer);
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  artifacts: PlanningArtifactMeta[] = [];
  selectedArtifact: PlanningArtifactMeta | null = null;
  selectedContent: string | null = null;
  renderedHtml: SafeHtml | null = null;
  loadingContent = false;
  error: string | null = null;

  ngOnInit(): void {
    this.loadArtifacts();
    this.pollTimer = setInterval(() => this.loadArtifacts(), 15000);
  }

  ngOnDestroy(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  loadArtifacts(): void {
    this.api.getPlanningV2Artifacts(this.jobId).subscribe({
      next: (res) => {
        this.artifacts = res.artifacts;
        this.error = null;
        // If we have a selected artifact, check if it was updated
        if (this.selectedArtifact) {
          const updated = this.artifacts.find(a => a.name === this.selectedArtifact!.name);
          if (updated && updated.modified_at !== this.selectedArtifact.modified_at) {
            this.selectArtifact(updated);
          }
        }
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to load artifacts';
      },
    });
  }

  selectArtifact(artifact: PlanningArtifactMeta): void {
    this.selectedArtifact = artifact;
    this.loadingContent = true;
    this.selectedContent = null;
    this.renderedHtml = null;

    this.api.getPlanningV2ArtifactContent(this.jobId, artifact.name).subscribe({
      next: (res) => {
        const content = typeof res.content === 'string' ? res.content : JSON.stringify(res.content, null, 2);
        this.selectedContent = content;
        if (artifact.name.endsWith('.md')) {
          this.renderMarkdown(content);
        }
        this.loadingContent = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to load artifact content';
        this.loadingContent = false;
      },
    });
  }

  private renderMarkdown(text: string): void {
    try {
      const result = marked.parse(text);
      const html = typeof result === 'string' ? result : '';
      this.renderedHtml = this.sanitizer.bypassSecurityTrustHtml(html);
    } catch {
      this.renderedHtml = this.sanitizer.bypassSecurityTrustHtml(`<pre>${this.escapeHtml(text)}</pre>`);
    }
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  isSelected(artifact: PlanningArtifactMeta): boolean {
    return this.selectedArtifact?.name === artifact.name;
  }

  isPrimaryDoc(artifact: PlanningArtifactMeta): boolean {
    return artifact.name === 'planning_document.md';
  }

  formatSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    return `${(bytes / 1024).toFixed(1)} KB`;
  }

  formatDate(iso: string): string {
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }

  artifactIcon(artifact: PlanningArtifactMeta): string {
    if (this.isPrimaryDoc(artifact)) return 'menu_book';
    if (artifact.name.endsWith('.json')) return 'data_object';
    return 'description';
  }
}
