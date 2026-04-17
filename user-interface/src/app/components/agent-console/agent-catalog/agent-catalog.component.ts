import { Component, inject, OnInit, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { AgentCatalogApiService } from '../../../services/agent-catalog-api.service';
import type {
  AgentDetail,
  AgentSummary,
  TeamGroup,
} from '../../../models/agent-catalog.model';

/**
 * Browsable, searchable catalog of every specialist agent registered in the
 * backend. Drives the first tab of the Agent Console.
 */
@Component({
  selector: 'app-agent-catalog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatChipsModule,
    MatIconModule,
    MatButtonModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatSidenavModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
  ],
  templateUrl: './agent-catalog.component.html',
  styleUrl: './agent-catalog.component.scss',
})
export class AgentCatalogComponent implements OnInit {
  private readonly api = inject(AgentCatalogApiService);

  readonly agents = signal<AgentSummary[]>([]);
  readonly teams = signal<TeamGroup[]>([]);
  readonly loading = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  readonly selectedDetail = signal<AgentDetail | null>(null);
  readonly drawerOpen = signal<boolean>(false);
  readonly detailLoading = signal<boolean>(false);

  // Filter state.
  readonly query = signal<string>('');
  readonly selectedTeam = signal<string | null>(null);
  readonly selectedTag = signal<string | null>(null);

  readonly allTags = computed(() => {
    const set = new Set<string>();
    for (const a of this.agents()) a.tags.forEach((t) => set.add(t));
    return Array.from(set).sort();
  });

  ngOnInit(): void {
    this.refresh();
    this.api.listTeams().subscribe({
      next: (groups) => this.teams.set(groups),
      error: (err) => console.error('Failed to load teams', err),
    });
  }

  refresh(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api
      .listAgents({
        team: this.selectedTeam() ?? undefined,
        tag: this.selectedTag() ?? undefined,
        q: this.query().trim() || undefined,
      })
      .subscribe({
        next: (agents) => {
          this.agents.set(agents);
          this.loading.set(false);
        },
        error: (err) => {
          this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to load agents');
          this.loading.set(false);
        },
      });
  }

  onSearchChange(value: string): void {
    this.query.set(value);
    this.refresh();
  }

  onTeamChange(team: string | null): void {
    this.selectedTeam.set(team);
    this.refresh();
  }

  onTagToggle(tag: string): void {
    this.selectedTag.set(this.selectedTag() === tag ? null : tag);
    this.refresh();
  }

  clearFilters(): void {
    this.query.set('');
    this.selectedTeam.set(null);
    this.selectedTag.set(null);
    this.refresh();
  }

  openDetail(agent: AgentSummary): void {
    this.drawerOpen.set(true);
    this.detailLoading.set(true);
    this.selectedDetail.set(null);
    this.api.getAgent(agent.id).subscribe({
      next: (detail) => {
        this.selectedDetail.set(detail);
        this.detailLoading.set(false);
      },
      error: (err) => {
        console.error('Failed to load agent detail', err);
        this.detailLoading.set(false);
      },
    });
  }

  closeDetail(): void {
    this.drawerOpen.set(false);
    this.selectedDetail.set(null);
  }

  teamDisplayName(teamKey: string): string {
    const group = this.teams().find((g) => g.team === teamKey);
    return group?.display_name ?? teamKey;
  }

  trackAgent(_index: number, agent: AgentSummary): string {
    return agent.id;
  }

  trackTeam(_index: number, team: TeamGroup): string {
    return team.team;
  }
}
