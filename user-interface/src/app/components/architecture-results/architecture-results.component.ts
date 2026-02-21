import {
  Component,
  Input,
  signal,
  effect,
  inject,
  ElementRef,
  ChangeDetectorRef,
  AfterViewInit,
  AfterViewChecked,
} from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { MatCardModule } from '@angular/material/card';
import { MatExpansionModule } from '@angular/material/expansion';
import { marked } from 'marked';
import type { ArchitectDesignResponse } from '../../models';

@Component({
  selector: 'app-architecture-results',
  standalone: true,
  imports: [MatCardModule, MatExpansionModule],
  templateUrl: './architecture-results.component.html',
  styleUrl: './architecture-results.component.scss',
})
export class ArchitectureResultsComponent implements AfterViewInit, AfterViewChecked {
  private readonly sanitizer = inject(DomSanitizer);
  private readonly elementRef = inject(ElementRef);
  private readonly cdr = inject(ChangeDetectorRef);

  @Input({ required: true }) data!: ArchitectDesignResponse;

  overviewHtml = signal<SafeHtml>('');
  architectureDocHtml = signal<SafeHtml>('');
  diagramEntries: Array<{ name: string; id: string; rendered: boolean }> = [];
  private mermaidRendered = new Set<string>();

  objectEntries(obj: Record<string, unknown>): Array<{ key: string; value: unknown }> {
    return Object.entries(obj).map(([key, value]) => ({ key, value }));
  }

  isObjectDecision(d: unknown): boolean {
    return d !== null && typeof d === 'object';
  }

  getDecisionTitle(d: unknown): string {
    if (d && typeof d === 'object') {
      const o = d as Record<string, unknown>;
      return (o['id'] ?? o['title'] ?? o['name'] ?? '') as string;
    }
    return '';
  }

  getDecisionDetails(d: unknown): Array<{ key: string; value: unknown }> {
    if (d && typeof d === 'object') {
      const o = d as Record<string, unknown>;
      return Object.entries(o)
        .filter(([k]) => !['id', 'title', 'name'].includes(k))
        .filter(([, v]) => v != null)
        .map(([key, value]) => ({ key, value }));
    }
    return [];
  }

  constructor() {
    effect(() => {
      const d = this.data;
      if (!d) return;
      this.overviewHtml.set(this.renderMarkdown(d.overview));
      this.architectureDocHtml.set(this.renderMarkdown(d.architecture_document));
      this.diagramEntries = Object.keys(d.diagrams || {}).map((name, i) => ({
        name,
        id: `mermaid-diagram-${i}`,
        rendered: false,
      }));
    });
  }

  ngAfterViewInit(): void {
    this.renderMermaidDiagrams();
  }

  ngAfterViewChecked(): void {
    this.renderMermaidDiagrams();
  }

  private renderMarkdown(text: string): SafeHtml {
    if (!text?.trim()) return '';
    try {
      const result = marked.parse(text);
      const html = typeof result === 'string' ? result : '';
      return this.sanitizer.bypassSecurityTrustHtml(html || `<pre class="markdown-fallback">${this.escapeHtml(text)}</pre>`);
    } catch {
      return this.sanitizer.bypassSecurityTrustHtml(
        `<pre class="markdown-fallback">${this.escapeHtml(text)}</pre>`
      );
    }
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  private async renderMermaidDiagrams(): Promise<void> {
    const diagrams = this.data?.diagrams;
    if (!diagrams || typeof diagrams !== 'object') return;

    for (let i = 0; i < this.diagramEntries.length; i++) {
      const { name, id } = this.diagramEntries[i];
      if (this.mermaidRendered.has(id)) continue;

      const container = this.elementRef.nativeElement.querySelector(
        `[data-mermaid-id="${id}"]`
      );
      if (!container) continue;

      const code = diagrams[name];
      if (!code?.trim()) continue;

      try {
        const mermaid = await import('mermaid').then((m) => m.default);
        mermaid
          .render(`mermaid-svg-${id}`, code)
          .then(({ svg }: { svg: string }) => {
            container.innerHTML = svg;
            this.mermaidRendered.add(id);
            this.cdr.markForCheck();
          })
          .catch(() => {
            container.innerHTML = `<pre class="mermaid-fallback"><code>${this.escapeHtml(code)}</code></pre>`;
            this.mermaidRendered.add(id);
            this.cdr.markForCheck();
          });
      } catch {
        container.innerHTML = `<pre class="mermaid-fallback"><code>${this.escapeHtml(code)}</code></pre>`;
        this.mermaidRendered.add(id);
        this.cdr.markForCheck();
      }
    }
  }
}
