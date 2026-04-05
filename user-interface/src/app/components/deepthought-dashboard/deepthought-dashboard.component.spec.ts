import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { DeepthoughtDashboardComponent } from './deepthought-dashboard.component';

describe('DeepthoughtDashboardComponent', () => {
  let component: DeepthoughtDashboardComponent;
  let fixture: ComponentFixture<DeepthoughtDashboardComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DeepthoughtDashboardComponent],
      providers: [provideHttpClient(), provideAnimations()],
    }).compileComponents();

    fixture = TestBed.createComponent(DeepthoughtDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should start with empty messages', () => {
    expect(component.messages.length).toBe(0);
  });

  it('should format agent names correctly', () => {
    expect(component.formatAgentName('quantum_physics_expert')).toBe('Quantum Physics Expert');
    expect(component.formatAgentName('general_analyst')).toBe('General Analyst');
  });

  it('should count agents in tree', () => {
    const tree = {
      agent_id: '1',
      agent_name: 'root',
      depth: 0,
      focus_question: 'q',
      answer: 'a',
      confidence: 0.9,
      was_decomposed: true,
      deliberation_notes: null,
      reused_from_cache: false,
      child_results: [
        {
          agent_id: '2',
          agent_name: 'child',
          depth: 1,
          focus_question: 'q2',
          answer: 'a2',
          confidence: 0.8,
          was_decomposed: false,
          deliberation_notes: null,
          reused_from_cache: false,
          child_results: [],
        },
      ],
    };
    expect(component.countAgents(tree)).toBe(2);
  });

  it('should toggle settings', () => {
    expect(component.showSettings).toBe(false);
    component.toggleSettings();
    expect(component.showSettings).toBe(true);
    component.toggleSettings();
    expect(component.showSettings).toBe(false);
  });

  it('should clear state on new conversation', () => {
    component.messages = [{ role: 'user', content: 'test', timestamp: '2024-01-01' }];
    component.newConversation();
    expect(component.messages.length).toBe(0);
    expect(component.conversationHistory.length).toBe(0);
    expect(component.selectedTreeSnapshot).toBeNull();
  });
});
