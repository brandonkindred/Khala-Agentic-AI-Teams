import { Component, Input, inject, OnInit, OnChanges, SimpleChanges } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import type { WishlistItem, Deal } from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

/**
 * Deals component for managing wishlist and searching for deals.
 */
@Component({
  selector: 'app-pa-deals',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatListModule,
    MatChipsModule,
    MatDividerModule,
    MatSnackBarModule,
  ],
  templateUrl: './pa-deals.component.html',
  styleUrl: './pa-deals.component.scss',
})
export class PaDealsComponent implements OnInit, OnChanges {
  @Input() userId = 'default';

  private readonly api = inject(PersonalAssistantApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);

  wishlist: WishlistItem[] = [];
  deals: Deal[] = [];
  loading = false;
  searching = false;
  addingItem = false;

  wishlistForm: FormGroup;
  searchForm: FormGroup;

  constructor() {
    this.wishlistForm = this.fb.nonNullable.group({
      description: ['', [Validators.required, Validators.minLength(2)]],
      targetPrice: [''],
      category: [''],
    });

    this.searchForm = this.fb.nonNullable.group({
      query: [''],
    });
  }

  ngOnInit(): void {
    this.loadWishlist();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['userId'] && !changes['userId'].firstChange) {
      this.loadWishlist();
    }
  }

  private loadWishlist(): void {
    this.loading = true;
    this.api.getWishlist(this.userId).subscribe({
      next: (items) => {
        this.wishlist = items;
        this.loading = false;
      },
      error: () => {
        this.wishlist = [];
        this.loading = false;
      },
    });
  }

  onAddToWishlist(): void {
    if (this.wishlistForm.invalid || this.addingItem) return;

    const formValue = this.wishlistForm.getRawValue();
    this.addingItem = true;

    this.api
      .addToWishlist(this.userId, {
        description: formValue.description.trim(),
        target_price: formValue.targetPrice ? parseFloat(formValue.targetPrice) : undefined,
        category: formValue.category?.trim() || undefined,
      })
      .subscribe({
        next: (item) => {
          this.wishlist.push(item);
          this.wishlistForm.reset();
          this.addingItem = false;
          this.snackBar.open('Added to wishlist', 'Close', { duration: 3000 });
        },
        error: (err) => {
          this.addingItem = false;
          this.snackBar.open(extractErrorDetail(err, 'Failed to add item'), 'Close', { duration: 3000 });
        },
      });
  }

  onRemoveFromWishlist(item: WishlistItem): void {
    this.api.removeFromWishlist(this.userId, item.item_id).subscribe({
      next: () => {
        this.wishlist = this.wishlist.filter((i) => i.item_id !== item.item_id);
        this.snackBar.open('Removed from wishlist', 'Close', { duration: 3000 });
      },
      error: () => {
        this.snackBar.open('Failed to remove item', 'Close', { duration: 3000 });
      },
    });
  }

  onSearchDeals(): void {
    if (this.searching) return;

    const query = this.searchForm.getRawValue().query?.trim();
    this.searching = true;
    this.deals = [];

    this.api.searchDeals(this.userId, { query: query || undefined }).subscribe({
      next: (res) => {
        this.deals = res.deals;
        this.searching = false;
        if (this.deals.length === 0) {
          this.snackBar.open('No deals found', 'Close', { duration: 3000 });
        }
      },
      error: (err) => {
        this.searching = false;
        this.snackBar.open(extractErrorDetail(err, 'Failed to search deals'), 'Close', { duration: 3000 });
      },
    });
  }

  formatPrice(price?: number): string {
    if (!price) return '';
    return `$${price.toFixed(2)}`;
  }

  formatDiscount(percent?: number): string {
    if (!percent) return '';
    return `${Math.round(percent)}% off`;
  }
}
