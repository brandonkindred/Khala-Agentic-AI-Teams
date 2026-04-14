import {
  Component,
  Input,
  Output,
  EventEmitter,
  OnInit,
  OnChanges,
  SimpleChanges,
  ViewChild,
  ElementRef,
  AfterViewChecked,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import type {
  AgenticTeam,
  AgenticTeamAgent,
  AgentQualityScore,
  TestChatSession,
  TestChatMessage,
} from '../../models';

@Component({
  selector: 'app-agent-test-chat',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatSelectModule,
    MatTooltipModule,
  ],
  templateUrl: './agent-test-chat.component.html',
  styleUrl: './agent-test-chat.component.scss',
})
export class AgentTestChatComponent implements OnInit, OnChanges, AfterViewChecked {
  @Input() team!: AgenticTeam;
  @Input() qualityScores: AgentQualityScore[] = [];
  @Output() scoresChanged = new EventEmitter<void>();

  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(AgenticTeamApiService);
  private readonly fb = inject(FormBuilder);

  selectedAgent = signal<AgenticTeamAgent | null>(null);
  sessions = signal<TestChatSession[]>([]);
  activeSession = signal<TestChatSession | null>(null);
  messages = signal<TestChatMessage[]>([]);
  suggestedPrompts = signal<string[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  renamingSessionId = signal<string | null>(null);
  private shouldAutoScroll = false;

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  renameForm = this.fb.nonNullable.group({
    name: ['', [Validators.required, Validators.minLength(1)]],
  });

  ngOnInit(): void {
    if (this.team.agents.length > 0) {
      this.selectAgent(this.team.agents[0]);
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['team'] && !changes['team'].firstChange) {
      if (this.team.agents.length > 0) {
        this.selectAgent(this.team.agents[0]);
      }
    }
  }

  ngAfterViewChecked(): void {
    if (this.shouldAutoScroll) {
      this.scrollToBottom();
      this.shouldAutoScroll = false;
    }
  }

  private scrollToBottom(): void {
    if (this.messagesContainer?.nativeElement) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  selectAgent(agent: AgenticTeamAgent): void {
    this.selectedAgent.set(agent);
    this.activeSession.set(null);
    this.messages.set([]);
    this.suggestedPrompts.set([]);
    this.loadSessions(agent.agent_name);
  }

  private loadSessions(agentName: string): void {
    this.api.listTestChatSessions(this.team.team_id, agentName).subscribe({
      next: (sessions) => {
        this.sessions.set(sessions);
        if (sessions.length > 0) {
          this.openSession(sessions[0].session_id);
        }
      },
    });
  }

  openSession(sessionId: string): void {
    this.api.getTestChatSession(this.team.team_id, sessionId).subscribe({
      next: (detail) => {
        this.activeSession.set(detail.session);
        this.messages.set(detail.messages);
        this.shouldAutoScroll = true;
        this.suggestedPrompts.set(detail.suggested_prompts);
      },
    });
  }

  createSession(): void {
    const agent = this.selectedAgent();
    if (!agent) return;
    this.api.createTestChatSession(this.team.team_id, agent.agent_name).subscribe({
      next: (session) => {
        this.sessions.update((s) => [session, ...s]);
        this.openSession(session.session_id);
      },
    });
  }

  deleteSession(sessionId: string): void {
    this.api.deleteTestChatSession(this.team.team_id, sessionId).subscribe({
      next: () => {
        this.sessions.update((s) => s.filter((ss) => ss.session_id !== sessionId));
        if (this.activeSession()?.session_id === sessionId) {
          this.activeSession.set(null);
          this.messages.set([]);
          this.suggestedPrompts.set([]);
        }
      },
    });
  }

  startRename(session: TestChatSession): void {
    this.renamingSessionId.set(session.session_id);
    this.renameForm.reset({ name: session.session_name || '' });
  }

  confirmRename(sessionId: string): void {
    const name = this.renameForm.getRawValue().name.trim();
    if (!name) return;
    this.api.renameTestChatSession(this.team.team_id, sessionId, name).subscribe({
      next: () => {
        this.sessions.update((s) =>
          s.map((ss) => (ss.session_id === sessionId ? { ...ss, session_name: name } : ss)),
        );
        this.renamingSessionId.set(null);
      },
    });
  }

  cancelRename(): void {
    this.renamingSessionId.set(null);
  }

  exportSession(): void {
    const session = this.activeSession();
    if (!session) return;
    this.api.exportTestChatSession(this.team.team_id, session.session_id).subscribe({
      next: (blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${session.session_name || session.agent_name}-chat.md`;
        a.click();
        URL.revokeObjectURL(url);
      },
    });
  }

  onSubmit(): void {
    if (this.form.invalid || this.loading()) return;
    const message = this.form.getRawValue().message.trim();
    if (!message) return;
    this.sendMessage(message);
  }

  onSuggestedPrompt(prompt: string): void {
    this.sendMessage(prompt);
  }

  private sendMessage(content: string): void {
    const session = this.activeSession();
    if (!session) {
      // Auto-create session on first message
      const agent = this.selectedAgent();
      if (!agent) return;
      this.api.createTestChatSession(this.team.team_id, agent.agent_name).subscribe({
        next: (newSession) => {
          this.sessions.update((s) => [newSession, ...s]);
          this.activeSession.set(newSession);
          this._sendToSession(newSession.session_id, content);
        },
      });
      return;
    }
    this._sendToSession(session.session_id, content);
  }

  private _sendToSession(sessionId: string, content: string): void {
    this.form.reset({ message: '' });
    this.messages.update((msgs) => [
      ...msgs,
      {
        message_id: `temp-${Date.now()}`,
        session_id: sessionId,
        role: 'user' as const,
        content,
        rating: null,
        created_at: new Date().toISOString(),
      },
    ]);
    this.shouldAutoScroll = true;
    this.loading.set(true);
    this.error.set(null);
    this.suggestedPrompts.set([]);

    this.api.sendTestChatMessage(this.team.team_id, sessionId, content).subscribe({
      next: (res) => {
        this.messages.set(res.messages);
        this.shouldAutoScroll = true;
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err?.error?.detail ?? 'Failed to send message');
        this.loading.set(false);
      },
    });
  }

  rateMessage(messageId: string, rating: 'thumbs_up' | 'thumbs_down'): void {
    this.api.rateTestChatMessage(this.team.team_id, messageId, rating).subscribe({
      next: () => {
        this.messages.update((msgs) =>
          msgs.map((m) => (m.message_id === messageId ? { ...m, rating } : m)),
        );
        this.scoresChanged.emit();
      },
    });
  }

  getScoreForAgent(agentName: string): AgentQualityScore | undefined {
    return this.qualityScores.find((s) => s.agent_name === agentName);
  }

  scoreColor(score: AgentQualityScore | undefined): string {
    if (!score || score.total_rated === 0) return 'neutral';
    if (score.score_pct > 70) return 'good';
    if (score.score_pct >= 40) return 'ok';
    return 'poor';
  }

  formatTime(timestamp: string): string {
    if (!timestamp) return '';
    try {
      return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }
}
