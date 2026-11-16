import unittest
from decimal import Decimal
from recruitment_atlas.journey_graph.attribution import JourneyAttribution
class AttributionTests(unittest.TestCase):
 def setUp(self):self.flow=JourneyAttribution();self.events=[{'event_id':'e1','person_id':'p1','type':'visit','occurred_at':'2026-11-01T09:00:00+00:00'},{'event_id':'e2','person_id':'p1','type':'submission','occurred_at':'2026-11-02T09:00:00+00:00'},{'event_id':'e3','person_id':'p1','type':'intent','occurred_at':'2026-11-03T09:00:00+00:00'}]
 def test_identity_union(self):self.assertEqual(len(set(self.flow.canonical_identities([{'left':'email:a','right':'phone:1'},{'left':'phone:1','right':'device:x'}]).values())),1)
 def test_deduplicate(self):self.assertEqual(len(self.flow.deduplicate(self.events+[dict(self.events[0])])),3)
 def test_session_gap(self):self.assertEqual(len(self.flow.sessionize(self.events+[{'event_id':'e4','person_id':'p1','type':'view','occurred_at':'2026-11-10T09:00:00+00:00'}],60)),4)
 def test_weights_sum_to_one(self):self.assertEqual(sum(self.flow.weights(self.events).values(),Decimal('0')),Decimal('1'))
 def test_later_touch_gets_more_credit(self):
  weights=self.flow.weights(self.events);self.assertGreater(weights['e2'],weights['e1'])
 def test_no_conversion(self):self.assertEqual(self.flow.weights(self.events[:-1]),{})
 def test_funnel_unique_people(self):self.assertEqual(self.flow.funnel(self.events)['visit'],1)
