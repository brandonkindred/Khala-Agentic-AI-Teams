import { Injectable, inject } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { marked } from 'marked';
import DOMPurify from 'dompurify';

/**
 * Single marked.parse() + DOMPurify.sanitize() pipeline shared by every
 * component that renders LLM-authored markdown, replacing what were four
 * independently copy-pasted implementations (architecture-results,
 * blog-artifact-viewer, blogging-dashboard, deepthought-dashboard).
 */
@Injectable({ providedIn: 'root' })
export class MarkdownRendererService {
  private readonly sanitizer = inject(DomSanitizer);

  /**
   * Sanitized HTML string with no `SafeHtml` wrapper and no fallback on a
   * parse failure — for `[innerHTML]` bindings that go through Angular's own
   * sanitizer as a second pass rather than pre-trusting the result.
   */
  renderToHtmlString(text: string): string {
    if (!text) return '';
    return DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
  }

  /**
   * `SafeHtml`, pre-trusted via `bypassSecurityTrustHtml`, with an
   * HTML-escaped `<pre>` fallback when the input is empty or marked throws.
   * `fallbackClass`, when given, is applied to that fallback `<pre>`.
   */
  renderToSafeHtml(text: string, fallbackClass?: string): SafeHtml {
    if (!text?.trim()) return this.sanitizer.bypassSecurityTrustHtml('');
    const openTag = fallbackClass ? `<pre class="${fallbackClass}">` : '<pre>';
    try {
      const html = DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
      return this.sanitizer.bypassSecurityTrustHtml(html || `${openTag}${this.escapeHtml(text)}</pre>`);
    } catch {
      return this.sanitizer.bypassSecurityTrustHtml(`${openTag}${this.escapeHtml(text)}</pre>`);
    }
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}
