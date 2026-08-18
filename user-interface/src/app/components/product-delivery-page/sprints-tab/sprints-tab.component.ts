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
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { ProductDeliveryService } from '../../../services/product-delivery.service';
import type {
  Product,
  Sprint,
  SprintPlanResult,
} from '../../../models/product-delivery.model';

const ACTIVE_STATUSES = new Set(['draft', 'proposed', 'planning']);

@Component({
  selector: 'app-sprints-tab',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatChipsModule,
    MatFormFieldModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatSelectModule,
  ],
  templateUrl: './sprints-tab.component.html',
  styleUrl: './sprints-tab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SprintsTabComponent implements OnInit {
  private readonly api = inject(ProductDeliveryService);

  readonly products = signal<Product[]>([]);
  readonly selectedProductId = signal<string | null>(null);
  readonly sprints = signal<Sprint[]>([]);
  readonly loadingProducts = signal<boolean>(false);
  readonly loadingSprints = signal<boolean>(false);
  /** Sprint id currently being planned (drives per-row spinner). */
  readonly planningId = signal<string | null>(null);
  readonly planResult = signal<SprintPlanResult | null>(null);
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
    this.planResult.set(null);
    this.loadSprints();
  }

  loadSprints(): void {
    const productId = this.selectedProductId();
    if (!productId) return;
    this.loadingSprints.set(true);
    this.error.set(null);
    this.api.listSprints(productId).subscribe({
      next: (rows) => {
        this.sprints.set(rows);
        this.loadingSprints.set(false);
      },
      error: (err) => {
        this.sprints.set([]);
        this.loadingSprints.set(false);
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to load sprints.');
      },
    });
  }

  canPlan(sprint: Sprint): boolean {
    return ACTIVE_STATUSES.has(sprint.status);
  }

  planSprint(sprint: Sprint): void {
    this.planningId.set(sprint.id);
    this.planResult.set(null);
    this.error.set(null);
    this.api.planSprint(sprint.id).subscribe({
      next: (res) => {
        this.planResult.set(res);
        this.planningId.set(null);
        this.loadSprints();
      },
      error: (err) => {
        this.planningId.set(null);
        this.error.set(err?.error?.detail ?? err?.message ?? 'Failed to plan sprint.');
      },
    });
  }

  dismissPlanResult(): void {
    this.planResult.set(null);
  }
}
