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

  it('onContinue advances the active stage and marks prior steps done', () => {
    component.onContinue();
    fixture.detectChanges();
    expect(component.state.activeStage()).toBe(1);
    expect(component.activeStageDef().key).toBe('test');
    const steps = fixture.nativeElement.querySelectorAll('.studio__step');
    expect(steps[0].classList.contains('is-done')).toBe(true);
    expect(steps[1].classList.contains('is-active')).toBe(true);
  });

  it('disables Continue once the final stage is reached', () => {
    component.state.navigateToStage(3);
    fixture.detectChanges();
    expect(component.activeStageDef().key).toBe('personas');
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__continue');
    expect(button.disabled).toBe(true);
    // A further advance is a no-op.
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
