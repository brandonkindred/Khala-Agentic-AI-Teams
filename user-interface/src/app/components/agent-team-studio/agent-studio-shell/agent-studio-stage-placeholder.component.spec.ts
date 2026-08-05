import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { AgentStudioHandoffState } from '../../../models/agent-studio.model';
import { AgentStudioStagePlaceholderComponent } from './agent-studio-stage-placeholder.component';

const EMPTY: AgentStudioHandoffState = {
  registryAgentId: null,
  teamId: null,
  processId: null,
  personaId: null,
  draftAgentId: null,
};

describe('AgentStudioStagePlaceholderComponent', () => {
  let component: AgentStudioStagePlaceholderComponent;
  let fixture: ComponentFixture<AgentStudioStagePlaceholderComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AgentStudioStagePlaceholderComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(AgentStudioStagePlaceholderComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('title', 'Build Agent');
    fixture.componentRef.setInput('blurb', 'Author a new agent.');
    fixture.componentRef.setInput('icon', 'build_circle');
    fixture.componentRef.setInput('handoff', EMPTY);
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('renders the stage title, blurb, and icon', () => {
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.stage-stub__title')?.textContent).toContain('Build Agent');
    expect(el.querySelector('.stage-stub__blurb')?.textContent).toContain('Author a new agent.');
    expect(el.querySelector('.stage-stub__icon')?.textContent).toContain('build_circle');
  });

  it('renders an em dash for every empty handoff slot', () => {
    const entries = component.entries();
    expect(entries).toHaveLength(5);
    expect(entries.every(([, value]) => value === '—')).toBe(true);
  });

  it('renders the handoff values when present', () => {
    fixture.componentRef.setInput('handoff', { ...EMPTY, registryAgentId: 'reg-1', teamId: 'team-1' });
    fixture.detectChanges();
    const map = new Map(component.entries());
    expect(map.get('registryAgentId')).toBe('reg-1');
    expect(map.get('teamId')).toBe('team-1');
    expect(map.get('processId')).toBe('—');
    const rows = fixture.nativeElement.querySelectorAll('.stage-stub__row');
    expect(rows.length).toBe(5);
  });
});
