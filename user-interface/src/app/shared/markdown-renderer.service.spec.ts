import { SecurityContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { DomSanitizer } from '@angular/platform-browser';
import { beforeEach, describe, expect, it } from 'vitest';
import { MarkdownRendererService } from './markdown-renderer.service';

describe('MarkdownRendererService', () => {
  let service: MarkdownRendererService;
  let sanitizer: DomSanitizer;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    service = TestBed.inject(MarkdownRendererService);
    sanitizer = TestBed.inject(DomSanitizer);
  });

  const unwrap = (safe: unknown): string => sanitizer.sanitize(SecurityContext.HTML, safe as string) ?? '';

  describe('renderToHtmlString', () => {
    it('returns empty string for empty input without calling marked', () => {
      expect(service.renderToHtmlString('')).toBe('');
    });

    it('renders and sanitizes real markdown', () => {
      const html = service.renderToHtmlString('# Hello');
      expect(html).toContain('<h1');
      expect(html).toContain('Hello');
    });

    it('strips disallowed tags via DOMPurify', () => {
      const html = service.renderToHtmlString('<script>alert(1)</script>');
      expect(html).not.toContain('<script');
    });
  });

  describe('renderToSafeHtml', () => {
    it('returns an empty SafeHtml for empty/whitespace input', () => {
      expect(unwrap(service.renderToSafeHtml(''))).toBe('');
      expect(unwrap(service.renderToSafeHtml('   '))).toBe('');
    });

    it('renders real markdown as SafeHtml', () => {
      const html = unwrap(service.renderToSafeHtml('# Hello world'));
      expect(html).toContain('Hello world');
    });

    it('falls back to an escaped <pre> when sanitizing strips everything', () => {
      const html = unwrap(service.renderToSafeHtml('<script>alert(1)</script>'));
      expect(html).toContain('<pre');
      expect(html).not.toContain('<script');
      expect(html).toContain('alert(1)');
    });

    it('applies fallbackClass to the fallback <pre>', () => {
      const html = unwrap(service.renderToSafeHtml('<script>alert(1)</script>', 'markdown-fallback'));
      expect(html).toContain('markdown-fallback');
    });

    it('omits the class attribute on the fallback <pre> when fallbackClass is not given', () => {
      const html = unwrap(service.renderToSafeHtml('<script>alert(1)</script>'));
      expect(html).toMatch(/<pre>/);
    });
  });
});
