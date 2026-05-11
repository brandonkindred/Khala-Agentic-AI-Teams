import {
  ChangeDetectionStrategy,
  Component,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import {
  MAT_DIALOG_DATA,
  MatDialogModule,
  MatDialogRef,
} from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import type { Story } from '../../../models/product-delivery.model';

export interface LinkStoryDialogData {
  feedbackId: string;
  /** Pre-flattened stories belonging to the feedback row's product. */
  stories: Story[];
  /** Current link (used to populate the picker). `null` = unlinked. */
  currentStoryId: string | null;
}

export interface LinkStoryDialogResult {
  /** New target. `null` clears the link; `undefined` cancels. */
  storyId: string | null;
}

@Component({
  selector: 'app-link-story-dialog',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatSelectModule,
  ],
  templateUrl: './link-story-dialog.component.html',
  styleUrl: './link-story-dialog.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LinkStoryDialogComponent {
  readonly data = inject<LinkStoryDialogData>(MAT_DIALOG_DATA);
  readonly ref = inject<MatDialogRef<LinkStoryDialogComponent, LinkStoryDialogResult>>(MatDialogRef);

  readonly selected = signal<string | null>(this.data.currentStoryId);

  apply(): void {
    this.ref.close({ storyId: this.selected() });
  }

  unlink(): void {
    this.ref.close({ storyId: null });
  }

  cancel(): void {
    this.ref.close();
  }
}
