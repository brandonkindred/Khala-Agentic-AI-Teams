import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { of, Subject, throwError } from 'rxjs';
import { vi, beforeEach } from 'vitest';
import { DeepthoughtApiService, StreamEvent } from '../../services/deepthought-api.service';
import { DeepthoughtDashboardComponent } from './deepthought-dashboard.component';
import type { AgentResult, KnowledgeEntry } from '../../models/deepthought.model';

const makeAgentResult = (overrides: Partial<AgentResult> = {}): AgentResult => ({
  agent_id: 'a1',
  agent_name: 'Root',
  depth: 0,
  focus_question: 'q',
  answer: 'a',
  confidence: 0.5,
  was_decomposed: false,
  deliberation_notes: null,
  reused_from_cache: false,
  child_results: [],
  ...overrides,
} as AgentResult);

describe('DeepthoughtDashboardComponent (extra coverage)', () => {
  let component: DeepthoughtDashboardComponent;
  let fixture: ComponentFixture<DeepthoughtDashboardComponent>;
  let apiStub: { askStream: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiStub = { askStream: vi.fn().mockReturnValue(of({ type: 'done' } as StreamEvent)) };
    await TestBed.configureTestingModule({
      imports: [DeepthoughtDashboardComponent],
      providers: [
        provideHttpClient(),
        provideAnimations(),
        { provide: DeepthoughtApiService, useValue: apiStub },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(DeepthoughtDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  // ---------------------------------------------------------------------
  // Formatting helpers
  // ---------------------------------------------------------------------

  it('formatAgentName transforms snake_case to Title Case', () => {
    expect(component.formatAgentName('hello_world')).toBe('Hello World');
  });

  it('formatTime parses valid ISO timestamps', () => {
    const result = component.formatTime('2025-01-01T12:30:00Z');
    expect(result.length).toBeGreaterThan(0);
  });

  it('formatTime returns empty for empty string', () => {
    expect(component.formatTime('')).toBe('');
  });

  it('renderMarkdown renders simple text', () => {
    const html = component.renderMarkdown('# Hello');
    expect(html).toContain('h1');
    expect(component.renderMarkdown('')).toBe('');
  });

  it('countAgents counts recursive children', () => {
    const tree = makeAgentResult({
      child_results: [
        makeAgentResult({ agent_id: 'c1' }),
        makeAgentResult({
          agent_id: 'c2',
          child_results: [makeAgentResult({ agent_id: 'c2a' })],
        }),
      ],
    });
    expect(component.countAgents(tree)).toBe(4);
  });

  it('getMaxDepth finds deepest node', () => {
    const tree = makeAgentResult({
      depth: 0,
      child_results: [
        makeAgentResult({ depth: 1 }),
        makeAgentResult({
          depth: 1,
          child_results: [makeAgentResult({ depth: 2 })],
        }),
      ],
    });
    expect(component.getMaxDepth(tree)).toBe(2);
    expect(component.getMaxDepth(makeAgentResult({ depth: 4 }))).toBe(4);
  });

  // ---------------------------------------------------------------------
  // Send / submit
  // ---------------------------------------------------------------------

  it('onSubmit ignores empty messages', () => {
    component.messageControl.setValue('   ');
    component.onSubmit();
    expect(apiStub.askStream).not.toHaveBeenCalled();
  });

  it('onSubmit ignores while processing', () => {
    component.messageControl.setValue('hello');
    component.isProcessing = true;
    component.onSubmit();
    expect(apiStub.askStream).not.toHaveBeenCalled();
  });

  it('onSubmit sends message via askStream and updates state', () => {
    component.messageControl.setValue('What is 2+2?');
    component.onSubmit();
    expect(apiStub.askStream).toHaveBeenCalled();
    expect(component.messages.length).toBe(1);
    expect(component.messages[0].role).toBe('user');
    expect(component.conversationHistory.length).toBe(1);
  });

  it('onSubmit reuses prior history when sending', () => {
    component.conversationHistory = [{ role: 'user', content: 'old' }, { role: 'assistant', content: 'oldresp' }];
    component.messageControl.setValue('new');
    component.onSubmit();
    const call = apiStub.askStream.mock.calls[0][0];
    expect(call.conversation_history.length).toBe(2);
    // The new message is sent separately in `message`, not in history yet
    expect(call.message).toBe('new');
  });

  it('sendMessage sets error on stream failure', () => {
    apiStub.askStream.mockReturnValue(throwError(() => new Error('net')));
    component.sendMessage('oops');
    expect(component.error).toBe('Connection lost. Please try again.');
    expect(component.lastFailedMessage).toBe('oops');
    expect(component.isProcessing).toBe(false);
  });

  // ---------------------------------------------------------------------
  // Stream event handling
  // ---------------------------------------------------------------------

  it('handles agent_event stream events', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('q');
    subject.next({
      type: 'agent_event',
      payload: { event_type: 'agent_spawned', agent_id: 'a1', agent_name: 'A', depth: 0 },
    } as StreamEvent);
    expect(component.liveAgentNodes.size).toBe(1);
    expect(component.activeAgentCount).toBe(1);
    expect(component.liveAgentNodes.get('a1')?.status).toBe('spawned');
  });

  it('maps known agent event types to statuses', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('q');
    const types = [
      ['agent_analysing', 'analysing'],
      ['agent_answering', 'answering'],
      ['agent_decomposing', 'decomposing'],
      ['agent_deliberating', 'deliberating'],
      ['agent_synthesising', 'synthesising'],
      ['agent_complete', 'complete'],
      ['budget_warning', 'budget_warning'],
      ['knowledge_reused', 'knowledge_reused'],
      ['unknown_event', 'spawned'], // fallback
    ];
    for (const [evtType, status] of types) {
      subject.next({
        type: 'agent_event',
        payload: { event_type: evtType, agent_id: evtType, agent_name: 'x', depth: 0 },
      } as StreamEvent);
      expect(component.liveAgentNodes.get(evtType as string)?.status).toBe(status);
    }
  });

  it('handles result stream event', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('q');
    const tree = makeAgentResult();
    const knowledge: KnowledgeEntry[] = [{ id: 'k1' } as KnowledgeEntry];
    subject.next({
      type: 'result',
      payload: {
        answer: 'final answer',
        agent_tree: tree,
        total_agents_spawned: 3,
        knowledge_entries: knowledge,
      },
    } as StreamEvent);
    expect(component.messages.length).toBeGreaterThanOrEqual(2);
    expect(component.messages.at(-1)?.role).toBe('assistant');
    expect(component.selectedTreeSnapshot).toBe(tree);
    expect(component.selectedKnowledge).toBe(knowledge);
    expect(component.expandedNodes.has('a1')).toBe(true);
  });

  it('handles result with no knowledge_entries field', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('q');
    const tree = makeAgentResult();
    subject.next({
      type: 'result',
      payload: { answer: 'a', agent_tree: tree, total_agents_spawned: 1 },
    } as StreamEvent);
    expect(component.selectedKnowledge).toEqual([]);
  });

  it('handles error stream event', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('q');
    subject.next({ type: 'error', payload: 'broken' } as StreamEvent);
    expect(component.error).toBe('broken');
    expect(component.lastFailedMessage).toBe('q');
    expect(component.isProcessing).toBe(false);
  });

  it('error event with non-user last message has null lastFailedMessage', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('q1');
    // Simulate assistant message arriving
    subject.next({
      type: 'result',
      payload: { answer: 'ok', agent_tree: makeAgentResult(), total_agents_spawned: 1 },
    } as StreamEvent);
    subject.next({ type: 'error', payload: 'err' } as StreamEvent);
    expect(component.lastFailedMessage).toBeNull();
  });

  it('handles done stream event', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('q');
    component.liveAgentNodes.set('x', { agent_id: 'x' } as never);
    subject.next({ type: 'done' } as StreamEvent);
    expect(component.isProcessing).toBe(false);
    expect(component.liveAgentNodes.size).toBe(0);
  });

  // ---------------------------------------------------------------------
  // retryLastMessage
  // ---------------------------------------------------------------------

  it('retryLastMessage does nothing without lastFailedMessage', () => {
    component.lastFailedMessage = null;
    component.retryLastMessage();
    expect(apiStub.askStream).not.toHaveBeenCalled();
  });

  it('retryLastMessage replays the failed message', () => {
    component.lastFailedMessage = 'retry me';
    component.messages = [{ role: 'user', content: 'retry me', timestamp: 'x' }];
    component.conversationHistory = [{ role: 'user', content: 'retry me' }];
    component.retryLastMessage();
    // The original user message is removed, then sendMessage re-adds it
    expect(component.messages.length).toBe(1);
    expect(apiStub.askStream).toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------
  // selectTree, toggleNode
  // ---------------------------------------------------------------------

  it('selectTree picks tree, sets knowledge from response message', () => {
    const tree = makeAgentResult();
    const knowledge: KnowledgeEntry[] = [{ id: 'k' } as KnowledgeEntry];
    component.messages = [
      { role: 'assistant', content: 'a', timestamp: 't', agentTree: tree, knowledge } as never,
    ];
    component.selectTree(tree);
    expect(component.selectedTreeSnapshot).toBe(tree);
    expect(component.selectedKnowledge).toBe(knowledge);
    expect(component.expandedNodes.has(tree.agent_id)).toBe(true);
  });

  it('selectTree clears knowledge when no matching message', () => {
    const tree = makeAgentResult({ agent_id: 'orphan' });
    component.messages = [];
    component.selectTree(tree);
    expect(component.selectedKnowledge).toEqual([]);
  });

  it('selectTree switches to tree tab on mobile', () => {
    const tree = makeAgentResult();
    (component as unknown as { isMobile: boolean }).isMobile = true;
    component.selectTree(tree);
    expect(component.mobileTab).toBe('tree');
  });

  it('toggleNode adds and removes from expanded set', () => {
    component.toggleNode('a1');
    expect(component.expandedNodes.has('a1')).toBe(true);
    component.toggleNode('a1');
    expect(component.expandedNodes.has('a1')).toBe(false);
  });

  it('liveAgentNodesArray returns values', () => {
    component.liveAgentNodes.set('x', { agent_id: 'x' } as never);
    expect(component.liveAgentNodesArray.length).toBe(1);
  });

  // ---------------------------------------------------------------------
  // Lifecycle / mobile
  // ---------------------------------------------------------------------

  it('checkMobile via onResize sets isMobile', () => {
    Object.defineProperty(window, 'innerWidth', { value: 500, writable: true, configurable: true });
    component.onResize();
    expect(component.isMobile).toBe(true);

    Object.defineProperty(window, 'innerWidth', { value: 2000, writable: true, configurable: true });
    component.onResize();
    expect(component.isMobile).toBe(false);
  });

  it('ngOnDestroy unsubscribes from active stream', () => {
    const subject = new Subject<StreamEvent>();
    apiStub.askStream.mockReturnValue(subject.asObservable());
    component.sendMessage('hi');
    expect(component['streamSub']).toBeTruthy();
    const spy = vi.spyOn(component['streamSub']!, 'unsubscribe');
    component.ngOnDestroy();
    expect(spy).toHaveBeenCalled();
  });

  it('ngAfterViewChecked scrolls the messages container', () => {
    component.messagesContainer = { nativeElement: { scrollTop: 0, scrollHeight: 500 } as HTMLDivElement } as never;
    component.ngAfterViewChecked();
    expect(component.messagesContainer.nativeElement.scrollTop).toBe(500);
  });

  it('ngAfterViewChecked is safe with no container', () => {
    (component.messagesContainer as never) = undefined as never;
    component.ngAfterViewChecked();
    // Should not throw
    expect(true).toBe(true);
  });
});
