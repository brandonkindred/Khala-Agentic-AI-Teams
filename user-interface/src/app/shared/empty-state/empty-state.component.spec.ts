import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { EmptyStateComponent } from './empty-state.component';

// `color-contrast` is disabled because jsdom can't paint; contrast is
// enforced by the --kh-* token system + the SCSS contrast guard spec.
const axeOptions = { rules: { 'color-contrast': { enabled: false } } };

describe('EmptyStateComponent', () => {
  let fixture: ComponentFixture<EmptyStateComponent>;
  let component: EmptyStateComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [EmptyStateComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(EmptyStateComponent);
    component = fixture.componentInstance;
  });

  it('renders title and description inside the live region, defaulting to h2', () => {
    component.title = 'Nothing yet';
    component.description = 'Run something first.';
    fixture.detectChanges();
    const status = fixture.nativeElement.querySelector('[role="status"]') as HTMLElement;
    expect(status).toBeTruthy();
    expect(status.querySelector('h2')?.textContent).toContain('Nothing yet');
    expect(status.textContent).toContain('Run something first.');
  });

  it('renders an h3 when headingLevel is 3', () => {
    component.title = 'Section empty';
    component.headingLevel = 3;
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('h3')?.textContent).toContain('Section empty');
    expect(fixture.nativeElement.querySelector('h2')).toBeNull();
  });

  it('keeps interactive examples outside the live region and emits clicks', () => {
    component.examples = ['Do the thing'];
    fixture.detectChanges();
    const status = fixture.nativeElement.querySelector('[role="status"]') as HTMLElement;
    expect(status.querySelector('button')).toBeNull();

    const emitted = vi.fn();
    component.exampleClick.subscribe(emitted);
    const button = fixture.nativeElement.querySelector('.kh-empty-example-btn') as HTMLButtonElement;
    expect(button.getAttribute('aria-label')).toBe('Start with: Do the thing');
    button.click();
    expect(emitted).toHaveBeenCalledWith('Do the thing');
  });

  it('has no axe violations', async () => {
    component.title = 'Nothing yet';
    component.description = 'Run something first.';
    component.examples = ['Example one'];
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('h2')).toBeTruthy();
    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);
});
