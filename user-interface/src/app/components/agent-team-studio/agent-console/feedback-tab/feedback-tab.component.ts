import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { ProductDeliveryService } from '../../../../services/product-delivery.service';
import {
  LinkStoryDialogComponent,
  LinkStoryDialogData,
  LinkStoryDialogResult,
} from './link-story-dialog.component';
import type {
  FeedbackItem,
  Product,
  Story,
} from '../../../../models/product-delivery.model';

const STATUS_FILTERS: { label: string; value: string | null }[] = [
  { label: 'All', value: null },
  { label: 'Open', value: 'open' },
  { label: 'Closed', value: 'closed' },
];

@Component({
  selector: 'app-feedback-tab',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatChipsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatSelectModule,
  ],
  templateUrl: './feedback-tab.component.html',
  styleUrl: './feedback-tab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FeedbackTabComponent implements OnInit {
  private readonly api = inject(ProductDeliveryService);
  private readonly dialog = inject(MatDialog);

  readonly statusFilters = STATUS_FILTERS;

  readonly products = signal<Product[]>([]);
  readonly selectedProductId = signal<string | null>(null);
  readonly statusFilter = signal<string | null>('open');
  readonly items = signal<FeedbackItem[]>([]);
  readonly stories = signal<Story[]>([]);
  readonly loadingProducts = signal<boolean>(false);
  readonly loadingFeedback = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.refreshProducts();
  }

  refreshProducts(): void {
    this.loadingProducts.set(true);
    this.error.set(null);
    this.api.listProducts().subscribe({
      next: (rows) => {
        this.products.set(rows);
        this.loadingProducts.set(false);
        if (rows.length && this.selectedProductId() === null) {
          this.selectProduct(rows[0].id);
        }
      },
      error: (err) => {
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to load products.');
        this.loadingProducts.set(false);
      },
    });
  }

  selectProduct(productId: string): void {
    this.selectedProductId.set(productId);
    this.loadFeedback();
    this.loadStories(productId);
  }

  setStatusFilter(value: string | null): void {
    this.statusFilter.set(value);
    this.loadFeedback();
  }

  loadFeedback(): void {
    const productId = this.selectedProductId();
    if (!productId) return;
    this.loadingFeedback.set(true);
    this.error.set(null);
    this.api.listFeedback(productId, this.statusFilter()).subscribe({
      next: (rows) => {
        this.items.set(rows);
        this.loadingFeedback.set(false);
      },
      error: (err) => {
        this.items.set([]);
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to load feedback.');
        this.loadingFeedback.set(false);
      },
    });
  }

  loadStories(productId: string): void {
    this.api.getBacklog(productId).subscribe({
      next: (tree) => {
        this.stories.set(ProductDeliveryService.flattenStories(tree));
      },
      error: () => {
        // Linking is the only consumer; surface the empty list silently
        // and let the feedback-list-side error explain failure.
        this.stories.set([]);
      },
    });
  }

  storyTitle(storyId: string | null): string {
    if (storyId === null) return '—';
    return this.stories().find((s) => s.id === storyId)?.title ?? storyId;
  }

  /** Truncated, one-line summary of `raw_payload` for the row. */
  payloadSnippet(item: FeedbackItem): string {
    if (!item.raw_payload) return '';
    const json = JSON.stringify(item.raw_payload);
    return json.length > 120 ? `${json.slice(0, 119)}…` : json;
  }

  openLinkDialog(item: FeedbackItem): void {
    const data: LinkStoryDialogData = {
      feedbackId: item.id,
      stories: this.stories(),
      currentStoryId: item.linked_story_id,
    };
    const ref = this.dialog.open<
      LinkStoryDialogComponent,
      LinkStoryDialogData,
      LinkStoryDialogResult
    >(LinkStoryDialogComponent, { data });
    ref.afterClosed().subscribe((res) => {
      if (!res) return;
      this.applyLink(item, res.storyId);
    });
  }

  /** Public for unit tests; called by `openLinkDialog` after Apply. */
  applyLink(item: FeedbackItem, storyId: string | null): void {
    const previous = item.linked_story_id;
    // Optimistic update.
    this.items.update((rows) =>
      rows.map((row) => (row.id === item.id ? { ...row, linked_story_id: storyId } : row)),
    );
    this.api.linkFeedback(item.id, storyId).subscribe({
      next: (updated) => {
        this.items.update((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
      },
      error: (err) => {
        // Roll back to the previous link, surface the detail message.
        this.items.update((rows) =>
          rows.map((row) =>
            row.id === item.id ? { ...row, linked_story_id: previous } : row,
          ),
        );
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to link feedback.');
      },
    });
  }
}
