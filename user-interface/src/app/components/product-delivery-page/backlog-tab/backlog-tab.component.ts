import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
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
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { ProductDeliveryService } from '../../../services/product-delivery.service';
import {
  GroomModalComponent,
  GroomModalData,
  GroomModalResult,
} from '../groom-modal/groom-modal.component';
import type {
  BacklogTree,
  Product,
  Story,
  StoryNode,
} from '../../../models/product-delivery.model';
import { extractErrorDetail } from '../../../shared/extract-error-detail';

/**
 * Backlog tab — product picker + nested Initiative → Epic → Story tree
 * with inline status / WSJF / RICE chips and a drawer for editing
 * story status + scores.
 */
@Component({
  selector: 'app-backlog-tab',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatChipsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressSpinnerModule,
    MatSelectModule,
  ],
  templateUrl: './backlog-tab.component.html',
  styleUrl: './backlog-tab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BacklogTabComponent implements OnInit {
  private readonly api = inject(ProductDeliveryService);
  private readonly dialog = inject(MatDialog);

  readonly products = signal<Product[]>([]);
  readonly selectedProductId = signal<string | null>(null);
  readonly backlog = signal<BacklogTree | null>(null);
  readonly loadingProducts = signal<boolean>(false);
  readonly loadingBacklog = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  /** Story currently shown in the right-side drawer; null = closed. */
  readonly drawerStory = signal<Story | null>(null);
  /** Mirror of drawer edits — committed via `saveDrawer`. */
  readonly drawerStatus = signal<string>('');
  readonly drawerWsjf = signal<string>('');
  readonly drawerRice = signal<string>('');
  readonly drawerSaving = signal<boolean>(false);
  readonly drawerError = signal<string | null>(null);

  /** True iff a product is selected but its tree hasn't loaded yet. */
  readonly hasSelection = computed(() => this.selectedProductId() !== null);

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
        this.error.set(extractErrorDetail(err, 'Failed to load products.'));
        this.loadingProducts.set(false);
      },
    });
  }

  selectProduct(productId: string): void {
    this.selectedProductId.set(productId);
    this.loadBacklog();
  }

  loadBacklog(): void {
    const id = this.selectedProductId();
    if (!id) return;
    this.loadingBacklog.set(true);
    this.error.set(null);
    this.api.getBacklog(id).subscribe({
      next: (tree) => {
        this.backlog.set(tree);
        this.loadingBacklog.set(false);
      },
      error: (err) => {
        this.backlog.set(null);
        this.error.set(extractErrorDetail(err, 'Failed to load backlog.'));
        this.loadingBacklog.set(false);
      },
    });
  }

  openStoryDrawer(story: StoryNode): void {
    this.drawerStory.set(story);
    this.drawerStatus.set(story.status);
    this.drawerWsjf.set(story.wsjf_score?.toString() ?? '');
    this.drawerRice.set(story.rice_score?.toString() ?? '');
    this.drawerError.set(null);
  }

  closeDrawer(): void {
    this.drawerStory.set(null);
    this.drawerError.set(null);
  }

  saveDrawer(): void {
    const story = this.drawerStory();
    if (!story) return;
    this.drawerSaving.set(true);
    this.drawerError.set(null);

    const tree = this.backlog();
    const prev = { ...story };

    // Optimistic update + rollback on error.
    const wsjfRaw = this.drawerWsjf().trim();
    const riceRaw = this.drawerRice().trim();
    const next = {
      ...story,
      status: this.drawerStatus().trim() || story.status,
      wsjf_score: wsjfRaw === '' ? null : Number(wsjfRaw),
      rice_score: riceRaw === '' ? null : Number(riceRaw),
    };
    if (
      (next.wsjf_score !== null && Number.isNaN(next.wsjf_score)) ||
      (next.rice_score !== null && Number.isNaN(next.rice_score))
    ) {
      this.drawerError.set('Scores must be numeric.');
      this.drawerSaving.set(false);
      return;
    }
    if (tree) this.backlog.set(replaceStory(tree, next));

    const statusChanged = next.status !== prev.status;
    const scoresChanged =
      next.wsjf_score !== prev.wsjf_score || next.rice_score !== prev.rice_score;

    const rollback = (msg: string) => {
      if (tree) this.backlog.set(replaceStory(tree, prev));
      this.drawerError.set(msg);
      this.drawerSaving.set(false);
    };

    const finish = () => {
      this.drawerSaving.set(false);
      this.closeDrawer();
    };

    if (statusChanged && scoresChanged) {
      this.api.patchStoryStatus(story.id, { status: next.status }).subscribe({
        next: () => {
          this.api
            .patchStoryScores(story.id, {
              wsjf_score: next.wsjf_score,
              rice_score: next.rice_score,
            })
            .subscribe({
              next: () => finish(),
              error: (err) =>
                rollback(extractErrorDetail(err, 'Failed to update scores.')),
            });
        },
        error: (err) =>
          rollback(extractErrorDetail(err, 'Failed to update status.')),
      });
    } else if (statusChanged) {
      this.api.patchStoryStatus(story.id, { status: next.status }).subscribe({
        next: () => finish(),
        error: (err) =>
          rollback(extractErrorDetail(err, 'Failed to update status.')),
      });
    } else if (scoresChanged) {
      this.api
        .patchStoryScores(story.id, {
          wsjf_score: next.wsjf_score,
          rice_score: next.rice_score,
        })
        .subscribe({
          next: () => finish(),
          error: (err) =>
            rollback(extractErrorDetail(err, 'Failed to update scores.')),
        });
    } else {
      finish();
    }
  }

  openGroomModal(): void {
    const productId = this.selectedProductId();
    if (!productId) return;
    const data: GroomModalData = { productId };
    const ref = this.dialog.open<GroomModalComponent, GroomModalData, GroomModalResult>(
      GroomModalComponent,
      { data, width: '720px', maxWidth: '90vw' },
    );
    ref.afterClosed().subscribe((res) => this.onGroomClosed(res ?? undefined));
  }

  /** Public for unit tests; reload backlog only if the user applied. */
  onGroomClosed(result?: GroomModalResult): void {
    if (result?.applied) this.loadBacklog();
  }
}

/** Pure helper: return a new BacklogTree with `story` replaced by `next`. */
function replaceStory(tree: BacklogTree, next: Story): BacklogTree {
  return {
    ...tree,
    initiatives: tree.initiatives.map((i) => ({
      ...i,
      epics: i.epics.map((e) => ({
        ...e,
        stories: e.stories.map((s) =>
          s.id === next.id ? { ...s, ...next, tasks: s.tasks, acceptance_criteria: s.acceptance_criteria } : s,
        ),
      })),
    })),
  };
}
