import { Component, Input, inject, OnInit, OnChanges, SimpleChanges } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatChipsModule } from '@angular/material/chips';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import type { Reservation } from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

/**
 * Reservations component for managing restaurant/appointment reservations.
 */
@Component({
  selector: 'app-pa-reservations',
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
    MatSnackBarModule,
  ],
  templateUrl: './pa-reservations.component.html',
  styleUrl: './pa-reservations.component.scss',
})
export class PaReservationsComponent implements OnInit, OnChanges {
  @Input() userId = 'default';

  private readonly api = inject(PersonalAssistantApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);

  reservations: Reservation[] = [];
  loading = false;
  creating = false;
  form: FormGroup;

  constructor() {
    this.form = this.fb.nonNullable.group({
      reservationText: ['', [Validators.required, Validators.minLength(5)]],
    });
  }

  ngOnInit(): void {
    this.loadReservations();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['userId'] && !changes['userId'].firstChange) {
      this.loadReservations();
    }
  }

  private loadReservations(): void {
    this.loading = true;
    this.api.getReservations(this.userId).subscribe({
      next: (reservations) => {
        this.reservations = reservations;
        this.loading = false;
      },
      error: () => {
        this.reservations = [];
        this.loading = false;
      },
    });
  }

  onCreateReservation(): void {
    if (this.form.invalid || this.creating) return;

    const text = this.form.getRawValue().reservationText.trim();
    this.creating = true;

    this.api.createReservationFromText(this.userId, { text }).subscribe({
      next: (res) => {
        this.creating = false;
        if (res.success) {
          this.snackBar.open(
            res.action_required
              ? `Reservation pending: ${res.action_required}`
              : 'Reservation created!',
            'Close',
            { duration: 5000 }
          );
          this.form.reset();
          this.loadReservations();
        } else {
          this.snackBar.open(res.message || 'Failed to create reservation', 'Close', { duration: 3000 });
        }
      },
      error: (err) => {
        this.creating = false;
        this.snackBar.open(extractErrorDetail(err, 'Failed to create reservation'), 'Close', { duration: 3000 });
      },
    });
  }

  formatDateTime(dateStr: string): string {
    const date = new Date(dateStr);
    return date.toLocaleString([], {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  getTypeIcon(type?: string): string {
    switch (type) {
      case 'restaurant':
        return 'restaurant';
      case 'appointment':
        return 'event';
      case 'service':
        return 'build';
      default:
        return 'bookmark';
    }
  }

  getStatusColor(status?: string): string {
    switch (status) {
      case 'confirmed':
        return '#3fb950';
      case 'pending':
        return '#d29922';
      case 'cancelled':
        return '#f85149';
      default:
        return '#8b949e';
    }
  }
}
