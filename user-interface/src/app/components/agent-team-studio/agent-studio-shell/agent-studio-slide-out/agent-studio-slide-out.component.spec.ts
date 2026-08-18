import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { AgentStudioSlideOutComponent } from './agent-studio-slide-out.component';

describe('AgentStudioSlideOutComponent', () => {
  let fixture: ComponentFixture<AgentStudioSlideOutComponent>;
  let component: AgentStudioSlideOutComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioSlideOutComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(AgentStudioSlideOutComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('heading', 'Browse agents');
  });

  it('renders nothing while closed', () => {
    fixture.componentRef.setInput('open', false);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.studio-slide-out__scrim')).toBeNull();
    expect(fixture.nativeElement.querySelector('.studio-slide-out__panel')).toBeNull();
  });

  it('renders the scrim, a focus-trapping dialog panel, and the heading when open', () => {
    fixture.componentRef.setInput('open', true);
    fixture.detectChanges();
    const scrim = fixture.nativeElement.querySelector('.studio-slide-out__scrim');
    const panel = fixture.nativeElement.querySelector('.studio-slide-out__panel');
    expect(scrim).toBeTruthy();
    expect(panel).toBeTruthy();
    expect(panel.getAttribute('role')).toBe('dialog');
    expect(panel.getAttribute('aria-modal')).toBe('true');
    expect(panel.getAttribute('aria-label')).toBe('Browse agents');
    expect(panel.hasAttribute('cdkTrapFocus')).toBe(true);
    expect(panel.querySelector('h2')?.textContent).toBe('Browse agents');
  });

  it('falls back to "Close {heading}" for the close button aria-label when none is given', () => {
    fixture.componentRef.setInput('open', true);
    fixture.detectChanges();
    const closeBtn = fixture.nativeElement.querySelector('.studio-slide-out__head button');
    expect(closeBtn.getAttribute('aria-label')).toBe('Close Browse agents');
  });

  it('uses an explicit closeButtonLabel when provided', () => {
    fixture.componentRef.setInput('open', true);
    fixture.componentRef.setInput('closeButtonLabel', 'Close provisioning panel');
    fixture.detectChanges();
    const closeBtn = fixture.nativeElement.querySelector('.studio-slide-out__head button');
    expect(closeBtn.getAttribute('aria-label')).toBe('Close provisioning panel');
  });

  it('defaults the panel width and honours an override', () => {
    fixture.componentRef.setInput('open', true);
    fixture.detectChanges();
    let panel = fixture.nativeElement.querySelector('.studio-slide-out__panel') as HTMLElement;
    expect(panel.style.width).toBe('min(560px, 92vw)');

    fixture.componentRef.setInput('panelWidth', 'min(920px, 96vw)');
    fixture.detectChanges();
    panel = fixture.nativeElement.querySelector('.studio-slide-out__panel') as HTMLElement;
    expect(panel.style.width).toBe('min(920px, 96vw)');
  });

  it('emits closeRequested exactly once on scrim click, without mutating open', () => {
    const spy = vi.fn();
    component.closeRequested.subscribe(spy);
    fixture.componentRef.setInput('open', true);
    fixture.detectChanges();
    fixture.nativeElement.querySelector('.studio-slide-out__scrim').click();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(component.open).toBe(true);
  });

  it('emits closeRequested exactly once on close-button click, without mutating open', () => {
    const spy = vi.fn();
    component.closeRequested.subscribe(spy);
    fixture.componentRef.setInput('open', true);
    fixture.detectChanges();
    fixture.nativeElement.querySelector('.studio-slide-out__head button').click();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(component.open).toBe(true);
  });

  it('emits closeRequested exactly once on Escape, without mutating open', () => {
    const spy = vi.fn();
    component.closeRequested.subscribe(spy);
    fixture.componentRef.setInput('open', true);
    fixture.detectChanges();
    const panel = fixture.nativeElement.querySelector('.studio-slide-out__panel');
    panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(component.open).toBe(true);
  });

  it('projects body content only while open', () => {
    const host = TestBed.createComponent(SlideOutHostComponent);
    host.componentInstance.open = false;
    host.detectChanges();
    expect(host.nativeElement.querySelector('.projected-stub')).toBeNull();

    host.componentInstance.open = true;
    host.detectChanges();
    expect(host.nativeElement.querySelector('.projected-stub')).toBeTruthy();
  });
});

@Component({
  standalone: true,
  imports: [AgentStudioSlideOutComponent],
  template: `
    <app-agent-studio-slide-out [open]="open" heading="Browse agents">
      <div class="projected-stub">projected</div>
    </app-agent-studio-slide-out>
  `,
})
class SlideOutHostComponent {
  open = false;
}
