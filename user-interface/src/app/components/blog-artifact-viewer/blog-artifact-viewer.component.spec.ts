import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { SecurityContext } from '@angular/core';
import { DomSanitizer } from '@angular/platform-browser';
import { vi } from 'vitest';
import { BloggingApiService } from '../../services/blogging-api.service';
import { BlogArtifactViewerComponent } from './blog-artifact-viewer.component';

describe('BlogArtifactViewerComponent', () => {
  let component: BlogArtifactViewerComponent;
  let fixture: ComponentFixture<BlogArtifactViewerComponent>;
  let apiSpy: { getJobArtifactContent: ReturnType<typeof vi.fn>; getJobArtifactDownloadUrl: ReturnType<typeof vi.fn> };

  const buildFixture = (artifactName: string, content: string | object) => {
    const routeStub = {
      snapshot: {
        paramMap: {
          get: vi.fn((key: string) => (key === 'jobId' ? 'job-1' : artifactName)),
        },
      },
    } as unknown as ActivatedRoute;

    apiSpy = {
      getJobArtifactContent: vi.fn().mockReturnValue(of({ name: artifactName, content })),
      getJobArtifactDownloadUrl: vi.fn().mockReturnValue('/download-url'),
    };

    TestBed.configureTestingModule({
      imports: [BlogArtifactViewerComponent],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: ActivatedRoute, useValue: routeStub },
        { provide: BloggingApiService, useValue: apiSpy },
      ],
    });

    fixture = TestBed.createComponent(BlogArtifactViewerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  };

  afterEach(() => TestBed.resetTestingModule());

  it('creates and loads artifact content', () => {
    buildFixture('notes.md', '# Hello');
    expect(component).toBeTruthy();
    expect(component.loading).toBe(false);
    expect(component.content).toBe('# Hello');
  });

  it('neutralizes malicious HTML in markdown artifacts while keeping safe content', () => {
    buildFixture('notes.md', '<script>alert(1)</script>\n# Safe Title');

    const sanitizer = TestBed.inject(DomSanitizer);
    const html = sanitizer.sanitize(SecurityContext.HTML, component.getMarkdownHtml()) ?? '';

    expect(html).not.toContain('<script');
    expect(html).not.toContain('alert(1)');
    expect(html).toContain('Safe Title');
  });

  it('returns empty markdown html for non-markdown artifacts', () => {
    buildFixture('notes.json', '{"a": 1}');
    const sanitizer = TestBed.inject(DomSanitizer);
    const html = sanitizer.sanitize(SecurityContext.HTML, component.getMarkdownHtml()) ?? '';
    expect(html).toBe('');
  });
});
