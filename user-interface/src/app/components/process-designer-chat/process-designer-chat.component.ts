import {
  Component,
  Input,
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
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import type {
  AgenticTeam,
  AgenticTeamAgent,
  AgenticConversationMessage,
  ProcessDefinition,
  RosterValidationResult,
} from '../../models';

@Component({
  selector: 'app-process-designer-chat',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
    MatTooltipModule,
  ],
  templateUrl: './process-designer-chat.component.html',
  styleUrl: './process-designer-chat.component.scss',
})
export class ProcessDesignerChatComponent implements OnInit, OnChanges, AfterViewChecked {
  @Input() team!: AgenticTeam;

  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(AgenticTeamApiService);
  private readonly fb = inject(FormBuilder);
  private readonly sanitizer = inject(DomSanitizer);

  messages = signal<AgenticConversationMessage[]>([]);
  currentProcess = signal<ProcessDefinition | null>(null);
  suggestedQuestions = signal<string[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  flowchartSvg = signal<SafeHtml | null>(null);

  rosterAgents = signal<AgenticTeamAgent[]>([]);
  rosterValidation = signal<RosterValidationResult | null>(null);
  rosterLoading = signal(false);
  expandedAgent = signal<string | null>(null);

  private conversationId: string | null = null;

  form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  ngOnInit(): void {
    this.startConversation();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['team'] && !changes['team'].firstChange) {
      this.startConversation();
    }
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  private scrollToBottom(): void {
    if (this.messagesContainer?.nativeElement) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }

  private startConversation(): void {
    this.error.set(null);
    this.conversationId = null;
    this.messages.set([]);
    this.currentProcess.set(null);
    this.suggestedQuestions.set([]);

    this.api.createConversation(this.team.team_id).subscribe({
      next: (res) => this.applyState(res),
      error: (err) => this.error.set(err?.error?.detail ?? 'Failed to start conversation'),
    });
  }

  private applyState(res: {
    conversation_id: string;
    messages: AgenticConversationMessage[];
    current_process: ProcessDefinition | null;
    suggested_questions: string[];
  }): void {
    this.conversationId = res.conversation_id;
    this.messages.set(res.messages);
    this.currentProcess.set(res.current_process);
    this.suggestedQuestions.set(res.suggested_questions);
    this.buildFlowchart(res.current_process);
    this.refreshRoster();
  }

  refreshRoster(): void {
    this.rosterLoading.set(true);
    this.api.listTeamAgents(this.team.team_id).subscribe({
      next: (agents) => {
        this.rosterAgents.set(agents);
        this.rosterLoading.set(false);
        this.api.validateRoster(this.team.team_id).subscribe({
          next: (result) => this.rosterValidation.set(result),
          error: () => this.rosterValidation.set(null),
        });
      },
      error: () => this.rosterLoading.set(false),
    });
  }

  toggleAgentExpand(agentName: string): void {
    this.expandedAgent.update((current) => (current === agentName ? null : agentName));
  }

  gapCountForAgent(agentName: string): number {
    const v = this.rosterValidation();
    if (!v) return 0;
    return v.gaps.filter((g) => g.agent_name === agentName).length;
  }

  onSubmit(): void {
    if (this.form.invalid || this.loading()) return;
    const message = this.form.getRawValue().message.trim();
    if (!message) return;
    this.sendMessage(message);
  }

  onSuggestedQuestion(q: string): void {
    this.sendMessage(q);
  }

  private sendMessage(message: string): void {
    if (!this.conversationId) return;

    this.form.reset({ message: '' });
    this.messages.update((msgs) => [
      ...msgs,
      { role: 'user' as const, content: message, timestamp: new Date().toISOString() },
    ]);
    this.loading.set(true);
    this.error.set(null);

    this.api.sendMessage(this.conversationId, message).subscribe({
      next: (res) => {
        this.applyState(res);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(err?.error?.detail ?? 'Failed to send message');
        this.loading.set(false);
      },
    });
  }

  newConversation(): void {
    this.startConversation();
  }

  formatTime(timestamp: string): string {
    if (!timestamp) return '';
    try {
      return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }

  /**
   * Build a Mermaid-style flowchart as inline SVG from the process definition.
   * We generate the SVG directly to avoid requiring an external Mermaid runtime.
   */
  private buildFlowchart(process: ProcessDefinition | null): void {
    if (!process || process.steps.length === 0) {
      this.flowchartSvg.set(null);
      return;
    }

    const steps = process.steps;
    const nodeSpacingY = 100;
    const nodeWidth = 200;
    const nodeHeight = 50;
    const padding = 40;
    const svgWidth = nodeWidth + padding * 2;

    // Build a map from step_id to index for layout
    const idxMap = new Map<string, number>();
    steps.forEach((s, i) => idxMap.set(s.step_id, i));

    // Layout: one trigger node at top, then steps vertically, then output at bottom
    const totalNodes = steps.length + 2; // trigger + steps + output
    const svgHeight = totalNodes * nodeSpacingY + padding * 2;

    const cx = svgWidth / 2;
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${svgWidth} ${svgHeight}" width="100%" height="100%">`;
    svg += `<defs>
      <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
        <polygon points="0 0, 10 3.5, 0 7" fill="#58a6ff"/>
      </marker>
    </defs>`;

    // Helper: node Y position
    const nodeY = (idx: number) => padding + idx * nodeSpacingY;

    // Draw trigger node (rounded rect, green tint)
    const trigY = nodeY(0);
    svg += `<rect x="${cx - nodeWidth / 2}" y="${trigY}" width="${nodeWidth}" height="${nodeHeight}" rx="25" ry="25" fill="#1a3a2a" stroke="#3fb950" stroke-width="1.5"/>`;
    svg += `<text x="${cx}" y="${trigY + nodeHeight / 2 + 5}" text-anchor="middle" fill="#3fb950" font-size="12" font-family="sans-serif">${this.escSvg(process.trigger.trigger_type.toUpperCase())}: ${this.truncate(process.trigger.description, 20)}</text>`;

    // Arrow from trigger to first step
    if (steps.length > 0) {
      svg += this.arrow(cx, trigY + nodeHeight, cx, nodeY(1));
    }

    // Draw step nodes
    steps.forEach((step, i) => {
      const y = nodeY(i + 1);
      const isDecision = step.step_type === 'decision';

      if (isDecision) {
        // Diamond shape
        const hw = nodeWidth / 2;
        const hh = nodeHeight / 2;
        const dmx = cx;
        const dmy = y + hh;
        svg += `<polygon points="${dmx},${y} ${dmx + hw},${dmy} ${dmx},${y + nodeHeight} ${dmx - hw},${dmy}" fill="#2d1b3d" stroke="#bc8cff" stroke-width="1.5"/>`;
        svg += `<text x="${cx}" y="${dmy + 4}" text-anchor="middle" fill="#bc8cff" font-size="11" font-family="sans-serif">${this.escSvg(this.truncate(step.name, 22))}</text>`;
      } else {
        svg += `<rect x="${cx - nodeWidth / 2}" y="${y}" width="${nodeWidth}" height="${nodeHeight}" rx="8" ry="8" fill="#161b22" stroke="#58a6ff" stroke-width="1.5"/>`;
        svg += `<text x="${cx}" y="${y + 20}" text-anchor="middle" fill="#f0f6fc" font-size="12" font-family="sans-serif">${this.escSvg(this.truncate(step.name, 22))}</text>`;

        // Show agent names below step name
        if (step.agents.length > 0) {
          const agentLabel = step.agents.map((a) => a.agent_name).join(', ');
          svg += `<text x="${cx}" y="${y + 36}" text-anchor="middle" fill="#8b949e" font-size="10" font-family="sans-serif">${this.escSvg(this.truncate(agentLabel, 28))}</text>`;
        }
      }

      // Arrows to next steps (simplified: straight down to next sequential node)
      for (const nextId of step.next_steps) {
        const nextIdx = idxMap.get(nextId);
        if (nextIdx !== undefined) {
          svg += this.arrow(cx, y + nodeHeight, cx, nodeY(nextIdx + 1));
        }
      }

      // If no explicit next and it's the last step, arrow to output
      if (step.next_steps.length === 0 && i === steps.length - 1) {
        svg += this.arrow(cx, y + nodeHeight, cx, nodeY(steps.length + 1));
      }
    });

    // Draw output node (rounded rect, orange tint)
    const outY = nodeY(steps.length + 1);
    svg += `<rect x="${cx - nodeWidth / 2}" y="${outY}" width="${nodeWidth}" height="${nodeHeight}" rx="25" ry="25" fill="#3d2b1a" stroke="#d29922" stroke-width="1.5"/>`;
    svg += `<text x="${cx}" y="${outY + nodeHeight / 2 + 5}" text-anchor="middle" fill="#d29922" font-size="12" font-family="sans-serif">${this.escSvg(this.truncate(process.output.description || 'Output', 22))}</text>`;

    svg += '</svg>';
    this.flowchartSvg.set(this.sanitizer.bypassSecurityTrustHtml(svg));
  }

  private arrow(x1: number, y1: number, x2: number, y2: number): string {
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2 - 4}" stroke="#58a6ff" stroke-width="1.5" marker-end="url(#arrowhead)"/>`;
  }

  private truncate(text: string, max: number): string {
    return text.length > max ? text.substring(0, max - 1) + '\u2026' : text;
  }

  private escSvg(text: string): string {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
}
