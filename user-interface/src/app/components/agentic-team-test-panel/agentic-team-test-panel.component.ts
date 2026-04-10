import { Component, Input, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatIconModule } from '@angular/material/icon';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { AgentTestChatComponent } from '../agent-test-chat/agent-test-chat.component';
import { PipelineTestRunnerComponent } from '../pipeline-test-runner/pipeline-test-runner.component';
import type { AgenticTeam, AgentQualityScore } from '../../models';

@Component({
  selector: 'app-agentic-team-test-panel',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonToggleModule,
    MatIconModule,
    AgentTestChatComponent,
    PipelineTestRunnerComponent,
  ],
  templateUrl: './agentic-team-test-panel.component.html',
  styleUrl: './agentic-team-test-panel.component.scss',
})
export class AgenticTeamTestPanelComponent implements OnInit {
  @Input() team!: AgenticTeam;

  private readonly api = inject(AgenticTeamApiService);

  activeTab = signal<'chat' | 'pipeline'>('chat');
  qualityScores = signal<AgentQualityScore[]>([]);

  ngOnInit(): void {
    this.loadQualityScores();
  }

  loadQualityScores(): void {
    if (!this.team) return;
    this.api.getAgentQualityScores(this.team.team_id).subscribe({
      next: (scores) => this.qualityScores.set(scores),
    });
  }
}
