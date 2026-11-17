import datetime as dt
import decimal
import unittest

from recruitment_atlas.attribution.engine import AttributionEngine
from recruitment_atlas.journey_graph.attribution import JourneyAttribution


def at(day, hour=9, minute=0):
    return f'2026-11-{day:02d}T{hour:02d}:{minute:02d}:00+00:00'


def event(event_id, kind, day, hour=9, minute=0, **fields):
    return {'event_id': event_id, 'type': kind, 'occurred_at': at(day, hour, minute), **fields}


class CrossPersonTests(unittest.TestCase):
    def setUp(self):
        self.flow = JourneyAttribution()

    def test_conversion_never_credits_another_candidates_touchpoint(self):
        events = [
            event('a1', 'visit', 1, person_id='candidate-a'),
            event('b1', 'visit', 2, person_id='candidate-b'),
            event('b2', 'submission', 3, person_id='candidate-b'),
            event('a2', 'intent', 4, person_id='candidate-a'),
        ]
        weights = self.flow.weights(events)
        self.assertEqual(set(weights), {'a1'})
        self.assertNotIn('b1', weights)
        facts = self.flow.evaluate({'id': 'shared', 'events': events}, {})
        self.assertEqual(set(facts['weights']), {'a1'})
        self.assertEqual(facts['ignored']['other_person'], 2)

    def test_two_conversions_for_two_people_are_uncertain(self):
        events = [
            event('a1', 'visit', 1, person_id='candidate-a'),
            event('b1', 'visit', 2, person_id='candidate-b'),
            event('a2', 'intent', 4, person_id='candidate-a'),
            event('b2', 'intent', 4, person_id='candidate-b'),
        ]
        facts = self.flow.evaluate({'id': 'shared', 'events': events}, {})
        self.assertEqual(facts['weights'], {})
        self.assertTrue(facts['uncertain'])
        self.assertIn('ambiguous_person', facts['reasons'])

    def test_weighted_attribution_within_owner_only(self):
        events = [
            event('a1', 'visit', 1, person_id='candidate-a'),
            event('b1', 'visit', 2, person_id='candidate-b'),
            event('a2', 'intent', 4, person_id='candidate-a'),
        ]
        facts = self.flow.evaluate({'id': 'journey-a', 'owner_id': 'candidate-a',
                                    'events': events}, {})
        self.assertEqual(set(facts['weights']), {'a1'})
        self.assertEqual(decimal.Decimal(facts['weights']['a1']), decimal.Decimal('1'))
        self.assertEqual(facts['ignored']['other_person'], 1)
        self.assertEqual(facts['persons'], ['candidate-a', 'candidate-b'])

    def test_owner_mismatch_is_reported_not_credited(self):
        events = [event('b1', 'visit', 1, person_id='candidate-b'),
                  event('b2', 'intent', 2, person_id='candidate-b')]
        facts = self.flow.evaluate({'id': 'journey-a', 'owner_id': 'candidate-a',
                                    'events': events}, {})
        self.assertEqual(facts['weights'], {})
        self.assertIn('owner_person_mismatch', facts['reasons'])
        self.assertTrue(facts['uncertain'])

    def test_sessions_do_not_span_people(self):
        events = [event('a1', 'view', 1, 9, 0, person_id='candidate-a'),
                  event('b1', 'view', 1, 9, 10, person_id='candidate-b')]
        sessions = self.flow.sessionize(events, 60)
        self.assertEqual([[item['event_id'] for item in session] for session in sessions],
                         [['a1'], ['b1']])

    def test_funnel_counts_each_person_once(self):
        events = [event('a1', 'visit', 1, person_id='candidate-a'),
                  event('a2', 'visit', 2, person_id='candidate-a'),
                  event('b1', 'visit', 1, person_id='candidate-b')]
        self.assertEqual(self.flow.funnel(events)['visit'], 2)


