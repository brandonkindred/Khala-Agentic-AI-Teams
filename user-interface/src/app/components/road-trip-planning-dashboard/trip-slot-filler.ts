/**
 * Client-side conversational slot filler for the Road Trip Planning team.
 *
 * The backend exposes a one-shot planner — there is no chat endpoint.
 * This module provides the rule-based conversational layer: given the
 * current trip context and a free-text user message, it infers which
 * slot is being updated, applies the edit, and decides what to ask next.
 *
 * Kept as pure functions so the dashboard component stays thin and so
 * the logic is easy to unit-test.
 */

import type {
  AgeGroup,
  BudgetLevel,
  ChatMessage,
  Traveler,
  TripRequest,
  TripSlotKey,
  VehicleType,
} from '../../models';
import { DEFAULT_TRIP, REQUIRED_SLOTS, TRIP_CONTEXT_SCHEMA } from '../../models';

// ---------------------------------------------------------------------------
// Intent detection
// ---------------------------------------------------------------------------

export type SlotIntent =
  | { kind: 'fill'; slot: TripSlotKey; rawValue: string }
  | { kind: 'add_stop'; value: string }
  | { kind: 'remove_stop'; value: string }
  | { kind: 'add_traveler'; traveler: Partial<Traveler> }
  | { kind: 'remove_traveler'; name: string }
  | { kind: 'add_preference'; value: string }
  | { kind: 'clear_preferences' }
  | { kind: 'restart' }
  | { kind: 'plan_now' }
  | { kind: 'unknown' };

/** Detect the user's intent from a free-text message + the pending slot. */
export function detectIntent(message: string, pendingSlot: TripSlotKey | null): SlotIntent {
  const text = message.trim();
  const lower = text.toLowerCase();

  if (/^(start over|restart|reset|clear( all)?)\b/.test(lower)) {
    return { kind: 'restart' };
  }
  if (/^(plan|plan it|plan now|generate|go|build( it)?|make( the)? trip)\b/.test(lower)) {
    return { kind: 'plan_now' };
  }

  // Explicit "add/remove a stop" verbs anywhere in the message.
  const addStop = lower.match(/\b(?:add|include|stop (?:in|at)|visit|also (?:go|stop|visit))\s+(?:a\s+stop\s+(?:at|in)\s+)?([^.,]+?)(?:\s+(?:to|on|in)\s+(?:the\s+)?(?:trip|route|itinerary))?\s*$/);
  if (addStop && !pendingSlot) {
    const v = addStop[1].replace(/^as (a|an) (stop|visit)/i, '').trim();
    if (v) return { kind: 'add_stop', value: titleCasePlace(v) };
  }
  const removeStop = lower.match(/\b(?:skip|drop|remove|cancel)\s+(?:the\s+)?(?:stop\s+(?:at|in)\s+)?([^.,]+)$/);
  if (removeStop) {
    const v = removeStop[1].trim();
    if (v && !['dates', 'date', 'everything', 'all of it'].includes(v)) {
      return { kind: 'remove_stop', value: titleCasePlace(v) };
    }
  }

  // Preferences / style vocabulary
  const prefMatch = lower.match(/\b(?:prefer|i want|we want|more|less|avoid|no|stick to)\s+(.+)$/);
  if (prefMatch && !pendingSlot) {
    return { kind: 'add_preference', value: text };
  }

  // If a slot is pending, treat the message as the answer to that slot.
  if (pendingSlot) {
    return { kind: 'fill', slot: pendingSlot, rawValue: text };
  }

  // Heuristic guesses when nothing is pending.
  if (/\b\d+\s*(days?|nights?|weeks?)\b/.test(lower)) {
    return { kind: 'fill', slot: 'trip_duration_days', rawValue: text };
  }
  if (/\b(rv|suv|van|motorcycle|car)\b/.test(lower) && !/\bcar(eful|ry)/.test(lower)) {
    return { kind: 'fill', slot: 'vehicle_type', rawValue: text };
  }
  if (/\b(budget|moderate|mid-?range|luxury|cheap|fancy)\b/.test(lower)) {
    return { kind: 'fill', slot: 'budget_level', rawValue: text };
  }
  return { kind: 'unknown' };
}

// ---------------------------------------------------------------------------
// Value parsers
// ---------------------------------------------------------------------------

/**
 * Outcome of trying to parse a user answer into a slot.
 *
 * - `accepted` — we stored a real value and should move on.
 * - `declined` — user explicitly opted out (e.g. "skip for now", "flexible",
 *   "I'm done"). The slot stays empty and the picker won't ask again.
 * - neither  — answer was unintelligible; caller should re-prompt.
 */
