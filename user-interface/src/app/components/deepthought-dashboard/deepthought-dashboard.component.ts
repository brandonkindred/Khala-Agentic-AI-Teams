import {
  Component,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  OnDestroy,
  inject,
  HostListener,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule, FormControl, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatSliderModule } from '@angular/material/slider';
import { MatSelectModule } from '@angular/material/select';
import { Subscription } from 'rxjs';
import { marked } from 'marked';

import { DeepthoughtApiService, StreamEvent } from '../../services/deepthought-api.service';
import type {
  AgentResult,
  ChatMessage,
  DecompositionStrategy,
  KnowledgeEntry,
  LiveAgentNode,
  AgentNodeStatus,
} from '../../models/deepthought.model';
import { DECOMPOSITION_STRATEGIES } from '../../models/deepthought.model';

@Component({
  selector: 'app-deepthought-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatTooltipModule,
    MatSliderModule,
    MatSelectModule,
  ],
  templateUrl: './deepthought-dashboard.component.html',
  styleUrl: './deepthought-dashboard.component.scss',
})
export class DeepthoughtDashboardComponent implements AfterViewChecked, OnDestroy {
  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(DeepthoughtApiService);
  private streamSub: Subscription | null = null;

  // Chat state
  messages: ChatMessage[] = [];
  conversationHistory: { role: string; content: string }[] = [];
  messageControl = new FormControl('', [Validators.required, Validators.minLength(1)]);
  isProcessing = false;
  error: string | null = null;
  lastFailedMessage: string | null = null;

  // Agent tree state
  liveAgentNodes = new Map<string, LiveAgentNode>();
  activeAgentCount = 0;
  selectedTreeSnapshot: AgentResult | null = null;
  selectedKnowledge: KnowledgeEntry[] = [];
  expandedNodes = new Set<string>();
  showKnowledge = false;

  // Settings
  showSettings = false;
  maxDepth = 3;
  decompositionStrategy: DecompositionStrategy = 'auto';
  strategies = DECOMPOSITION_STRATEGIES;

  // Mobile
  isMobile = false;
  mobileTab: 'chat' | 'tree' = 'chat';

  // Welcome state
  readonly exampleQuestions = [
    'What are the economic implications of universal basic income?',
    'Compare microservices vs monolith architectures for a startup',
    'Should a mid-size company adopt AI for customer support?',
    'What is the future of quantum computing in cryptography?',
  ];

  constructor() {
    this.checkMobile();
  }

