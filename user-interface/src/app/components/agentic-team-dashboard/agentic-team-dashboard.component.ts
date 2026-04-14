import { Component, inject, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatChipsModule } from '@angular/material/chips';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import { ProcessDesignerChatComponent } from '../process-designer-chat/process-designer-chat.component';
import { AgenticTeamTestPanelComponent } from '../agentic-team-test-panel/agentic-team-test-panel.component';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import type { AgenticTeamSummary, AgenticTeam, TeamMode } from '../../models';

@Component({
  selector: 'app-agentic-team-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatListModule,
    MatChipsModule,
    MatButtonToggleModule,
    ProcessDesignerChatComponent,
    AgenticTeamTestPanelComponent,
    DashboardShellComponent,
  ],
  templateUrl: './agentic-team-dashboard.component.html',
  styleUrl: './agentic-team-dashboard.component.scss',
})
export class AgenticTeamDashboardComponent implements OnInit {
  private readonly api = inject(AgenticTeamApiService);
  private readonly fb = inject(FormBuilder);

  teams = signal<AgenticTeamSummary[]>([]);
  selectedTeam = signal<AgenticTeam | null>(null);
  viewMode = signal<TeamMode>('development');
  showCreateForm = signal(false);
  creating = signal(false);
  error = signal<string | null>(null);

  form = this.fb.nonNullable.group({
    name: ['', [Validators.required, Validators.minLength(1), Validators.maxLength(200)]],
    description: ['', [Validators.maxLength(1000)]],
  });

  ngOnInit(): void {
    this.loadTeams();
  }

  loadTeams(): void {
    this.api.listTeams().subscribe({
      next: (teams) => this.teams.set(teams),
      error: (err) => this.error.set(err?.error?.detail ?? 'Failed to load teams'),
    });
  }

  toggleCreateForm(): void {
    this.showCreateForm.update((v) => !v);
    if (!this.showCreateForm()) {
      this.form.reset({ name: '', description: '' });
    }
  }

  onCreateTeam(): void {
    if (this.form.invalid || this.creating()) return;
    this.creating.set(true);
    this.error.set(null);

    const { name, description } = this.form.getRawValue();
    this.api.createTeam({ name, description }).subscribe({
      next: () => {
        this.creating.set(false);
        this.showCreateForm.set(false);
        this.form.reset({ name: '', description: '' });
        this.loadTeams();
      },
      error: (err) => {
        this.creating.set(false);
        this.error.set(err?.error?.detail ?? 'Failed to create team');
      },
    });
  }

  selectTeam(teamId: string): void {
    this.api.getTeam(teamId).subscribe({
      next: (res) => {
        this.selectedTeam.set(res.team);
        this.viewMode.set(res.team.mode ?? 'development');
      },
      error: (err) => this.error.set(err?.error?.detail ?? 'Failed to load team'),
    });
  }

  onModeChange(newMode: TeamMode): void {
    const team = this.selectedTeam();
    if (!team) return;
    this.viewMode.set(newMode);
    this.api.setTeamMode(team.team_id, newMode).subscribe({
      error: (err) => this.error.set(err?.error?.detail ?? 'Failed to change mode'),
    });
  }

  backToList(): void {
    this.selectedTeam.set(null);
    this.loadTeams();
  }
}