export interface ParseResult {
  trip: TripRequest;
  accepted: boolean;
  declined: boolean;
}

/**
 * Per-slot matcher for "I'd rather skip this" phrases. Keyed separately
 * from the parsers so the decline vocabulary can evolve independently.
 */
function isDeclineForSlot(slot: TripSlotKey, raw: string): boolean {
  const v = raw.trim().toLowerCase();
  if (!v) return false;
  switch (slot) {
    case 'end_location':
      return /^(round ?trip|same|back|loop|no|none|still deciding|not sure|don'?t know)\b/.test(v);
    case 'trip_duration_days':
      return /^(flexible|not sure|don'?t know|tbd|whatever|we'?ll see)\b/.test(v);
    case 'travel_start_date':
      return /^(flexible|not sure|don'?t know|tbd|whenever|we'?ll see)\b/.test(v);
    case 'required_stops':
      return /^(none(\s+specific|\s+in\s+particular|\s+yet)?|skip(\s+for\s+now)?|no(\s+specific(\s+stops)?)?|nothing|not\s+sure|we'?ll\s+see)\b/.test(v);
    case 'preferences':
      return /^(i'?m\s+done|done|we'?re\s+good|that'?s\s+all|that'?s it|nothing( else)?|no( more| thanks)?|skip|none)\b/.test(v);
    case 'start_location':
    case 'travelers':
    case 'vehicle_type':
    case 'budget_level':
      return false; // required (or always has a default) — never decline
  }
}

export function parseSlotValue(
  slot: TripSlotKey,
  raw: string,
  current: TripRequest,
): ParseResult {
  const value = raw.trim();
  const trip: TripRequest = { ...current };

  // Decline phrases short-circuit the parser for every slot where they
  // apply. This is what keeps "None specific" from becoming a literal stop
  // and "I'm done" from becoming a preference string.
  if (isDeclineForSlot(slot, value)) {
    return { trip, accepted: false, declined: true };
  }

  switch (slot) {
    case 'start_location':
      if (!value) return { trip, accepted: false, declined: false };
      trip.start_location = titleCasePlace(value);
      return { trip, accepted: true, declined: false };

    case 'end_location':
      // Decline cases ("round trip" etc.) are handled above; any remaining
      // value is a real destination.
      if (!value) return { trip, accepted: false, declined: false };
      trip.end_location = titleCasePlace(value);
      return { trip, accepted: true, declined: false };

    case 'trip_duration_days': {
      const m = value.match(/(\d+)\s*(day|night|week)?/i);
      if (!m) return { trip, accepted: false, declined: false };
      const n = parseInt(m[1], 10);
      const unit = (m[2] || 'day').toLowerCase();
      trip.trip_duration_days = unit.startsWith('week') ? n * 7 : n;
      return { trip, accepted: true, declined: false };
    }

    case 'travel_start_date': {
      const d = parseDate(value);
      if (d === null) return { trip, accepted: false, declined: false };
      trip.travel_start_date = d;
      return { trip, accepted: true, declined: false };
    }

    case 'travelers': {
      const parsed = parseTravelers(value);
      if (parsed.length === 0) return { trip, accepted: false, declined: false };
      trip.travelers = parsed;
      return { trip, accepted: true, declined: false };
    }

    case 'required_stops': {
      const stops = value
        .split(/[,;]|\band\b/)
        .map((s) => titleCasePlace(s.trim()))
        .filter(Boolean);
      if (stops.length === 0) return { trip, accepted: false, declined: false };
      trip.required_stops = stops;
      return { trip, accepted: true, declined: false };
    }

    case 'vehicle_type':
      trip.vehicle_type = parseVehicle(value);
      return { trip, accepted: true, declined: false };

    case 'budget_level':
      trip.budget_level = parseBudget(value);
      return { trip, accepted: true, declined: false };

    case 'preferences':
      if (!value) return { trip, accepted: false, declined: false };
      trip.preferences = [...trip.preferences, value];
      return { trip, accepted: true, declined: false };
  }
}

function titleCasePlace(s: string): string {
  return s
    .trim()
    .replace(/\s+/g, ' ')
    .split(' ')
    .map((w) => (w.length <= 2 ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(' ');
}

function parseDate(value: string): string | null {
  if (!value) return null;
  // Accept ISO or natural-ish dates — Date.parse is lenient enough here.
  const t = Date.parse(value);
  if (!Number.isNaN(t)) {
    return new Date(t).toISOString().slice(0, 10);
  }
  return null;
}

function parseVehicle(value: string): VehicleType {
  const v = value.toLowerCase();
  if (v.includes('rv') || v.includes('motorhome')) return 'rv';
  if (v.includes('suv')) return 'suv';
  if (v.includes('van')) return 'van';
  if (v.includes('motor') || v.includes('bike')) return 'motorcycle';
  return 'car';
}

function parseBudget(value: string): BudgetLevel {
  const v = value.toLowerCase();
  if (v.includes('lux') || v.includes('fancy') || v.includes('splurge')) return 'luxury';
  if (v.includes('cheap') || v.includes('budget') || v.includes('shoestring')) return 'budget';
  return 'moderate';
}

function parseTravelers(value: string): Traveler[] {
  // "2 adults and 1 kid" / "me and my partner" / "Sarah (vegetarian) + Jake (hiker)"
  const travelers: Traveler[] = [];
  const parts = value.split(/,|;|\band\b|\+/i).map((p) => p.trim()).filter(Boolean);

  for (const part of parts) {
    const countMatch = part.match(/^(\d+)\s*(adults?|kids?|child(?:ren)?|teens?|seniors?)/i);
    if (countMatch) {
      const n = parseInt(countMatch[1], 10);
      const group = countMatch[2].toLowerCase();
      const ageGroup: AgeGroup = group.startsWith('kid') || group.startsWith('child')
        ? 'child'
        : group.startsWith('teen')
          ? 'teen'
          : group.startsWith('senior')
            ? 'senior'
            : 'adult';
      for (let i = 0; i < n; i++) {
        travelers.push(makeTraveler('', ageGroup));
      }
      continue;
    }

    // "Sarah (vegetarian, hiker)" style
    const parenMatch = part.match(/^([^\s(]+)\s*\(([^)]+)\)/);
    if (parenMatch) {
      const name = parenMatch[1].replace(/^(me|myself)$/i, 'You');
      const traits = parenMatch[2].split(/[,/]/).map((t) => t.trim()).filter(Boolean);
      const needs = traits.filter((t) =>
        /vegetarian|vegan|wheelchair|pet|allergy|allergic|mobility|kosher|halal|gluten/i.test(t),
      );
      const interests = traits.filter((t) => !needs.includes(t));
      travelers.push({
        name,
        age_group: 'adult',
        interests,
        needs,
        notes: '',
      });
      continue;
    }

    // Bare name or pronoun
    if (/^(me|myself|i)$/i.test(part)) {
      travelers.push(makeTraveler('You', 'adult'));
    } else if (/^(my )?(wife|husband|partner|spouse|girlfriend|boyfriend)/i.test(part)) {
      travelers.push(makeTraveler('Partner', 'adult'));
    } else if (part.length > 0 && part.length < 60) {
      travelers.push(makeTraveler(titleCasePlace(part), 'adult'));
    }
  }

  // Failsafe — if nothing parsed, record a single anonymous adult so the
  // trip passes backend validation.
  if (travelers.length === 0 && value.length > 0) {
    travelers.push(makeTraveler('', 'adult'));
  }
  return travelers;
}

function makeTraveler(name: string, ageGroup: AgeGroup): Traveler {
  return { name, age_group: ageGroup, interests: [], needs: [], notes: '' };
}

// ---------------------------------------------------------------------------
// Conversation orchestration
// ---------------------------------------------------------------------------

const SLOT_PROMPTS: Record<TripSlotKey, { question: string; chips?: string[] }> = {
  start_location: {
    question: "Great — where are you starting from? (a city, address, or landmark works)",
  },
  end_location: {
    question: "Where do you want to end up? If it's a round trip, just say 'round trip.'",
    chips: ['Round trip', 'Still deciding'],
  },
  trip_duration_days: {
    question: 'How long do you have for this trip?',
    chips: ['3 days', '1 week', '10 days', '2 weeks'],
  },
  travel_start_date: {
    question: 'When do you want to leave? (a date, or "flexible" is fine)',
    chips: ['Flexible'],
  },
  travelers: {
    question:
      "Who's coming along? You can describe them freely — e.g. \"2 adults and a kid\" or \"Sarah (hiker), Jake (foodie)\".",
  },
  required_stops: {
    question: 'Any must-see stops along the way? Comma-separated is fine.',
    chips: ['None specific', 'Skip for now'],
  },
  vehicle_type: {
    question: 'What are you driving?',
    chips: ['Car', 'SUV', 'RV', 'Van', 'Motorcycle'],
  },
  budget_level: {
    question: 'What feels like the right budget vibe?',
    chips: ['Budget', 'Moderate', 'Luxury'],
  },
  preferences: {
    question: 'Any preferences I should know? (scenic routes, avoid highways, pet-friendly, etc.)',
    chips: ['Scenic routes', 'Avoid highways', 'Pet-friendly', "I'm done"],
  },
};

const GREETING: ChatMessage = {
  role: 'assistant',
  content:
    "Hi! I'm your road trip planner. Tell me a bit about the trip and I'll put together a day-by-day itinerary — and re-work it anytime you change your mind.",
  timestamp: new Date().toISOString(),
  quickReplies: ['Start from scratch', 'Surprise me'],
};

/**
 * Pick the next slot to ask about, preferring required then useful fields.
 * Slots the user has explicitly declined are skipped so the chat doesn't
 * loop on answered-but-empty optional fields.
 */
export function pickNextSlot(
  trip: TripRequest,
  declined: ReadonlySet<TripSlotKey> = new Set(),
): TripSlotKey | null {
  const isAskable = (key: TripSlotKey) => isSlotEmpty(trip, key) && !declined.has(key);
  for (const key of REQUIRED_SLOTS) {
    if (isAskable(key)) return key;
  }
  const order: TripSlotKey[] = [
    'trip_duration_days',
    'travel_start_date',
    'end_location',
    'required_stops',
    'vehicle_type',
    'budget_level',
    'preferences',
  ];
  for (const key of order) {
    if (isAskable(key)) return key;
  }
  return null;
}

export function isSlotEmpty(trip: TripRequest, slot: TripSlotKey): boolean {
  switch (slot) {
    case 'start_location':
      return !trip.start_location;
    case 'end_location':
      return trip.end_location === null;
    case 'trip_duration_days':
      return trip.trip_duration_days === null;
    case 'travel_start_date':
      return trip.travel_start_date === null;
    case 'travelers':
      return trip.travelers.length === 0;
    case 'required_stops':
      return trip.required_stops.length === 0;
    case 'vehicle_type':
      return trip.vehicle_type === 'car';
    case 'budget_level':
      return false; // always has a default — don't nag
    case 'preferences':
      return trip.preferences.length === 0;
  }
}

export function assistantMessage(text: string, chips?: string[]): ChatMessage {
  return {
    role: 'assistant',
    content: text,
    timestamp: new Date().toISOString(),
    quickReplies: chips,
  };
}

export function userMessage(text: string): ChatMessage {
  return { role: 'user', content: text, timestamp: new Date().toISOString() };
}

export function promptForSlot(slot: TripSlotKey): ChatMessage {
  const p = SLOT_PROMPTS[slot];
  return assistantMessage(p.question, p.chips);
}

export function initialGreeting(): ChatMessage {
  return { ...GREETING, timestamp: new Date().toISOString() };
}

export function freshTrip(): TripRequest {
  return {
    ...DEFAULT_TRIP,
    required_stops: [],
    travelers: [],
    preferences: [],
  };
}

// ---------------------------------------------------------------------------
// Readability helpers for the context panel
// ---------------------------------------------------------------------------

export function readinessSummary(trip: TripRequest): {
  ready: boolean;
  missing: TripSlotKey[];
} {
  const missing = REQUIRED_SLOTS.filter((k) => isSlotEmpty(trip, k));
  return { ready: missing.length === 0, missing: [...missing] };
}

export function displayValueFor(slot: TripSlotKey, trip: TripRequest): string | null {
  switch (slot) {
    case 'start_location':
      return trip.start_location || null;
    case 'end_location':
      return trip.end_location ?? (trip.start_location ? 'Round trip' : null);
    case 'trip_duration_days':
      return trip.trip_duration_days ? `${trip.trip_duration_days} days` : null;
    case 'travel_start_date':
      return trip.travel_start_date;
    case 'travelers':
      if (trip.travelers.length === 0) return null;
      return summariseTravelers(trip.travelers);
    case 'required_stops':
      return trip.required_stops.length === 0 ? null : trip.required_stops.join(', ');
    case 'vehicle_type':
      return capitalise(trip.vehicle_type);
    case 'budget_level':
      return capitalise(trip.budget_level);
    case 'preferences':
      return trip.preferences.length === 0 ? null : trip.preferences.join('; ');
  }
}

function summariseTravelers(travelers: Traveler[]): string {
  const byGroup: Record<string, number> = {};
  let named = 0;
  for (const t of travelers) {
    if (t.name && t.name !== 'You' && t.name !== 'Partner') named += 1;
    byGroup[t.age_group] = (byGroup[t.age_group] ?? 0) + 1;
  }
  const parts = Object.entries(byGroup).map(([g, n]) =>
    n === 1 ? `1 ${g}` : `${n} ${g}s`,
  );
  if (named > 0) {
    parts.push(`${named} named`);
  }
  return parts.join(', ');
}

function capitalise(s: string): string {
  return s.length ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

export const CONTEXT_SCHEMA = TRIP_CONTEXT_SCHEMA;
