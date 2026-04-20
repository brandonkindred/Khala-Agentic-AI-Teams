/**
 * Models for the Road Trip Planning team UI.
 *
 * Mirrors backend/agents/road_trip_planning_team/models.py on the request
 * and itinerary side. Adds frontend-only types for the client-orchestrated
 * chat (slot filling) and async job polling.
 */

// ---------------------------------------------------------------------------
// Backend wire types (mirror Pydantic models)
// ---------------------------------------------------------------------------

export type AgeGroup = 'child' | 'teen' | 'adult' | 'senior';
export type BudgetLevel = 'budget' | 'moderate' | 'luxury';
export type VehicleType = 'car' | 'suv' | 'rv' | 'motorcycle' | 'van';

export interface Traveler {
  name: string;
  age_group: AgeGroup;
  interests: string[];
  needs: string[];
  notes: string;
}

export interface TripRequest {
  start_location: string;
  required_stops: string[];
  end_location: string | null;
  travelers: Traveler[];
  trip_duration_days: number | null;
  budget_level: BudgetLevel;
  travel_start_date: string | null;
  vehicle_type: VehicleType;
  preferences: string[];
}

export interface PlanTripRequestBody {
  trip: TripRequest;
}

export interface Activity {
  name: string;
  description: string;
  duration_hours: number | null;
  activity_type: string;
  address: string | null;
  tips: string[];
  good_for: string[];
  approximate_cost: string | null;
}

export interface Accommodation {
  name: string;
  accommodation_type: string;
  address: string | null;
  approximate_cost_per_night: string | null;
  amenities: string[];
  booking_tips: string;
}

export interface DayPlan {
  day_number: number;
  date: string | null;
  location: string;
  driving_from: string | null;
  driving_distance_miles: number | null;
  driving_time_hours: number | null;
  driving_notes: string;
  morning_activities: Activity[];
  afternoon_activities: Activity[];
  evening_activities: Activity[];
  meals: Activity[];
  accommodation: Accommodation | null;
  day_summary: string;
  day_tips: string[];
}

export interface TripItinerary {
  title: string;
  overview: string;
  total_days: number;
  total_driving_miles: number | null;
  route_summary: string[];
  traveler_highlights: string;
  days: DayPlan[];
  travel_tips: string[];
  packing_suggestions: string[];
  budget_estimate: string;
  generated_at: string | null;
}

// Async job wire types (GET /jobs/{id})
export type JobStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface PlanJobSubmission {
  job_id: string;
  status: JobStatus;
}

export interface PlanJob {
  job_id: string;
  status: JobStatus;
  result?: TripItinerary;
  error?: string | null;
  request?: PlanTripRequestBody;
  created_at?: string;
  updated_at?: string;
}

// ---------------------------------------------------------------------------
// Client-side chat + slot-filling types
// ---------------------------------------------------------------------------

export type ChatRole = 'user' | 'assistant' | 'system';

export interface ChatMessage {
  role: ChatRole;
  content: string;
  timestamp: string;
  /** Quick-action chips the assistant offers after this message. */
  quickReplies?: string[];
}

/** Canonical keys the slot filler targets, in the order it asks for them. */
export type TripSlotKey =
  | 'start_location'
  | 'end_location'
  | 'trip_duration_days'
  | 'travel_start_date'
  | 'travelers'
  | 'required_stops'
  | 'vehicle_type'
  | 'budget_level'
  | 'preferences';

/** A single entry in the side-panel "Trip so far" view. */
export interface TripContextField {
  key: TripSlotKey;
  label: string;
  /** Display-ready value, or `null` when the slot is still empty. */
  displayValue: string | null;
  required: boolean;
}

/** Persisted snapshot stored in localStorage so refreshes don't wipe work. */
export interface RoadTripSessionState {
  trip: TripRequest;
  messages: ChatMessage[];
  /** Slot the assistant is currently awaiting an answer for. */
  pendingSlot: TripSlotKey | null;
  /** Most recent itinerary (if generated). */
  itinerary: TripItinerary | null;
  /** Up to 3 previous itineraries so the user can diff / revert after a re-plan. */
  previousItineraries: TripItinerary[];
  /** True when the trip context has changed since `itinerary` was produced. */
  dirtyRePlan: boolean;
  /**
   * Slot keys the user has explicitly declined (e.g. "skip for now",
   * "flexible", "I'm done"). These are skipped by the slot picker so
   * the chat doesn't re-ask a question the user already dismissed.
   */
  declinedSlots: TripSlotKey[];
}

export const DEFAULT_TRIP: TripRequest = {
  start_location: '',
  required_stops: [],
  end_location: null,
  travelers: [],
  trip_duration_days: null,
  budget_level: 'moderate',
  travel_start_date: null,
  vehicle_type: 'car',
  preferences: [],
};

/** Order + labels for the context panel. */
export const TRIP_CONTEXT_SCHEMA: readonly {
  key: TripSlotKey;
  label: string;
  required: boolean;
}[] = [
  { key: 'start_location', label: 'Start', required: true },
  { key: 'end_location', label: 'End', required: false },
  { key: 'trip_duration_days', label: 'Duration', required: false },
  { key: 'travel_start_date', label: 'Start date', required: false },
  { key: 'travelers', label: 'Travelers', required: true },
  { key: 'required_stops', label: 'Stops', required: false },
  { key: 'vehicle_type', label: 'Vehicle', required: false },
  { key: 'budget_level', label: 'Budget', required: false },
  { key: 'preferences', label: 'Preferences', required: false },
];

/** Minimum slots that must be filled before the /plan call is valid. */
export const REQUIRED_SLOTS: readonly TripSlotKey[] = [
  'start_location',
  'travelers',
];
