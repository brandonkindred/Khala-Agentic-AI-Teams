export interface NutritionHealthResponse {
  status: string;
  team?: string;
}

export interface NutritionHouseholdMember {
  name: string;
  age_or_role: string;
  dietary_needs: string[];
  allergies: string[];
  notes: string;
}

export interface ClientProfile {
  client_id: string;
  household: {
    number_of_people: number;
    description: string;
    ages_if_relevant: string[];
    /** Per-person rows when returned by API (may be absent on older payloads). */
    members?: NutritionHouseholdMember[];
  };
  dietary_needs: string[];
  allergies_and_intolerances: string[];
  lifestyle: {
    max_cooking_time_minutes?: number | null;
    lunch_context: string;
    equipment_constraints: string[];
    other_constraints: string;
  };
  preferences: {
    cuisines_liked: string[];
    cuisines_disliked: string[];
    ingredients_disliked: string[];
    preferences_free_text: string;
  };
  goals: {
    goal_type: string;
    notes: string;
  };
  updated_at?: string | null;
}

export interface NutritionProfileUpdateRequest {
  household?: {
    number_of_people: number;
    description: string;
    ages_if_relevant: string[];
    members?: NutritionHouseholdMember[];
  };
  dietary_needs?: string[];
  allergies_and_intolerances?: string[];
  lifestyle?: {
    max_cooking_time_minutes?: number | null;
    lunch_context: string;
    equipment_constraints: string[];
    other_constraints: string;
  };
  preferences?: {
    cuisines_liked: string[];
    cuisines_disliked: string[];
    ingredients_disliked: string[];
    preferences_free_text: string;
  };
  goals?: {
    goal_type: string;
    notes: string;
  };
}

export interface NutritionPlanResponse {
  client_id: string;
  plan: {
    daily_targets: {
      calories_kcal?: number | null;
      protein_g?: number | null;
      carbs_g?: number | null;
      fat_g?: number | null;
      fiber_g?: number | null;
      sodium_mg?: number | null;
      other_nutrients: Record<string, number>;
    };
    balance_guidelines: string[];
    foods_to_emphasize: string[];
    foods_to_avoid: string[];
    notes: string;
    generated_at?: string | null;
  };
}

export interface MealRecommendation {
  recommendation_id: string;
  name: string;
  ingredients: string[];
  portions_servings: string;
  prep_time_minutes?: number | null;
  cook_time_minutes?: number | null;
  rationale: string;
  meal_type: string;
  suggested_date?: string | null;
}

export interface MealPlanResponse {
  client_id: string;
  suggestions: MealRecommendation[];
}

export interface FeedbackResponse {
  recommendation_id: string;
  recorded: boolean;
}

// --- Chat models ---

export interface NutritionChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  /** Structured data attached to assistant messages after actions. */
  profile?: ClientProfile | null;
  nutritionPlan?: NutritionPlanResponse | null;
  mealSuggestions?: MealRecommendation[];
  phase?: string;
  action?: string;
}

export interface NutritionChatRequest {
  client_id: string;
  message: string;
  conversation_history: { role: string; content: string }[];
}

export interface NutritionChatResponse {
  message: string;
  phase: string;
  action: string;
  profile?: ClientProfile | null;
  nutrition_plan?: NutritionPlanResponse['plan'] | null;
  meal_suggestions?: MealRecommendation[];
  feedback_recorded?: boolean;
}

export interface MealHistoryResponse {
  client_id: string;
  entries: {
    recommendation_id: string;
    client_id: string;
    meal_snapshot: Record<string, unknown>;
    recommended_at?: string | null;
    feedback?: {
      recommendation_id: string;
      rating?: number | null;
      would_make_again?: boolean | null;
      notes: string;
      submitted_at?: string | null;
    } | null;
  }[];
}
