import { Component, OnInit, ViewChild, ElementRef, AfterViewChecked, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatTooltipModule } from '@angular/material/tooltip';
import { FormsModule } from '@angular/forms';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { NutritionApiService } from '../../services/nutrition-api.service';
import { NutritionFormsComponent } from '../nutrition-forms/nutrition-forms.component';
import type {
  ClientProfile,
  MealRecommendation,
  NutritionChatMessage,
  NutritionChatResponse,
  NutritionPlanResponse,
} from '../../models';

const CLIENT_ID_STORAGE_KEY = 'nutritionClientId';

const PHASES = [
  { key: 'intake', label: 'Getting to know you', icon: 'person' },
  { key: 'nutrition', label: 'Nutrition snapshot', icon: 'monitoring' },
  { key: 'meals', label: 'Meal planning', icon: 'restaurant_menu' },
  { key: 'feedback', label: 'Feedback & refine', icon: 'thumb_up' },
];

@Component({
  selector: 'app-nutrition-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
    MatDividerModule,
    MatTooltipModule,
    MatSlideToggleModule,
    MatButtonToggleModule,
    NutritionFormsComponent,
  ],
  templateUrl: './nutrition-dashboard.component.html',
  styleUrl: './nutrition-dashboard.component.scss',
})
export class NutritionDashboardComponent implements OnInit, AfterViewChecked {
  @ViewChild('messagesContainer') messagesContainer!: ElementRef<HTMLDivElement>;

  private readonly api = inject(NutritionApiService);
  private readonly fb = inject(FormBuilder);

  // --- State ---
  viewMode: 'chat' | 'forms' = 'chat';
  clientId = '';
  clientIdConfirmed = false;
  loading = false;
  healthStatus: 'checking' | 'healthy' | 'unhealthy' = 'checking';
  currentPhase = 'intake';
  phases = PHASES;

  messages: NutritionChatMessage[] = [];
  profile: ClientProfile | null = null;
  nutritionPlan: NutritionPlanResponse | null = null;
  mealSuggestions: MealRecommendation[] = [];

  // Inline feedback state
  feedbackMealId = '';
  feedbackRating: number | undefined;
  feedbackWouldMakeAgain: boolean | undefined;
  feedbackNotes = '';

  readonly messageForm = this.fb.nonNullable.group({
    message: ['', [Validators.required, Validators.minLength(1)]],
  });

  readonly quickActions = [
    { label: 'Tell me about my household', message: "I'd like to set up my household profile." },
    { label: 'Show nutrition snapshot', message: "Let's see my nutrition targets." },
    { label: 'Plan my week', message: 'Can you plan meals for the next 7 days?' },
    { label: 'I have feedback', message: "I'd like to give feedback on some meals." },
  ];

  constructor() {
    this.checkHealth();
  }

  ngOnInit(): void {
    const saved = localStorage.getItem(CLIENT_ID_STORAGE_KEY);
    if (saved?.trim()) {
      this.clientId = saved.trim();
    }
  }

  ngAfterViewChecked(): void {
    this.scrollToBottom();
  }

  get phaseIndex(): number {
    const idx = PHASES.findIndex((p) => p.key === this.currentPhase);
    return idx >= 0 ? idx : 0;
  }

  checkHealth(): void {
    this.healthStatus = 'checking';
    this.api.healthCheck().subscribe({
      next: () => (this.healthStatus = 'healthy'),
      error: () => (this.healthStatus = 'unhealthy'),
    });
  }

  suggestClientId(): void {
    const id =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? `home-${crypto.randomUUID().slice(0, 8)}`
        : `home-${Math.random().toString(36).slice(2, 10)}`;
    this.clientId = id;
  }

  confirmClientId(): void {
    const id = this.clientId.trim();
    if (!id) return;
    localStorage.setItem(CLIENT_ID_STORAGE_KEY, id);
    this.clientIdConfirmed = true;

    // Welcome message
    this.messages.push({
      role: 'assistant',
      content:
        `Hi there! I'm your nutritionist assistant. I'll help you build a personalized meal plan ` +
        `through our conversation.\n\n` +
        `Let's start by getting to know your household. Who are you cooking for? ` +
        `Tell me about the people in your home — ages, any dietary needs, allergies, that sort of thing.`,
      timestamp: new Date().toISOString(),
      phase: 'intake',
    });

    // Try to load existing profile
    this.api.getProfile(id).subscribe({
      next: (profile) => {
        this.profile = profile;
        this.messages.push({
          role: 'assistant',
          content:
            `Welcome back! I found your saved profile. I can see your household details are already on file. ` +
            `Would you like to update anything, see your nutrition snapshot, or jump straight to meal planning?`,
          timestamp: new Date().toISOString(),
          phase: 'intake',
          profile,
        });
      },
      error: () => {
        // No existing profile — that's fine
      },
    });
  }