class CrossDeviceTests(unittest.TestCase):
    def setUp(self):
        self.flow = JourneyAttribution()
        self.links = [{'left': 'device:tablet-1', 'right': 'person:candidate-a',
                       'relation': 'device_link', 'verified': True}]

    def test_verified_device_link_joins_the_right_candidate(self):
        events = [
            event('d1', 'view', 1, device_id='tablet-1'),
            event('b1', 'view', 1, 10, person_id='candidate-b'),
            event('a1', 'intent', 2, person_id='candidate-a'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'owner_id': 'candidate-a',
                                    'events': events, 'identity_links': self.links}, {})
        self.assertEqual(set(facts['weights']), {'d1'})
        self.assertEqual(facts['ignored']['other_person'], 1)

    def test_shared_device_link_is_treated_as_ambiguous(self):
        weak = [{'left': 'device:tablet-1', 'right': 'person:candidate-a',
                 'relation': 'shared_device'}]
        events = [
            event('d1', 'view', 1, device_id='tablet-1'),
            event('a1', 'intent', 2, person_id='candidate-a'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'events': events,
                                    'identity_links': weak}, {})
        self.assertEqual(facts['weights'], {})
        self.assertIn('ambiguous_identity', facts['reasons'])
        self.assertEqual(facts['identity_problems'][0]['code'], 'weak_identity_link')

    def test_conflicting_verified_links_do_not_merge_candidates(self):
        conflicting = [
            {'left': 'device:tablet-1', 'right': 'person:candidate-a', 'relation': 'device_link'},
            {'left': 'device:tablet-1', 'right': 'person:candidate-b', 'relation': 'device_link'},
        ]
        events = [
            event('d1', 'view', 1, device_id='tablet-1'),
            event('a1', 'intent', 2, person_id='candidate-a'),
            event('b1', 'intent', 2, 10, person_id='candidate-b'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'events': events,
                                    'identity_links': conflicting}, {})
        self.assertEqual(facts['weights'], {})
        self.assertIn('ambiguous_identity', facts['reasons'])
        self.assertEqual(facts['identity_problems'][0]['code'], 'conflicting_identity_link')

    def test_device_alias_never_rewrites_an_explicit_person(self):
        events = [
            event('a1', 'view', 1, person_id='candidate-a', device_id='unrelated'),
            event('a2', 'intent', 2, person_id='candidate-a'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'events': events,
                                    'identity_links': self.links}, {})
        self.assertEqual(set(facts['weights']), {'a1'})


class DuplicateEventTests(unittest.TestCase):
    def setUp(self):
        self.flow = JourneyAttribution()

    def test_repeated_event_does_not_change_people_or_weights(self):
        base = [event('v1', 'visit', 1, person_id='candidate-a'),
                event('i1', 'intent', 2, person_id='candidate-a')]
        duplicated = base + [dict(base[0])]
        self.assertEqual(self.flow.weights(base), self.flow.weights(duplicated))
        self.assertEqual(self.flow.funnel(base), self.flow.funnel(duplicated))

    def test_late_duplicate_cannot_steal_a_settled_attribution(self):
        events = [
            event('v1', 'visit', 1, person_id='candidate-a',
                  received_at='2026-11-01T09:01:00+00:00'),
            event('v1', 'visit', 1, person_id='candidate-b',
                  received_at='2026-11-05T09:00:00+00:00'),
            event('i1', 'intent', 2, person_id='candidate-a'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'owner_id': 'candidate-a',
                                    'events': events}, {})
        self.assertEqual(set(facts['weights']), {'v1'})
        self.assertEqual(facts['funnel']['visit'], 1)
        self.assertEqual(facts['event_problems'][0]['code'], 'conflicting_duplicate')
        self.assertEqual(facts['event_problems'][0]['kept'], 'candidate-a')

    def test_out_of_order_duplicate_uses_first_arrival_not_first_in_list(self):
        events = [
            event('v1', 'visit', 1, person_id='candidate-a',
                  received_at='2026-11-05T09:00:00+00:00'),
            event('v1', 'visit', 1, person_id='candidate-b',
                  received_at='2026-11-01T09:01:00+00:00'),
            event('i1', 'intent', 2, person_id='candidate-b'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'owner_id': 'candidate-b',
                                    'events': events}, {})
        self.assertEqual(set(facts['weights']), {'v1'})
        self.assertEqual(facts['target_person'], 'candidate-b')

    def test_duplicate_without_arrival_time_stays_unattributed(self):
        events = [
            event('v1', 'visit', 1, person_id='candidate-a'),
            event('v1', 'visit', 1, person_id='candidate-b'),
            event('i1', 'intent', 2, person_id='candidate-b'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'owner_id': 'candidate-b',
                                    'events': events}, {})
        self.assertEqual(facts['weights'], {})
        self.assertEqual(facts['event_problems'][0]['kept'], None)
        self.assertIn('ambiguous_identity', facts['reasons'])
        self.assertTrue(facts['uncertain'])
        # The conflicting copies stay one unknown actor: no inflation to two.
        self.assertEqual(facts['funnel']['visit'], 1)

    def test_ambiguous_duplicate_is_not_credited_when_owner_has_own_touch(self):
        events = [
            event('v1', 'visit', 1, person_id='candidate-a'),
            event('v1', 'visit', 1, person_id='candidate-b'),
            event('w1', 'visit', 1, 10, person_id='candidate-b'),
            event('i1', 'intent', 2, person_id='candidate-b'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'owner_id': 'candidate-b',
                                    'events': events}, {})
        self.assertEqual(set(facts['weights']), {'w1'})
        self.assertIn('ambiguous_identity_ignored', facts['warnings'])

    def test_late_duplicate_does_not_inflate_people_sessions_or_funnel(self):
        base = [event('v1', 'view', 1, person_id='candidate-a'),
                event('i1', 'intent', 2, person_id='candidate-a')]
        late = base + [event('v1', 'view', 1, person_id='candidate-a',
                             received_at='2026-11-09T09:00:00+00:00')]
        before = self.flow.evaluate({'id': 'journey', 'events': base}, {})
        after = self.flow.evaluate({'id': 'journey', 'events': late}, {})
        for key in ('deduplicated', 'sessions', 'funnel', 'weights', 'persons'):
            self.assertEqual(before[key], after[key], key)


class UncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.flow = JourneyAttribution()

    def test_no_conversion_is_explained(self):
        facts = self.flow.evaluate({'id': 'journey', 'events': [
            event('v1', 'visit', 1, person_id='candidate-a')]}, {})
        self.assertEqual(facts['weights'], {})
        self.assertIn('no_conversion', facts['reasons'])
        self.assertTrue(facts['uncertain'])

    def test_missing_timestamp_is_explained(self):
        events = [{'event_id': 'v1', 'type': 'visit', 'person_id': 'candidate-a'},
                  event('i1', 'intent', 2, person_id='candidate-a')]
        facts = self.flow.evaluate({'id': 'journey', 'events': events}, {})
        self.assertEqual(facts['weights'], {})
        self.assertIn('missing_timestamp', facts['reasons'])

    def test_ambiguous_person_is_explained(self):
        events = [event('v1', 'visit', 1, person_id='candidate-a'),
                  event('v2', 'visit', 1, person_id='candidate-b')]
        facts = self.flow.evaluate({'id': 'journey', 'events': events}, {})
        self.assertEqual(facts['weights'], {})
        self.assertIn('ambiguous_person', facts['reasons'])
        self.assertTrue(facts['uncertain'])

    def test_touches_outside_the_conversion_window_are_not_credited(self):
        events = [event('old', 'visit', 1, person_id='candidate-a'),
                  event('recent', 'visit', 12, person_id='candidate-a'),
                  event('after', 'view', 15, person_id='candidate-a'),
                  event('i1', 'intent', 14, person_id='candidate-a')]
        facts = self.flow.evaluate({'id': 'journey', 'events': events},
                                   {'conversion_window_days': 7})
        self.assertEqual(set(facts['weights']), {'recent'})
        self.assertEqual(facts['ignored']['outside_window'], 1)
        self.assertEqual(facts['ignored']['after_conversion'], 1)
        self.assertIn('touch_outside_conversion_window', facts['warnings'])
        self.assertIn('touch_after_conversion_ignored', facts['warnings'])

    def test_touchpoint_attributed_to_its_own_intent_only(self):
        events = [
            event('job1-view', 'view', 1, person_id='candidate-a', intent_id='job-1'),
            event('job2-view', 'view', 2, person_id='candidate-a', intent_id='job-2'),
            event('job2-intent', 'intent', 3, person_id='candidate-a', intent_id='job-2'),
        ]
        facts = self.flow.evaluate({'id': 'journey', 'events': events}, {})
        self.assertEqual(set(facts['weights']), {'job2-view'})
        self.assertEqual(facts['ignored']['other_intent'], 1)

    def test_engine_keeps_person_scoped_value_and_reasons(self):
        engine = AttributionEngine()
        mixed = engine.evaluate({'id': 'mixed', 'events': [
            event('a1', 'visit', 1, person_id='candidate-a'),
            event('b1', 'intent', 2, person_id='candidate-b')]}, {})
        self.assertFalse(mixed.accepted)
        self.assertEqual(mixed.value, decimal.Decimal('0'))
        self.assertTrue(mixed.reasons)
        single = engine.evaluate({'id': 'single', 'events': [
            event('a1', 'visit', 1, person_id='candidate-a'),
            event('a2', 'intent', 2, person_id='candidate-a')]}, {})
        self.assertTrue(single.accepted)
        self.assertEqual(single.value, decimal.Decimal('1'))


class PersistenceCompatibilityTests(unittest.TestCase):
    def test_acceptance_fixture_still_completes(self):
        from pathlib import Path
        import json
        from recruitment_atlas.service import ApplicationService
        from recruitment_atlas.storage import Database
        import tempfile

        fixture = json.loads(Path('fixtures/acceptance.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            service = ApplicationService(Database(Path(directory) / 'acceptance.sqlite3'))
            outputs = [service.process(item, fixture['context'],
                                       item.get('request_key', f'item-{index}'))
                       for index, item in enumerate(fixture['items'])]
            replay = service.process(fixture['items'][0], fixture['context'],
                                     fixture['items'][0]['request_key'])
        self.assertTrue(outputs[0]['accepted'])
        self.assertEqual(replay, outputs[0])
        self.assertEqual(decimal.Decimal(outputs[0]['value']), decimal.Decimal('1'))


if __name__ == '__main__':
    unittest.main()
