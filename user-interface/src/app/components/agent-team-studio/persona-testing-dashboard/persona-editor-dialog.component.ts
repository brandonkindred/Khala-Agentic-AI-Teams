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
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import type { PersonaInfo } from '../../../models';

/** Data handed to the dialog. */
export interface PersonaEditorDialogData {
  mode: 'create' | 'edit';
  /** Required in edit mode; ignored in create mode. */
  persona?: PersonaInfo;
}

/** Result returned when the user clicks Save. */
export interface PersonaEditorDialogResult {
  name: string;
  description: string;
  icon: string;
  system_prompt: string;
  spec_generation_prompt: string;
}

@Component({
  selector: 'app-persona-editor-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
  ],
  templateUrl: './persona-editor-dialog.component.html',
  styleUrl: './persona-editor-dialog.component.scss',
})
export class PersonaEditorDialogComponent {
  readonly data = inject<PersonaEditorDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<
    MatDialogRef<PersonaEditorDialogComponent, PersonaEditorDialogResult>
  >(MatDialogRef);

  readonly name = signal<string>('');
  readonly description = signal<string>('');
  readonly icon = signal<string>('person');
  readonly systemPrompt = signal<string>('');
  readonly specGenerationPrompt = signal<string>('');
  readonly serverError = signal<string | null>(null);
  readonly busy = signal<boolean>(false);

  constructor() {
    if (this.data.mode === 'edit' && this.data.persona) {
      const p = this.data.persona;
      this.name.set(p.name);
      this.description.set(p.description);
      this.icon.set(p.icon || 'person');
      this.systemPrompt.set(p.system_prompt);
      this.specGenerationPrompt.set(p.spec_generation_prompt);
    }
  }

  isValid(): boolean {
    return (
      this.name().trim().length > 0 &&
      this.description().trim().length > 0 &&
      this.icon().trim().length > 0 &&
      this.systemPrompt().trim().length > 0 &&
      this.specGenerationPrompt().trim().length > 0
    );
  }

  submit(): void {
    if (!this.isValid()) {
      this.serverError.set('All fields are required.');
      return;
    }
    this.busy.set(true);
    this.ref.close({
      name: this.name().trim(),
      description: this.description().trim(),
      icon: this.icon().trim(),
      system_prompt: this.systemPrompt(),
      spec_generation_prompt: this.specGenerationPrompt(),
    });
  }

  /**
   * Surface a server-side error without closing the dialog. Caller can keep
   * the dialog open by *not* closing it on error and invoking this on the
   * component instance.
   */
  setServerError(message: string): void {
    this.serverError.set(message);
    this.busy.set(false);
  }

  cancel(): void {
    this.ref.close();
  }
}