  onSubmit(): void {
    if (this.messageForm.valid && !this.loading) {
      const message = this.messageForm.getRawValue().message.trim();
      if (message) this.sendMessage(message);
    }
  }

  onQuickAction(action: { label: string; message: string }): void {
    this.sendMessage(action.message);
  }

  sendMessage(message: string): void {
    // Add user message
    this.messages.push({
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    });
    this.messageForm.reset();
    this.loading = true;

    // Build conversation history (text-only for the API)
    const history = this.messages
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .slice(-20) // Keep last 20 messages for context
      .map((m) => ({ role: m.role, content: m.content }));

    this.api
      .sendChatMessage({
        client_id: this.clientId,
        message,
        conversation_history: history.slice(0, -1), // Exclude the message we just sent
      })
      .subscribe({
        next: (res) => this.handleChatResponse(res),
        error: (err) => {
          this.messages.push({
            role: 'assistant',
            content: `Sorry, something went wrong: ${err?.error?.detail || err?.message || 'Unknown error'}. Please try again.`,
            timestamp: new Date().toISOString(),
          });
          this.loading = false;
        },
      });
  }

  private handleChatResponse(res: NutritionChatResponse): void {
    this.loading = false;

    // Update phase
    if (res.phase) {
      this.currentPhase = res.phase;
    }

    // Build the message object with any structured data
    const msg: NutritionChatMessage = {
      role: 'assistant',
      content: res.message,
      timestamp: new Date().toISOString(),
      phase: res.phase,
      action: res.action,
    };

    // Attach structured data based on action
    if (res.profile) {
      this.profile = res.profile;
      msg.profile = res.profile;
    }

    if (res.nutrition_plan) {
      this.nutritionPlan = { client_id: this.clientId, plan: res.nutrition_plan };
      msg.nutritionPlan = this.nutritionPlan;
    }

    if (res.meal_suggestions && res.meal_suggestions.length > 0) {
      this.mealSuggestions = res.meal_suggestions;
      msg.mealSuggestions = res.meal_suggestions;
    }

    this.messages.push(msg);
  }

  // --- Feedback helpers ---

  selectMealForFeedback(meal: MealRecommendation): void {
    this.feedbackMealId = meal.recommendation_id;
  }

  submitFeedback(): void {
    const id = this.clientId.trim();
    const recId = this.feedbackMealId.trim();
    if (!id || !recId) return;

    this.api.submitFeedback(id, recId, this.feedbackRating, this.feedbackWouldMakeAgain, this.feedbackNotes).subscribe({
      next: () => {
        const mealName = this.mealSuggestions.find((m) => m.recommendation_id === recId)?.name || 'that meal';
        this.messages.push({
          role: 'assistant',
          content: `Thanks for the feedback on "${mealName}"! I'll use this to improve your future meal plans.`,
          timestamp: new Date().toISOString(),
          phase: 'feedback',
        });
        this.feedbackMealId = '';
        this.feedbackRating = undefined;
        this.feedbackWouldMakeAgain = undefined;
        this.feedbackNotes = '';
      },
      error: () => {
        this.messages.push({
          role: 'assistant',
          content: "Sorry, I couldn't record that feedback. Please try again.",
          timestamp: new Date().toISOString(),
        });
      },
    });
  }

  // --- Rendering helpers ---

  get mealsByDay(): { key: string; label: string; meals: MealRecommendation[] }[] {
    const map = new Map<string, MealRecommendation[]>();
    for (const m of this.mealSuggestions) {
      const raw = m.suggested_date?.trim();
      const key = raw && raw.length > 0 ? raw : '_unscheduled';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(m);
    }
    const keys = [...map.keys()].sort((a, b) => {
      if (a === '_unscheduled') return 1;
      if (b === '_unscheduled') return -1;
      return a.localeCompare(b);
    });
    return keys.map((key) => ({
      key,
      label: key === '_unscheduled' ? 'Flexible timing' : key,
      meals: map.get(key)!,
    }));
  }

  formatTime(timestamp?: string): string {
    if (!timestamp) return '';
    return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  private scrollToBottom(): void {
    if (this.messagesContainer) {
      const el = this.messagesContainer.nativeElement;
      el.scrollTop = el.scrollHeight;
    }
  }
}
