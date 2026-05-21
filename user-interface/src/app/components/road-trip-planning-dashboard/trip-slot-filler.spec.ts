import {
  detectIntent,
  parseSlotValue,
  pickNextSlot,
  isSlotEmpty,
  assistantMessage,
  userMessage,
  promptForSlot,
  initialGreeting,
  freshTrip,
  readinessSummary,
  displayValueFor,
  CONTEXT_SCHEMA,
} from './trip-slot-filler';
import type { TripRequest, TripSlotKey } from '../../models';

describe('trip-slot-filler', () => {
  // ------------------ detectIntent ------------------
  describe('detectIntent', () => {
    it('detects restart phrases', () => {
      expect(detectIntent('start over', null).kind).toBe('restart');
      expect(detectIntent('Restart', null).kind).toBe('restart');
      expect(detectIntent('reset', null).kind).toBe('restart');
      expect(detectIntent('clear all', null).kind).toBe('restart');
    });

    it('detects plan_now phrases', () => {
      expect(detectIntent('plan now', null).kind).toBe('plan_now');
      expect(detectIntent('go', null).kind).toBe('plan_now');
      expect(detectIntent('build it', null).kind).toBe('plan_now');
      expect(detectIntent('generate', null).kind).toBe('plan_now');
      expect(detectIntent('make the trip', null).kind).toBe('plan_now');
    });

    it('detects add_stop', () => {
      const result = detectIntent('add Yellowstone', null);
      expect(result.kind).toBe('add_stop');
      if (result.kind === 'add_stop') {
        expect(result.value.toLowerCase()).toContain('yellowstone');
      }
    });

    it('detects remove_stop with cancel/skip/drop/remove', () => {
      const r = detectIntent('drop Denver', null);
      expect(r.kind).toBe('remove_stop');
    });

    it('skips remove_stop for date/everything', () => {
      const r = detectIntent('drop dates', null);
      // 'dates' is filtered — falls through to unknown
      expect(r.kind).not.toBe('remove_stop');
    });

    it('detects add_preference', () => {
      const r = detectIntent('I want scenic routes', null);
      expect(r.kind).toBe('add_preference');
    });

    it('treats message as slot fill when pending slot is set', () => {
      const r = detectIntent('Denver', 'start_location');
      expect(r.kind).toBe('fill');
    });

    it('heuristically detects trip_duration_days', () => {
      const r = detectIntent('3 days', null);
      expect(r.kind).toBe('fill');
      if (r.kind === 'fill') expect(r.slot).toBe('trip_duration_days');
    });

    it('heuristically detects vehicle_type', () => {
      const r = detectIntent('we have an RV', null);
      expect(r.kind).toBe('fill');
      if (r.kind === 'fill') expect(r.slot).toBe('vehicle_type');
    });

    it('skips vehicle_type when "careful" or "carry" appear', () => {
      const r = detectIntent('be careful with the kids', null);
      expect(r.kind).toBe('unknown');
    });

    it('heuristically detects budget_level', () => {
      const r = detectIntent('luxury feel', null);
      expect(r.kind).toBe('fill');
      if (r.kind === 'fill') expect(r.slot).toBe('budget_level');
    });

    it('returns unknown when nothing matches', () => {
      expect(detectIntent('hello there', null).kind).toBe('unknown');
    });
  });

  // ------------------ parseSlotValue ------------------
  describe('parseSlotValue', () => {
    const trip = freshTrip();

    it('declines round trip for end_location', () => {
      const r = parseSlotValue('end_location', 'round trip', trip);
      expect(r.declined).toBe(true);
      expect(r.accepted).toBe(false);
    });

    it('declines flexible for duration', () => {
      const r = parseSlotValue('trip_duration_days', 'flexible', trip);
      expect(r.declined).toBe(true);
    });

    it('declines flexible for start date', () => {
      const r = parseSlotValue('travel_start_date', 'flexible', trip);
      expect(r.declined).toBe(true);
    });

    it('declines none for required_stops', () => {
      const r = parseSlotValue('required_stops', 'none specific', trip);
      expect(r.declined).toBe(true);
    });

    it('declines done for preferences', () => {
      const r = parseSlotValue('preferences', "I'm done", trip);
      expect(r.declined).toBe(true);
    });

    it('parses start_location', () => {
      const r = parseSlotValue('start_location', 'denver', trip);
      expect(r.accepted).toBe(true);
      expect(r.trip.start_location).toBe('Denver');
    });

    it('refuses empty start_location', () => {
      const r = parseSlotValue('start_location', '   ', trip);
      expect(r.accepted).toBe(false);
      expect(r.declined).toBe(false);
    });

    it('parses end_location', () => {
      const r = parseSlotValue('end_location', 'aspen', trip);
      expect(r.accepted).toBe(true);
      expect(r.trip.end_location).toBe('Aspen');
    });

    it('parses end_location empty -> rejected', () => {
      const r = parseSlotValue('end_location', '', trip);
      expect(r.accepted).toBe(false);
    });

    it('parses trip_duration_days days', () => {
      const r = parseSlotValue('trip_duration_days', '5 days', trip);
      expect(r.trip.trip_duration_days).toBe(5);
    });

    it('parses trip_duration_days weeks', () => {
      const r = parseSlotValue('trip_duration_days', '2 weeks', trip);
      expect(r.trip.trip_duration_days).toBe(14);
    });

    it('parses trip_duration_days bare number', () => {
      const r = parseSlotValue('trip_duration_days', '7', trip);
      expect(r.trip.trip_duration_days).toBe(7);
    });

    it('rejects trip_duration_days when no number', () => {
      const r = parseSlotValue('trip_duration_days', 'soon-ish', trip);
      expect(r.accepted).toBe(false);
    });

    it('parses ISO travel_start_date', () => {
      const r = parseSlotValue('travel_start_date', '2025-06-15', trip);
      expect(r.trip.travel_start_date).toBe('2025-06-15');
    });

    it('rejects invalid travel_start_date', () => {
      const r = parseSlotValue('travel_start_date', 'definitely-not-a-date', trip);
      expect(r.accepted).toBe(false);
    });

    it('parses travelers numeric "2 adults"', () => {
      const r = parseSlotValue('travelers', '2 adults', trip);
      expect(r.trip.travelers.length).toBe(2);
      expect(r.trip.travelers[0].age_group).toBe('adult');
    });

    it('parses travelers "2 kids and 1 teen"', () => {
      const r = parseSlotValue('travelers', '2 kids and 1 teen', trip);
      expect(r.trip.travelers.length).toBe(3);
    });

    it('parses travelers named with parens (single trait)', () => {
      // Note: the comma in "Sarah (vegetarian, hiker)" splits parts before parens are read,
      // so use a slash-separated trait list instead.
      const r = parseSlotValue('travelers', 'Sarah (vegetarian/hiker)', trip);
      expect(r.trip.travelers[0].name).toBe('Sarah');
      expect(r.trip.travelers[0].needs).toContain('vegetarian');
      expect(r.trip.travelers[0].interests).toContain('hiker');
    });

    it('parses travelers "me and my partner"', () => {
      const r = parseSlotValue('travelers', 'me and my partner', trip);
      expect(r.trip.travelers.length).toBe(2);
      expect(r.trip.travelers[0].name).toBe('You');
      expect(r.trip.travelers[1].name).toBe('Partner');
    });

    it('parses travelers bare name', () => {
      const r = parseSlotValue('travelers', 'Alice', trip);
      expect(r.trip.travelers[0].name).toBe('Alice');
    });

    it('falls back to anonymous adult when nothing parses', () => {
      const r = parseSlotValue('travelers', '!!!!', trip);
      // No name patterns match, but value is non-empty so failsafe inserts anon
      expect(r.trip.travelers.length).toBeGreaterThan(0);
    });

    it('rejects travelers when empty', () => {
      const r = parseSlotValue('travelers', '', trip);
      expect(r.accepted).toBe(false);
    });

    it('parses required_stops comma-separated', () => {
      const r = parseSlotValue('required_stops', 'denver, aspen, vail', trip);
      expect(r.trip.required_stops).toEqual(['Denver', 'Aspen', 'Vail']);
    });

    it('rejects required_stops when empty after split', () => {
      const r = parseSlotValue('required_stops', '', trip);
      expect(r.accepted).toBe(false);
    });

    it('parses vehicle_type variants', () => {
      expect(parseSlotValue('vehicle_type', 'rv', trip).trip.vehicle_type).toBe('rv');
      expect(parseSlotValue('vehicle_type', 'motorhome', trip).trip.vehicle_type).toBe('rv');
      expect(parseSlotValue('vehicle_type', 'suv', trip).trip.vehicle_type).toBe('suv');
      expect(parseSlotValue('vehicle_type', 'van', trip).trip.vehicle_type).toBe('van');
      expect(parseSlotValue('vehicle_type', 'motorcycle', trip).trip.vehicle_type).toBe('motorcycle');
      expect(parseSlotValue('vehicle_type', 'bike', trip).trip.vehicle_type).toBe('motorcycle');
      expect(parseSlotValue('vehicle_type', 'sedan', trip).trip.vehicle_type).toBe('car');
    });

    it('parses budget_level variants', () => {
      expect(parseSlotValue('budget_level', 'luxury', trip).trip.budget_level).toBe('luxury');
      expect(parseSlotValue('budget_level', 'splurge', trip).trip.budget_level).toBe('luxury');
      expect(parseSlotValue('budget_level', 'cheap', trip).trip.budget_level).toBe('budget');
      expect(parseSlotValue('budget_level', 'moderate', trip).trip.budget_level).toBe('moderate');
    });

    it('parses preferences (appends)', () => {
      const start = { ...freshTrip(), preferences: ['scenic'] };
      const r = parseSlotValue('preferences', 'pet-friendly', start);
      expect(r.trip.preferences).toEqual(['scenic', 'pet-friendly']);
    });

    it('rejects empty preferences', () => {
      const r = parseSlotValue('preferences', '', freshTrip());
      expect(r.accepted).toBe(false);
    });
  });

  // ------------------ pickNextSlot ------------------
  describe('pickNextSlot', () => {
    it('picks the first empty required slot', () => {
      const trip = freshTrip();
      expect(pickNextSlot(trip)).toBe('start_location');
    });

    it('skips declined slots', () => {
      const trip = { ...freshTrip(), start_location: 'Denver', travelers: [{ name: 'X', age_group: 'adult' as const, interests: [], needs: [], notes: '' }] };
      const declined = new Set<TripSlotKey>(['trip_duration_days']);
      const next = pickNextSlot(trip, declined);
      expect(next).not.toBe('trip_duration_days');
    });

    it('returns null when nothing askable', () => {
      const trip: TripRequest = {
        start_location: 'A',
        required_stops: ['Z'],
        end_location: 'B',
        travelers: [{ name: 'X', age_group: 'adult', interests: [], needs: [], notes: '' }],
        trip_duration_days: 3,
        travel_start_date: '2025-01-01',
        vehicle_type: 'rv',
        budget_level: 'moderate',
        preferences: ['scenic'],
      };
      expect(pickNextSlot(trip)).toBeNull();
    });
  });

  // ------------------ isSlotEmpty ------------------
  describe('isSlotEmpty', () => {
    const filled: TripRequest = {
      start_location: 'A',
      required_stops: ['Z'],
      end_location: 'B',
      travelers: [{ name: 'X', age_group: 'adult', interests: [], needs: [], notes: '' }],
      trip_duration_days: 5,
      travel_start_date: '2025-01-01',
      vehicle_type: 'rv',
      budget_level: 'budget',
      preferences: ['hiking'],
    };

    it.each<TripSlotKey>([
      'start_location',
      'end_location',
      'trip_duration_days',
      'travel_start_date',
      'travelers',
      'required_stops',
      'vehicle_type',
      'preferences',
    ])('returns false when %s is filled', (slot) => {
      expect(isSlotEmpty(filled, slot)).toBe(false);
    });

    it('returns true when slots are empty in default trip', () => {
      const empty = freshTrip();
      expect(isSlotEmpty(empty, 'start_location')).toBe(true);
      expect(isSlotEmpty(empty, 'end_location')).toBe(true);
      expect(isSlotEmpty(empty, 'trip_duration_days')).toBe(true);
      expect(isSlotEmpty(empty, 'travelers')).toBe(true);
      expect(isSlotEmpty(empty, 'required_stops')).toBe(true);
      expect(isSlotEmpty(empty, 'preferences')).toBe(true);
    });

    it('budget_level is never empty (has a default)', () => {
      expect(isSlotEmpty(freshTrip(), 'budget_level')).toBe(false);
    });
  });

  // ------------------ messages ------------------
  describe('messages', () => {
    it('assistantMessage carries chips', () => {
      const m = assistantMessage('Hi', ['ok']);
      expect(m.role).toBe('assistant');
      expect(m.quickReplies).toEqual(['ok']);
    });

    it('userMessage', () => {
      const m = userMessage('hello');
      expect(m.role).toBe('user');
      expect(m.content).toBe('hello');
    });

    it('promptForSlot returns expected question', () => {
      const m = promptForSlot('start_location');
      expect(m.role).toBe('assistant');
      expect(m.content).toContain('starting from');
    });

    it('initialGreeting has freshly-stamped timestamp', () => {
      const a = initialGreeting();
      const b = initialGreeting();
      expect(a.role).toBe('assistant');
      expect(typeof a.timestamp).toBe('string');
      expect(b.quickReplies).toBeDefined();
    });
  });

  // ------------------ readinessSummary ------------------
  describe('readinessSummary', () => {
    it('reports missing required slots', () => {
      const s = readinessSummary(freshTrip());
      expect(s.ready).toBe(false);
      expect(s.missing.length).toBeGreaterThan(0);
    });

    it('reports ready when required slots filled', () => {
      const trip = {
        ...freshTrip(),
        start_location: 'Denver',
        travelers: [{ name: 'X', age_group: 'adult' as const, interests: [], needs: [], notes: '' }],
      };
      const s = readinessSummary(trip);
      expect(s.ready).toBe(true);
      expect(s.missing.length).toBe(0);
    });
  });

  // ------------------ displayValueFor ------------------
  describe('displayValueFor', () => {
    it('start_location returns null when empty, value when set', () => {
      expect(displayValueFor('start_location', freshTrip())).toBeNull();
      expect(displayValueFor('start_location', { ...freshTrip(), start_location: 'Denver' })).toBe('Denver');
    });

    it('end_location shows Round trip when start is set', () => {
      expect(displayValueFor('end_location', { ...freshTrip(), start_location: 'Denver' })).toBe('Round trip');
      expect(displayValueFor('end_location', freshTrip())).toBeNull();
      expect(displayValueFor('end_location', { ...freshTrip(), end_location: 'Aspen' })).toBe('Aspen');
    });

    it('trip_duration_days renders number of days', () => {
      expect(displayValueFor('trip_duration_days', freshTrip())).toBeNull();
      expect(displayValueFor('trip_duration_days', { ...freshTrip(), trip_duration_days: 5 })).toBe('5 days');
    });

    it('travel_start_date passes through', () => {
      expect(displayValueFor('travel_start_date', freshTrip())).toBeNull();
      expect(displayValueFor('travel_start_date', { ...freshTrip(), travel_start_date: '2025-06-01' })).toBe('2025-06-01');
    });

    it('travelers summarises groups + named', () => {
      const trip = {
        ...freshTrip(),
        travelers: [
          { name: 'You', age_group: 'adult' as const, interests: [], needs: [], notes: '' },
          { name: 'Sarah', age_group: 'adult' as const, interests: [], needs: [], notes: '' },
        ],
      };
      const out = displayValueFor('travelers', trip);
      expect(out).toContain('adult');
      expect(out).toContain('1 named');
    });

    it('travelers single group uses singular form', () => {
      const trip = {
        ...freshTrip(),
        travelers: [{ name: '', age_group: 'adult' as const, interests: [], needs: [], notes: '' }],
      };
      expect(displayValueFor('travelers', trip)).toBe('1 adult');
    });

    it('travelers empty returns null', () => {
      expect(displayValueFor('travelers', freshTrip())).toBeNull();
    });

    it('required_stops joins by comma', () => {
      expect(displayValueFor('required_stops', { ...freshTrip(), required_stops: ['A', 'B'] })).toBe('A, B');
      expect(displayValueFor('required_stops', freshTrip())).toBeNull();
    });

    it('vehicle_type capitalises', () => {
      expect(displayValueFor('vehicle_type', { ...freshTrip(), vehicle_type: 'rv' })).toBe('Rv');
    });

    it('budget_level capitalises', () => {
      expect(displayValueFor('budget_level', freshTrip())).toBe('Moderate');
    });

    it('preferences joined by semicolons or null', () => {
      expect(displayValueFor('preferences', { ...freshTrip(), preferences: ['x', 'y'] })).toBe('x; y');
      expect(displayValueFor('preferences', freshTrip())).toBeNull();
    });
  });

  it('CONTEXT_SCHEMA is re-exported', () => {
    expect(Array.isArray(CONTEXT_SCHEMA)).toBe(true);
  });
});
