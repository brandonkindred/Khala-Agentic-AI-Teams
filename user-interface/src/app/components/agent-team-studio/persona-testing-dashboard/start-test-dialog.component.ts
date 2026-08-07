import { Component, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  MatDialogModule,
  MatDialogRef,
  MAT_DIALOG_DATA,
} from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import type { PersonaInfo, TestableTeam } from '../../../models';

export interface StartTestDialogData {
  personas: PersonaInfo[];
  teams: TestableTeam[];
  /** Pre-selected persona id (e.g. clicked from a card). */
  initialPersonaId?: string;
}

export interface StartTestDialogResult {
  persona_id: string;
  target_team_key: string;
  project_name?: string;
}

@Component({
  selector: 'app-start-test-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
  ],
  templateUrl: './start-test-dialog.component.html',
  styleUrl: './start-test-dialog.component.scss',
})
export class StartTestDialogComponent {
  readonly data = inject<StartTestDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<
    MatDialogRef<StartTestDialogComponent, StartTestDialogResult>
  >(MatDialogRef);

  readonly personaId = signal<string>('');
  readonly targetTeamKey = signal<string>('');
  readonly projectName = signal<string>('');
  readonly serverError = signal<string | null>(null);
  readonly busy = signal<boolean>(false);

  constructor() {
    // Pre-select if a persona was passed in, otherwise default to first.
    if (this.data.initialPersonaId) {
      this.personaId.set(this.data.initialPersonaId);
    } else if (this.data.personas.length > 0) {
      this.personaId.set(this.data.personas[0].id);
    }
    if (this.data.teams.length === 1) {
      this.targetTeamKey.set(this.data.teams[0].team_key);
    }
  }

  isValid(): boolean {
    return this.personaId().length > 0 && this.targetTeamKey().length > 0;
  }

  submit(): void {
    if (!this.isValid()) {
      return;
    }
    this.busy.set(true);
    const project = this.projectName().trim();
    this.ref.close({
      persona_id: this.personaId(),
      target_team_key: this.targetTeamKey(),
      project_name: project ? project : undefined,
    });
  }

  setServerError(message: string): void {
    this.serverError.set(message);
    this.busy.set(false);
  }

  cancel(): void {
    this.ref.close();
  }
}
