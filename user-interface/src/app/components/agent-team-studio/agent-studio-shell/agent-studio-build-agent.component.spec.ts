import { Component, EventEmitter, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { AgentStudioStateService } from '../../../services/agent-studio-state.service';
import { AgentCatalogComponent } from '../agent-console/agent-catalog/agent-catalog.component';
import { AgentProvisioningDashboardComponent } from '../agent-provisioning-dashboard/agent-provisioning-dashboard.component';
import { AgentStudioBuildAgentComponent } from './agent-studio-build-agent.component';

/** Stand-in for the catalog: same selector + the one output Stage 1 wires, so
 *  no catalog HTTP fetch runs in these unit tests. */
@Component({ selector: 'app-agent-catalog', standalone: true, template: '' })
class StubAgentCatalogComponent {
  @Output() readonly requestRun = new EventEmitter<string>();
}

/** Stand-in for the provisioning dashboard (self-contained; polls + forms), so
 *  opening the slide-out doesn't start real polling/HTTP in tests. */
@Component({ selector: 'app-agent-provisioning-dashboard', standalone: true, template: '' })
class StubProvisioningDashboardComponent {}

describe('AgentStudioBuildAgentComponent', () => {
  let fixture: ComponentFixture<AgentStudioBuildAgentComponent>;
  let component: AgentStudioBuildAgentComponent;
  let state: AgentStudioStateService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioBuildAgentComponent, NoopAnimationsModule],
      providers: [AgentStudioStateService],
    })
      .overrideComponent(AgentStudioBuildAgentComponent, {
        remove: { imports: [AgentCatalogComponent, AgentProvisioningDashboardComponent] },
        add: { imports: [StubAgentCatalogComponent, StubProvisioningDashboardComponent] },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AgentStudioBuildAgentComponent);
    component = fixture.componentInstance;
    state = TestBed.inject(AgentStudioStateService);
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('shows the pick-an-agent hint and embeds the catalog before any selection', () => {
    expect(component.selectedAgentId()).toBeNull();
    expect(fixture.nativeElement.querySelector('app-agent-catalog')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.studio-build__hint')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.studio-build__selected')).toBeNull();
  });

  it('records the catalog selection as the journey agent and shows the selected bar', () => {
    const catalog = fixture.debugElement.query(By.directive(StubAgentCatalogComponent));
    (catalog.componentInstance as StubAgentCatalogComponent).requestRun.emit('blogging.planner');
    fixture.detectChanges();

    expect(state.registryAgentId()).toBe('blogging.planner');
    const selected = fixture.nativeElement.querySelector('.studio-build__selected');
    expect(selected).toBeTruthy();
    expect(selected.textContent).toContain('blogging.planner');
    expect(fixture.nativeElement.querySelector('.studio-build__hint')).toBeNull();
  });

  it('keeps the provisioning slide-out closed until requested', () => {
    expect(component.provisionOpen()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-agent-provisioning-dashboard')).toBeNull();
  });

  it('opens and closes the provisioning slide-out', () => {
    fixture.nativeElement.querySelector('.studio-build__provision-btn').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(true);
    expect(fixture.nativeElement.querySelector('app-agent-provisioning-dashboard')).toBeTruthy();

    fixture.nativeElement.querySelector('.studio-build__provision-head button').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-agent-provisioning-dashboard')).toBeNull();
  });

  it('closes the provisioning slide-out when the scrim is clicked', () => {
    component.openProvision();
    fixture.detectChanges();
    fixture.nativeElement.querySelector('.studio-build__scrim').click();
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(false);
  });

  it('marks the provisioning panel as a focus-trapping modal', () => {
    component.openProvision();
    fixture.detectChanges();
    const panel = fixture.nativeElement.querySelector('.studio-build__provision-panel');
    expect(panel.getAttribute('aria-modal')).toBe('true');
    expect(panel.getAttribute('role')).toBe('dialog');
    // CDK focus trap directive is applied (keeps Tab inside the modal).
    expect(panel.hasAttribute('cdkTrapFocus')).toBe(true);
  });

  it('closes the provisioning slide-out on Escape', () => {
    component.openProvision();
    fixture.detectChanges();
    const panel = fixture.nativeElement.querySelector('.studio-build__provision-panel');
    panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    fixture.detectChanges();
    expect(component.provisionOpen()).toBe(false);
  });
});
