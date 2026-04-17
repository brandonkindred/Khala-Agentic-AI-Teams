import {
  AfterViewChecked,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  ViewChild,
  inject,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTabsModule } from '@angular/material/tabs';
import { Subject, takeUntil } from 'rxjs';

import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { RoadTripPlanningApiService } from '../../services/road-trip-planning-api.service';
import type {
  ChatMessage,
  DayPlan,
  PlanJob,
  TripContextField,
  TripItinerary,
  TripRequest,
  TripSlotKey,
} from '../../models';
import {
  CONTEXT_SCHEMA,
  assistantMessage,
  detectIntent,
  displayValueFor,
  freshTrip,
  initialGreeting,
  isSlotEmpty,
  parseSlotValue,
  pickNextSlot,
  promptForSlot,
  readinessSummary,
  userMessage,
} from './trip-slot-filler';

const STORAGE_KEY = 'khala.roadTripPlanning.session.v1';

/**
 * Road Trip Planning — conversational planner dashboard.
 *
 * Two-panel layout (chat + trip context / itinerary) with client-side
 * slot-filling chat driving a one-shot backend pipeline. Session state
 * is persisted to localStorage so refreshes preserve work.
 */
@Component({
  selector: 'app-road-trip-planning-dashboard',
  standalone: true,
  imports: [
    DecimalPipe,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
    MatSnackBarModule,
    MatTabsModule,
    DashboardShellComponent,
  ],
  templateUrl: './road-trip-planning-dashboard.component.html',
  styleUrl: './road-trip-planning-dashboard.component.scss',
})
export class RoadTripPlanningDashboardComponent implements OnInit, AfterViewChecked, OnDestroy {
  @ViewChild('messagesContainer') messagesContainer?: ElementRef<HTMLDivElement>;

  private readonly api = inject(RoadTripPlanningApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);
  private readonly destroy$ = new Subject<void>();

  // --- Conversational state ---
  messages: ChatMessage[] = [];
  trip: TripRequest = freshTrip();
  pendingSlot: TripSlotKey | null = null;
  /** Slots the user has explicitly opted out of ("skip for now", "flexible", "I'm done"). */
  declinedSlots = new Set<TripSlotKey>();

  // --- Itinerary state ---
  itinerary: TripItinerary | null = null;
  previousItineraries: TripItinerary[] = [];
  dirtyRePlan = false;

  // --- UI state ---
  sending = false;
  planning = false;
  planningStep: string | null = null;
  error: string | null = null;
  editingKey: TripSlotKey | null = null;
  editingValue = '';

  readonly contextSchema = CONTEXT_SCHEMA;

  readonly form = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  readonly getHealth = () => this.api.getHealth();

