import { Component, Input, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatTooltipModule } from '@angular/material/tooltip';
import { NutritionApiService } from '../../services/nutrition-api.service';
import type {
  ClientProfile,
  MealRecommendation,
  NutritionPlanResponse,
  MealHistoryResponse,
} from '../../models';

@Component({
  selector: 'app-nutrition-forms',
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
    MatTabsModule,
    MatSelectModule,
    MatChipsModule,
    MatDividerModule,
    MatProgressSpinnerModule,
    MatSlideToggleModule,
    MatTooltipModule,
  ],
  templateUrl: './nutrition-forms.component.html',
  styleUrl: './nutrition-forms.component.scss',
})
export class NutritionFormsComponent {
  @Input() clientId = '';

  private readonly api = inject(NutritionApiService);
  private readonly fb = inject(FormBuilder);

  selectedTabIndex = 0;
  loading = false;
  statusMessage = '';
  statusType: 'success' | 'error' | '' = '';

  // Profile state
  profile: ClientProfile | null = null;
  profileForm = this.fb.group({
    numberOfPeople: [1, [Validators.required, Validators.min(1)]],
    description: [''],
    dietaryNeeds: [''],
    allergies: [''],
    maxCookingTime: [null as number | null],
    lunchContext: [''],
    equipmentConstraints: [''],
    otherConstraints: [''],
    cuisinesLiked: [''],
    cuisinesDisliked: [''],
    ingredientsDisliked: [''],
    preferencesFreeText: [''],
    goalType: ['maintain'],
    goalNotes: [''],
  });

  // Nutrition plan state
  nutritionPlan: NutritionPlanResponse | null = null;

  // Meal plan state
  mealPlanDays = 7;
  mealTypes = ['breakfast', 'lunch', 'dinner'];
  mealSuggestions: MealRecommendation[] = [];

  // Feedback state
  feedbackMealId = '';
  feedbackRating: number | undefined;
  feedbackWouldMakeAgain: boolean | undefined;
  feedbackNotes = '';

  // History state
  mealHistory: MealHistoryResponse | null = null;

  readonly goalOptions = [
    { value: 'maintain', label: 'Maintain weight' },
    { value: 'lose_weight', label: 'Lose weight' },
    { value: 'gain_weight', label: 'Gain weight' },
    { value: 'build_muscle', label: 'Build muscle' },
    { value: 'improve_health', label: 'Improve overall health' },
    { value: 'manage_condition', label: 'Manage health condition' },
  ];

  readonly mealTypeOptions = [
    { value: 'breakfast', label: 'Breakfast' },
    { value: 'lunch', label: 'Lunch' },
    { value: 'dinner', label: 'Dinner' },
    { value: 'snack', label: 'Snack' },
  ];

  // --- Profile ---

  loadProfile(): void {
    if (!this.clientId.trim()) return;
    this.loading = true;
    this.clearStatus();
    this.api.getProfile(this.clientId).subscribe({
      next: (profile) => {
        this.profile = profile;
        this.populateProfileForm(profile);
        this.showStatus('Profile loaded successfully.', 'success');
        this.loading = false;
      },
      error: (err) => {
        if (err.status === 404) {
          this.showStatus('No existing profile found. Fill out the form to create one.', 'success');
        } else {
          this.showStatus(`Failed to load profile: ${err?.error?.detail || err?.message || 'Unknown error'}`, 'error');
        }
        this.loading = false;
      },
    });
  }

  saveProfile(): void {
    if (!this.clientId.trim() || this.profileForm.invalid) return;
    this.loading = true;
    this.clearStatus();
    const v = this.profileForm.getRawValue();
    this.api
      .upsertProfile(this.clientId, {
        household: {
          number_of_people: v.numberOfPeople ?? 1,
          description: v.description ?? '',
          ages_if_relevant: [],
        },
        dietary_needs: this.splitComma(v.dietaryNeeds),
        allergies_and_intolerances: this.splitComma(v.allergies),
        lifestyle: {
          max_cooking_time_minutes: v.maxCookingTime,
          lunch_context: v.lunchContext ?? '',
          equipment_constraints: this.splitComma(v.equipmentConstraints),
          other_constraints: v.otherConstraints ?? '',
        },
        preferences: {
          cuisines_liked: this.splitComma(v.cuisinesLiked),
          cuisines_disliked: this.splitComma(v.cuisinesDisliked),
          ingredients_disliked: this.splitComma(v.ingredientsDisliked),
          preferences_free_text: v.preferencesFreeText ?? '',
        },
        goals: {
          goal_type: v.goalType ?? 'maintain',
          notes: v.goalNotes ?? '',
        },
      })
      .subscribe({
        next: (profile) => {
          this.profile = profile;
          this.showStatus('Profile saved successfully!', 'success');
          this.loading = false;
        },
        error: (err) => {
          this.showStatus(`Failed to save profile: ${err?.error?.detail || err?.message || 'Unknown error'}`, 'error');
          this.loading = false;
        },
      });
  }

