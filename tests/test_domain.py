import decimal
import unittest
from recruitment_atlas.attribution.engine import AttributionEngine

class DomainTests(unittest.TestCase):
    def setUp(self): self.engine=AttributionEngine()

    def journey(self,**changes):
        value={'id':'journey-1','events':[{'event_id':'e1','type':'visit','at':'2026-11-16T09:00:00'},{'event_id':'e2','type':'submission','at':'2026-11-16T10:00:00'},{'event_id':'e3','type':'intent','at':'2026-11-16T11:00:00'}]};value.update(changes);return value
    def test_conversion(self):self.assertTrue(self.engine.evaluate(self.journey(),{}).accepted)
    def test_weights_conserve(self):self.assertEqual(self.engine.evaluate(self.journey(),{}).value,decimal.Decimal('1'))
    def test_no_conversion(self):self.assertIn('no_conversion',self.engine.evaluate(self.journey(events=[]),{}).reasons)
    def test_duplicate_event_removed(self):
        events=self.journey()['events'];result=self.engine.evaluate(self.journey(events=events+[events[0]]),{});self.assertEqual(result.facts['deduplicated_events'],3)
    def test_recency_weights(self):
        weights=self.engine.evaluate(self.journey(),{}).facts['weights'];self.assertGreater(decimal.Decimal(weights['e2']),decimal.Decimal(weights['e1']))
    def test_rank_converted_first(self):self.assertTrue(self.engine.rank([self.journey(id='none',events=[]),self.journey(id='yes')],{})[0].accepted)

