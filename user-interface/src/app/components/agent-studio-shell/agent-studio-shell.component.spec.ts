import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { AgentStudioShellComponent } from './agent-studio-shell.component';

describe('AgentStudioShellComponent', () => {
  let component: AgentStudioShellComponent;
  let fixture: ComponentFixture<AgentStudioShellComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioShellComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(AgentStudioShellComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create with all four stages and start on Build', () => {
    expect(component).toBeTruthy();
    expect(component.stages).toHaveLength(4);
    expect(component.activeStageDef().key).toBe('build');
  });

  it('renders one stepper indicator per stage with the first active', () => {
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    expect(steps.length).toBe(4);
    expect(steps[0].classList.contains('is-active')).toBe(true);
    expect(steps[1].classList.contains('is-active')).toBe(false);
  });

  it('renders the disabled Save/Load draft header placeholders (spec §3.5)', () => {
    const draftButtons = fixture.nativeElement.querySelectorAll('.studio__draft-btn');
    expect(draftButtons.length).toBe(2);
    const labels = Array.from(draftButtons).map((b) => (b as HTMLElement).textContent?.trim());
    expect(labels[0]).toContain('Save draft');
    expect(labels[1]).toContain('Load draft');
    expect((draftButtons[0] as HTMLButtonElement).disabled).toBe(true);
    expect((draftButtons[1] as HTMLButtonElement).disabled).toBe(true);
  });

  it('stepper indicators are not buttons — there is no backward navigation', () => {
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    for (const step of Array.from(steps)) {
      expect((step as HTMLElement).tagName).toBe('DIV');
    }
    // No clickable control exists inside the stepper region.
    expect(fixture.nativeElement.querySelector('.studio__stepper button')).toBeNull();
  });

  it('onContinue advances and shows the stage-specific forward label', () => {
    let button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__continue');
    expect(button.textContent?.trim()).toBe('Test this agent →');
    component.onContinue();
    fixture.detectChanges();
    expect(component.state.activeStage()).toBe(1);
    expect(component.activeStageDef().key).toBe('test');
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    expect(steps[0].classList.contains('is-done')).toBe(true);
    expect(steps[1].classList.contains('is-active')).toBe(true);
    button = fixture.nativeElement.querySelector('.studio__continue');
    expect(button.textContent?.trim()).toBe('Add to team →');
  });

  it('hides the forward button on the final stage and advance is a no-op', () => {
    component.state.navigateToStage(3);
    fixture.detectChanges();
    expect(component.activeStageDef().key).toBe('personas');
    expect(component.state.canAdvance()).toBe(false);
    expect(fixture.nativeElement.querySelector('.studio__continue')).toBeNull();
    component.onContinue();
    expect(component.state.activeStage()).toBe(3);
  });

  it('passes the active stage and live handoff into the placeholder', () => {
    component.state.setRegistryAgentId('reg-1');
    fixture.detectChanges();
    const map = new Map(component.state.handoff() ? Object.entries(component.state.handoff()) : []);
    expect(map.get('registryAgentId')).toBe('reg-1');
    expect(fixture.nativeElement.querySelector('app-agent-studio-stage-placeholder')).toBeTruthy();
  });
});