  ngOnInit(): void {
    const restored = this.loadSession();
    if (restored) {
      this.trip = restored.trip;
      this.messages = restored.messages;
      this.pendingSlot = restored.pendingSlot;
      this.itinerary = restored.itinerary;
      this.previousItineraries = restored.previousItineraries;
      this.dirtyRePlan = restored.dirtyRePlan;
      this.declinedSlots = new Set(restored.declinedSlots ?? []);
    } else {
      this.startFreshConversation();
    }
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  // -------------------------------------------------------------------------
  // Conversation
  // -------------------------------------------------------------------------

  onSubmit(): void {
    if (this.form.invalid || this.sending) return;
    const text = this.form.getRawValue().message.trim();
    if (!text) return;
    this.form.reset({ message: '' });
    this.handleUserMessage(text);
  }

  onQuickReply(reply: string): void {
    if (this.sending) return;
    this.handleUserMessage(reply);
  }

  onRestart(): void {
    this.snackBar.open('Started a new trip', 'OK', { duration: 2000 });
    this.startFreshConversation();
    this.persistSession();
  }

  private startFreshConversation(): void {
    this.trip = freshTrip();
    this.itinerary = null;
    this.previousItineraries = [];
    this.dirtyRePlan = false;
    this.declinedSlots = new Set();
    this.pendingSlot = 'start_location';
    this.messages = [initialGreeting(), promptForSlot('start_location')];
  }

  private handleUserMessage(text: string): void {
    this.messages = [...this.messages, userMessage(text)];
    const intent = detectIntent(text, this.pendingSlot);

    switch (intent.kind) {
      case 'restart':
        this.onRestart();
        return;

      case 'plan_now':
        this.respond(this.readyToPlan() ? 'Great — kicking off the plan now.' : this.missingFieldsMessage());
        if (this.readyToPlan()) this.generateItinerary();
        break;

      case 'fill':
        this.applySlotFill(intent.slot, intent.rawValue);
        break;

      case 'add_stop':
        this.trip = { ...this.trip, required_stops: [...this.trip.required_stops, intent.value] };
        this.dirtyRePlan = this.itinerary !== null;
        this.respond(`Added **${intent.value}** to your stops.`);
        break;

      case 'remove_stop': {
        const before = this.trip.required_stops.length;
        this.trip = {
          ...this.trip,
          required_stops: this.trip.required_stops.filter(
            (s) => s.toLowerCase() !== intent.value.toLowerCase(),
          ),
        };
        this.dirtyRePlan = this.itinerary !== null && before !== this.trip.required_stops.length;
        this.respond(
          before === this.trip.required_stops.length
            ? `I couldn't find **${intent.value}** in your stops, so nothing changed.`
            : `Removed **${intent.value}** from your stops.`,
        );
        break;
      }

      case 'add_preference':
        this.trip = { ...this.trip, preferences: [...this.trip.preferences, text] };
        this.dirtyRePlan = this.itinerary !== null;
        this.respond(`Got it — I'll factor that into the plan.`);
        break;

      case 'clear_preferences':
        this.trip = { ...this.trip, preferences: [] };
        this.respond('Cleared your preferences.');
        break;

      case 'unknown':
      default:
        this.respond(
          "I didn't quite catch that — can you tell me a bit more? (Or click one of the quick replies.)",
        );
        break;
    }

    this.advanceSlot();
    this.persistSession();
  }

  private applySlotFill(slot: TripSlotKey, raw: string): void {
    const result = parseSlotValue(slot, raw, this.trip);
    const pretty = labelFor(slot);

    // User explicitly opted out of this optional field ("skip", "flexible",
    // "I'm done"). Mark it declined so advanceSlot() doesn't ask again.
    if (result.declined) {
      this.declinedSlots.add(slot);
      this.pendingSlot = null;
      this.respond(`Got it — we'll leave **${pretty}** open for now.`);
      return;
    }

    // Parser rejected the text (couldn't find a number, unparseable date,
    // empty traveler list, etc.). Re-prompt rather than storing garbage.
    if (!result.accepted) {
      this.respond(`I couldn't parse a value for **${pretty}** — want to try again?`);
      this.pendingSlot = slot;
      return;
    }

    const changed = JSON.stringify(result.trip) !== JSON.stringify(this.trip);
    this.trip = result.trip;
    this.dirtyRePlan = this.itinerary !== null && changed;
    // A fresh answer supersedes any prior decline of the same slot.
    this.declinedSlots.delete(slot);

    const display = displayValueFor(slot, result.trip);
    this.respond(display ? `Noted **${pretty}**: ${display}.` : `Got it — **${pretty}** updated.`);
    this.pendingSlot = null;
  }

  private advanceSlot(): void {
    // If we're already in the middle of a slot, don't advance.
    if (this.pendingSlot) return;
    const next = pickNextSlot(this.trip, this.declinedSlots);
    if (next && isSlotEmpty(this.trip, next)) {
      this.pendingSlot = next;
      this.messages = [...this.messages, promptForSlot(next)];
    } else if (this.readyToPlan() && !this.itinerary) {
      this.messages = [
        ...this.messages,
        assistantMessage("I've got enough to start planning. Click **Generate Itinerary** on the right whenever you're ready."),
      ];
    } else if (this.readyToPlan() && this.itinerary && this.dirtyRePlan) {
      this.messages = [
        ...this.messages,
        assistantMessage('Your trip changed — hit **Re-plan** on the right to rebuild the itinerary.'),
      ];
    }
  }

  private respond(content: string, chips?: string[]): void {
    this.messages = [...this.messages, assistantMessage(content, chips)];
  }

  // -------------------------------------------------------------------------
  // Planning (async job + poll)
  // -------------------------------------------------------------------------

  generateItinerary(): void {
    if (!this.readyToPlan() || this.planning) return;

    this.planning = true;
    this.error = null;
    this.planningStep = 'Submitting plan…';
    this.respond('Dispatching the planning team — this usually takes 30–90 seconds.');

    let stepIndex = 0;
    const steps = [
      'Traveler Profiler is synthesising the group…',
      'Route Planner is mapping the ordered stops…',
      'Activities Expert is picking things to do…',
      'Logistics Agent is finding places to stay…',
      'Itinerary Composer is assembling the day-by-day plan…',
    ];

    this.api
      .planAndPoll({ trip: this.trip }, 2500)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (job: PlanJob) => {
          if (job.status === 'running' || job.status === 'pending') {
            this.planningStep = steps[Math.min(stepIndex, steps.length - 1)];
            stepIndex += 1;
          }
          if (job.status === 'completed' && job.result) {
            this.onPlanCompleted(job.result);
          }
          if (job.status === 'failed') {
            this.onPlanFailed(job.error ?? 'Unknown error');
          }
        },
        error: (err) => {
          this.onPlanFailed(err?.error?.detail ?? err?.message ?? 'Planning request failed');
        },
      });
  }

  private onPlanCompleted(itinerary: TripItinerary): void {
    if (this.itinerary) {
      this.previousItineraries = [this.itinerary, ...this.previousItineraries].slice(0, 3);
    }
    this.itinerary = itinerary;
    this.dirtyRePlan = false;
    this.planning = false;
    this.planningStep = null;
    this.respond(
      `Your **${itinerary.total_days}-day** itinerary is ready — ${itinerary.title}. Scroll the right panel to review it, or tell me what to change.`,
      ['Make day 1 more relaxed', 'Add a rest day', 'Find cheaper stays'],
    );
    this.persistSession();
  }

  private onPlanFailed(detail: string): void {
    this.planning = false;
    this.planningStep = null;
    this.error = detail;
    this.respond(`Planning hit an error: ${detail}. You can try again when ready.`);
    this.persistSession();
  }

  // -------------------------------------------------------------------------
  // Direct context editing (pencil icons on the trip panel)
  // -------------------------------------------------------------------------

  startEdit(key: TripSlotKey): void {
    this.editingKey = key;
    this.editingValue = displayValueFor(key, this.trip) ?? '';
  }

  cancelEdit(): void {
    this.editingKey = null;
    this.editingValue = '';
  }

  saveEdit(): void {
    if (!this.editingKey) return;
    const key = this.editingKey;
    const value = this.editingValue.trim();
    this.editingKey = null;
    this.editingValue = '';
    if (!value) return;

    const result = parseSlotValue(key, value, this.trip);
    if (!result.accepted && !result.declined) {
      this.snackBar.open(
        `Couldn't parse "${value}" for ${labelFor(key).toLowerCase()}`,
        'OK',
        { duration: 2500 },
      );
      return;
    }

    const changed = JSON.stringify(result.trip) !== JSON.stringify(this.trip);
    this.trip = result.trip;
    if (result.declined) {
      this.declinedSlots.add(key);
    } else {
      this.declinedSlots.delete(key);
    }
    if (changed || result.declined) {
      this.dirtyRePlan = this.itinerary !== null && changed;
      this.snackBar.open(`Updated ${labelFor(key).toLowerCase()}`, 'OK', { duration: 1800 });
    }
    this.persistSession();
  }

  removeStop(stop: string): void {
    this.trip = {
      ...this.trip,
      required_stops: this.trip.required_stops.filter((s) => s !== stop),
    };
    this.dirtyRePlan = this.itinerary !== null;
    this.persistSession();
  }

  removePreference(pref: string): void {
    this.trip = {
      ...this.trip,
      preferences: this.trip.preferences.filter((p) => p !== pref),
    };
    this.dirtyRePlan = this.itinerary !== null;
    this.persistSession();
  }

  // -------------------------------------------------------------------------
  // Day-card "rework this day" shortcut
  // -------------------------------------------------------------------------

  reworkDay(day: DayPlan): void {
    const prompt = `Can you rework Day ${day.day_number} in ${day.location}? `;
    this.form.reset({ message: prompt });
    // Focus-the-chat intent — just scroll the messages area.
    this.scrollToBottom();
  }

  // -------------------------------------------------------------------------
  // Helpers for the template
  // -------------------------------------------------------------------------

  contextFields(): TripContextField[] {
    return this.contextSchema.map((s) => ({
      key: s.key,
      label: s.label,
      displayValue: displayValueFor(s.key, this.trip),
      required: s.required,
    }));
  }

  readyToPlan(): boolean {
    return readinessSummary(this.trip).ready;
  }

  missingLabels(): string[] {
    return readinessSummary(this.trip).missing.map((k) => labelFor(k));
  }

  private missingFieldsMessage(): string {
    const missing = this.missingLabels();
    if (missing.length === 0) return 'Looks ready to me!';
    return `I still need: ${missing.join(', ')}. Tell me any of them and we'll keep going.`;
  }

  lastQuickReplies(): string[] | undefined {
    const last = this.messages[this.messages.length - 1];
    return last?.role === 'assistant' ? last.quickReplies : undefined;
  }

  formatTime(ts: string): string {
    if (!ts) return '';
    try {
      return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }

  formatMessage(content: string): string {
    // Tiny inline markdown: only **bold** (safe, no HTML injection risk from agents).
    const escaped = content
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    return escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  }

  dayBadge(day: DayPlan): string {
    const dist = day.driving_distance_miles;
    if (dist == null) return '';
    const hours = day.driving_time_hours ? `${day.driving_time_hours.toFixed(1)} hr` : '';
    return `${Math.round(dist)} mi${hours ? ` · ${hours}` : ''}`;
  }

  labelForSlot(key: TripSlotKey): string {
    return labelFor(key);
  }

  // -------------------------------------------------------------------------
  // Persistence
  // -------------------------------------------------------------------------

  private persistSession(): void {
    try {
      const state = {
        trip: this.trip,
        messages: this.messages,
        pendingSlot: this.pendingSlot,
        itinerary: this.itinerary,
        previousItineraries: this.previousItineraries,
        dirtyRePlan: this.dirtyRePlan,
        // Sets don't survive JSON.stringify — serialise to an array.
        declinedSlots: Array.from(this.declinedSlots),
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // Quota / private browsing — silently ignore.
    }
  }

  private loadSession() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed?.trip || !Array.isArray(parsed.messages)) return null;
      return parsed;
    } catch {
      return null;
    }
  }

  private scrollToBottom(): void {
    const el = this.messagesContainer?.nativeElement;
    if (el) el.scrollTop = el.scrollHeight;
  }
}

function labelFor(key: TripSlotKey): string {
  const entry = CONTEXT_SCHEMA.find((f) => f.key === key);
  return entry?.label ?? key;
}
