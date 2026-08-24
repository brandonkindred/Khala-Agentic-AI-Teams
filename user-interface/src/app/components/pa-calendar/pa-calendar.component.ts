import { Component, Input, inject } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatChipsModule } from '@angular/material/chips';
import { PersonalAssistantApiService } from '../../services/personal-assistant-api.service';
import type { CalendarEvent, EventFromTextResponse } from '../../models';
import { extractErrorDetail } from '../../shared/extract-error-detail';

/**
 * Calendar component for viewing events and creating them with natural language.
 */
@Component({
  selector: 'app-pa-calendar',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatListModule,
    MatSnackBarModule,
    MatChipsModule,
  ],
  templateUrl: './pa-calendar.component.html',
  styleUrl: './pa-calendar.component.scss',
})
export class PaCalendarComponent {
  @Input() userId = 'default';

  private readonly api = inject(PersonalAssistantApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);

  events: CalendarEvent[] = [];
  parsedEvents: CalendarEvent[] = [];
  ambiguities: string[] = [];
  loading = false;
  needsConfirmation = false;
  form: FormGroup;

  constructor() {
    this.form = this.fb.nonNullable.group({
      eventText: ['', [Validators.required, Validators.minLength(5)]],
    });
  }

  onCreateEvent(): void {
    if (this.form.invalid || this.loading) return;

    const text = this.form.getRawValue().eventText.trim();
    this.loading = true;
    this.parsedEvents = [];
    this.ambiguities = [];
    this.needsConfirmation = false;

    this.api.createEventFromText(this.userId, { text, auto_create: false }).subscribe({
      next: (res) => {
        this.handleResponse(res);
        this.loading = false;
      },
      error: (err) => {
        this.loading = false;
        this.snackBar.open(extractErrorDetail(err, 'Failed to parse event'), 'Close', { duration: 3000 });
      },
    });
  }

  private handleResponse(res: EventFromTextResponse): void {
    if (res.needs_confirmation && res.parsed_events?.length) {
      this.needsConfirmation = true;
      this.parsedEvents = (res.parsed_events as unknown as CalendarEvent[]).map((e, i) => ({
        ...e,
        event_id: e.event_id || `preview-${i}`,
      }));
      this.ambiguities = res.ambiguities || [];
    } else if (res.success && res.created_event_ids?.length) {
      this.snackBar.open(`Created ${res.created_event_ids.length} event(s)`, 'Close', { duration: 3000 });
      this.form.reset();
    } else {
      this.snackBar.open(res.message || 'No events parsed', 'Close', { duration: 3000 });
    }
  }

  onConfirmEvents(): void {
    const text = this.form.getRawValue().eventText.trim();
    this.loading = true;

    this.api.createEventFromText(this.userId, { text, auto_create: true }).subscribe({
      next: (res) => {
        this.loading = false;
        this.needsConfirmation = false;
        this.parsedEvents = [];
        if (res.success) {
          this.snackBar.open(`Created ${res.created_event_ids?.length || 0} event(s)`, 'Close', { duration: 3000 });
          this.form.reset();
        } else {
          this.snackBar.open(res.message || 'Failed to create events', 'Close', { duration: 3000 });
        }
      },
      error: (err) => {
        this.loading = false;
        this.snackBar.open(extractErrorDetail(err, 'Failed to create events'), 'Close', { duration: 3000 });
      },
    });
  }

  onCancelConfirmation(): void {
    this.needsConfirmation = false;
    this.parsedEvents = [];
    this.ambiguities = [];
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

  formatDuration(minutes?: number): string {
    if (!minutes) return '';
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  }
}