  // --- Nutrition Plan ---

  generateNutritionPlan(): void {
    if (!this.clientId.trim()) return;
    this.loading = true;
    this.clearStatus();
    this.api.generateNutritionPlan(this.clientId).subscribe({
      next: (plan) => {
        this.nutritionPlan = plan;
        this.showStatus('Nutrition plan generated!', 'success');
        this.loading = false;
      },
      error: (err) => {
        this.showStatus(
          `Failed to generate nutrition plan: ${err?.error?.detail || err?.message || 'Unknown error'}. Make sure you have a saved profile first.`,
          'error'
        );
        this.loading = false;
      },
    });
  }

  // --- Meal Plan ---

  toggleMealType(type: string): void {
    const idx = this.mealTypes.indexOf(type);
    if (idx >= 0) {
      this.mealTypes.splice(idx, 1);
    } else {
      this.mealTypes.push(type);
    }
  }

  isMealTypeSelected(type: string): boolean {
    return this.mealTypes.includes(type);
  }

  generateMealPlan(): void {
    if (!this.clientId.trim() || this.mealTypes.length === 0) return;
    this.loading = true;
    this.clearStatus();
    this.api.generateMealPlan(this.clientId, this.mealPlanDays, this.mealTypes).subscribe({
      next: (res) => {
        this.mealSuggestions = res.suggestions;
        this.showStatus(`Generated ${res.suggestions.length} meal suggestions!`, 'success');
        this.loading = false;
      },
      error: (err) => {
        this.showStatus(
          `Failed to generate meal plan: ${err?.error?.detail || err?.message || 'Unknown error'}. Make sure you have a saved profile and nutrition plan first.`,
          'error'
        );
        this.loading = false;
      },
    });
  }

  selectMealForFeedback(meal: MealRecommendation): void {
    this.feedbackMealId = meal.recommendation_id;
  }

  submitFeedback(): void {
    if (!this.clientId.trim() || !this.feedbackMealId.trim()) return;
    this.loading = true;
    this.api
      .submitFeedback(this.clientId, this.feedbackMealId, this.feedbackRating, this.feedbackWouldMakeAgain, this.feedbackNotes)
      .subscribe({
        next: () => {
          this.showStatus('Feedback submitted! Future meal plans will reflect your preferences.', 'success');
          this.feedbackMealId = '';
          this.feedbackRating = undefined;
          this.feedbackWouldMakeAgain = undefined;
          this.feedbackNotes = '';
          this.loading = false;
        },
        error: (err) => {
          this.showStatus(`Failed to submit feedback: ${err?.error?.detail || err?.message || 'Unknown error'}`, 'error');
          this.loading = false;
        },
      });
  }

  // --- History ---

  loadHistory(): void {
    if (!this.clientId.trim()) return;
    this.loading = true;
    this.clearStatus();
    this.api.getMealHistory(this.clientId).subscribe({
      next: (history) => {
        this.mealHistory = history;
        this.showStatus(`Loaded ${history.entries.length} history entries.`, 'success');
        this.loading = false;
      },
      error: (err) => {
        this.showStatus(`Failed to load history: ${err?.error?.detail || err?.message || 'Unknown error'}`, 'error');
        this.loading = false;
      },
    });
  }

  // --- Helpers ---

  private populateProfileForm(p: ClientProfile): void {
    this.profileForm.patchValue({
      numberOfPeople: p.household.number_of_people,
      description: p.household.description,
      dietaryNeeds: p.dietary_needs.join(', '),
      allergies: p.allergies_and_intolerances.join(', '),
      maxCookingTime: p.lifestyle.max_cooking_time_minutes ?? null,
      lunchContext: p.lifestyle.lunch_context,
      equipmentConstraints: p.lifestyle.equipment_constraints.join(', '),
      otherConstraints: p.lifestyle.other_constraints,
      cuisinesLiked: p.preferences.cuisines_liked.join(', '),
      cuisinesDisliked: p.preferences.cuisines_disliked.join(', '),
      ingredientsDisliked: p.preferences.ingredients_disliked.join(', '),
      preferencesFreeText: p.preferences.preferences_free_text,
      goalType: p.goals.goal_type || 'maintain',
      goalNotes: p.goals.notes,
    });
  }

  private splitComma(value: string | null | undefined): string[] {
    if (!value) return [];
    return value
      .split(',')
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }

  private showStatus(message: string, type: 'success' | 'error'): void {
    this.statusMessage = message;
    this.statusType = type;
  }

  private clearStatus(): void {
    this.statusMessage = '';
    this.statusType = '';
  }
}
