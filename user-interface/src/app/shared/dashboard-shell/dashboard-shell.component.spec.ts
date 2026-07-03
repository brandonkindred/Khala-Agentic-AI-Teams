import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Title } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { axe } from 'vitest-axe';
import { DashboardShellComponent } from './dashboard-shell.component';

@Component({
  standalone: true,
  imports: [DashboardShellComponent],
  template: `
    <app-dashboard-shell
      title="Test Team"
      subtitle="Does test things"
      icon="science"
      [healthCheck]="health"
      [subTeams]="[{ label: 'Sub A', route: '/sub-a' }, { label: 'Sub B', route: '/sub-b' }]"
    >
      <button dashboardActions type="button">Action</button>
      <p>Body content</p>
    </app-dashboard-shell>
    <app-dashboard-shell title="Second Shell" />
  `,
})
class HostComponent {
  health = () => of({ status: 'ok' });
}

// `color-contrast` is disabled because jsdom can't paint; contrast is
// enforced by the --kh-* token system + the SCSS contrast guard spec.
const axeOptions = { rules: { 'color-contrast': { enabled: false } } };

describe('DashboardShellComponent', () => {
  let fixture: ComponentFixture<HostComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [HostComponent],
      providers: [provideRouter([])],
    }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    fixture.detectChanges();
  });

  it('renders the h1 title, subtitle, and sub-team nav', () => {
    const el: HTMLElement = fixture.nativeElement;
    const h1 = el.querySelector('h1');
    expect(h1?.textContent).toContain('Test Team');
    expect(el.textContent).toContain('Does test things');
    const nav = el.querySelector('nav[aria-label="Sub-teams"]');
    expect(nav).toBeTruthy();
    expect(nav?.querySelectorAll('a').length).toBe(2);
    expect(el.textContent).toContain('Body content');
  });

  it('gives each shell instance a unique labelled-by id', () => {
    const el: HTMLElement = fixture.nativeElement;
    const sections = el.querySelectorAll('section.dashboard-shell');
    expect(sections.length).toBe(2);
    const ids = Array.from(el.querySelectorAll('h1')).map((h) => h.id);
    expect(ids[0]).not.toBe(ids[1]);
    sections.forEach((section, i) => {
      expect(section.getAttribute('aria-labelledby')).toBe(ids[i]);
    });
  });

  it('sets the browser tab title from the title input', () => {
    const title = TestBed.inject(Title);
    expect(title.getTitle()).toContain('| Khala');
  });

  it('has no axe violations', async () => {
    // Guard: real content rendered before auditing.
    expect(fixture.nativeElement.querySelector('h1')).toBeTruthy();
    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);
});