  @HostListener('window:resize')
  onResize(): void {
    this.checkMobile();
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  ngOnDestroy(): void {
    this.streamSub?.unsubscribe();
  }

  // --- Actions ---

  onSubmit(): void {
    const msg = this.messageControl.value?.trim();
    if (!msg || this.isProcessing) return;
    this.sendMessage(msg);
  }

  sendMessage(message: string): void {
    this.messageControl.reset('');
    this.error = null;
    this.lastFailedMessage = null;

    // Push user message
    this.messages = [
      ...this.messages,
      { role: 'user', content: message, timestamp: new Date().toISOString() },
    ];
    this.conversationHistory = [
      ...this.conversationHistory,
      { role: 'user', content: message },
    ];

    // Reset live tree
    this.liveAgentNodes = new Map();
    this.activeAgentCount = 0;
    this.isProcessing = true;

    // Start SSE stream
    this.streamSub?.unsubscribe();
    this.streamSub = this.api
      .askStream({
        message,
        max_depth: this.maxDepth,
        conversation_history: this.conversationHistory,
        decomposition_strategy: this.decompositionStrategy,
      })
      .subscribe({
        next: (event: StreamEvent) => this.handleStreamEvent(event),
        error: () => {
          this.error = 'Connection lost. Please try again.';
          this.lastFailedMessage = message;
          this.isProcessing = false;
        },
      });
  }

  retryLastMessage(): void {
    if (this.lastFailedMessage) {
      // Remove the failed user message from history to avoid duplicates
      this.messages = this.messages.slice(0, -1);
      this.conversationHistory = this.conversationHistory.slice(0, -1);
      this.sendMessage(this.lastFailedMessage);
    }
  }

  toggleSettings(): void {
    this.showSettings = !this.showSettings;
  }

  newConversation(): void {
    this.streamSub?.unsubscribe();
    this.messages = [];
    this.conversationHistory = [];
    this.liveAgentNodes = new Map();
    this.activeAgentCount = 0;
    this.selectedTreeSnapshot = null;
    this.selectedKnowledge = [];
    this.expandedNodes = new Set();
    this.isProcessing = false;
    this.error = null;
    this.lastFailedMessage = null;
    this.showKnowledge = false;
  }

  selectTree(tree: AgentResult): void {
    this.selectedTreeSnapshot = tree;
    // Find matching knowledge entries from the message
    const msg = this.messages.find((m) => m.agentTree === tree);
    this.selectedKnowledge = [];
    // Collect knowledge from the response events if stored, otherwise empty
    if (msg) {
      // Knowledge entries are stored at the response level, not on individual messages.
      // We'll look for the matching response in our history.
      const responseMsg = this.messages.find(
        (m) => m.agentTree === tree && m.role === 'assistant'
      );
      if (responseMsg && (responseMsg as ChatMessage & { knowledge?: KnowledgeEntry[] }).knowledge) {
        this.selectedKnowledge =
          (responseMsg as ChatMessage & { knowledge?: KnowledgeEntry[] }).knowledge ?? [];
      }
    }
    this.expandedNodes = new Set();
    // Auto-expand root
    this.expandedNodes.add(tree.agent_id);

    if (this.isMobile) {
      this.mobileTab = 'tree';
    }
  }

  toggleNode(agentId: string): void {
    if (this.expandedNodes.has(agentId)) {
      this.expandedNodes.delete(agentId);
    } else {
      this.expandedNodes.add(agentId);
    }
  }

  // --- Helpers ---

  get liveAgentNodesArray(): LiveAgentNode[] {
    return Array.from(this.liveAgentNodes.values());
  }

  formatAgentName(name: string): string {
    return name
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  formatTime(timestamp: string): string {
    if (!timestamp) return '';
    try {
      return new Date(timestamp).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return '';
    }
  }

  renderMarkdown(content: string): string {
    if (!content) return '';
    return marked.parse(content, { async: false }) as string;
  }

  countAgents(node: AgentResult): number {
    let count = 1;
    for (const child of node.child_results ?? []) {
      count += this.countAgents(child);
    }
    return count;
  }

  getMaxDepth(node: AgentResult): number {
    if (!node.child_results?.length) return node.depth;
    return Math.max(...node.child_results.map((c) => this.getMaxDepth(c)));
  }

  // --- Private ---

  private handleStreamEvent(event: StreamEvent): void {
    switch (event.type) {
      case 'agent_event': {
        const e = event.payload;
        const status = this.eventTypeToStatus(e.event_type);
        this.liveAgentNodes.set(e.agent_id, {
          agent_id: e.agent_id,
          agent_name: e.agent_name,
          depth: e.depth,
          status,
          detail: e.detail,
        });
        // Recount active agents
        this.activeAgentCount = this.liveAgentNodes.size;
        break;
      }
      case 'result': {
        const resp = event.payload;
        const assistantMsg: ChatMessage & { knowledge?: KnowledgeEntry[] } = {
          role: 'assistant',
          content: resp.answer,
          timestamp: new Date().toISOString(),
          agentTree: resp.agent_tree,
          totalAgents: resp.total_agents_spawned,
          knowledge: resp.knowledge_entries,
        };
        this.messages = [...this.messages, assistantMsg];
        this.conversationHistory = [
          ...this.conversationHistory,
          { role: 'assistant', content: resp.answer },
        ];
        this.selectedTreeSnapshot = resp.agent_tree;
        this.selectedKnowledge = resp.knowledge_entries ?? [];
        this.expandedNodes = new Set([resp.agent_tree.agent_id]);
        break;
      }
      case 'error':
        this.error = event.payload;
        this.lastFailedMessage = this.messages.at(-1)?.role === 'user'
          ? this.messages.at(-1)!.content
          : null;
        this.isProcessing = false;
        break;
      case 'done':
        this.isProcessing = false;
        this.liveAgentNodes = new Map();
        break;
    }
  }

  private eventTypeToStatus(eventType: string): AgentNodeStatus {
    const map: Record<string, AgentNodeStatus> = {
      agent_spawned: 'spawned',
      agent_analysing: 'analysing',
      agent_answering: 'answering',
      agent_decomposing: 'decomposing',
      agent_deliberating: 'deliberating',
      agent_synthesising: 'synthesising',
      agent_complete: 'complete',
      budget_warning: 'budget_warning',
      knowledge_reused: 'knowledge_reused',
    };
    return map[eventType] ?? 'spawned';
  }

  private scrollToBottom(): void {
    if (this.messagesContainer?.nativeElement) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  private checkMobile(): void {
    this.isMobile = window.innerWidth <= 1024;
  }
}
