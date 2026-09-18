import unittest

from hubspot_audit.investigation_queue import InvestigationQueue
from hubspot_audit.models import InvestigationStatus


class InvestigationQueueTests(unittest.TestCase):
    def test_deduplicates_repeated_questions(self):
        queue = InvestigationQueue()
        first = queue.push("Is X a duplicate?", None, [], 0.5, depth=0)
        second = queue.push("is x a duplicate?  ", None, [], 0.5, depth=0)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(queue.all()), 1)

    def test_depth_cap_rejects_deep_investigations(self):
        queue = InvestigationQueue(max_depth=2)
        rejected = queue.push("deep question", None, [], 0.9, depth=3)
        self.assertIsNone(rejected)
        self.assertEqual(len(queue.all()), 0)

    def test_low_information_value_is_deferred_not_queued(self):
        queue = InvestigationQueue(low_value_threshold=0.5)
        investigation = queue.push("low value question", None, [], 0.1, depth=0)
        self.assertEqual(investigation.status, InvestigationStatus.DEFERRED_LOW_VALUE)
        self.assertIsNone(queue.pop_next())

    def test_needs_human_context_flag_short_circuits_priority(self):
        queue = InvestigationQueue()
        investigation = queue.push("needs a human", None, [], 0.9, depth=0, needs_human_context=True)
        self.assertEqual(investigation.status, InvestigationStatus.NEEDS_HUMAN_CONTEXT)
        self.assertIsNone(queue.pop_next())

    def test_pop_next_returns_highest_priority_first(self):
        queue = InvestigationQueue()
        queue.push("low", None, [], 0.3, depth=0)
        queue.push("high", None, [], 0.9, depth=0)
        queue.push("medium", None, [], 0.6, depth=0)
        order = []
        while True:
            investigation = queue.pop_next()
            if investigation is None:
                break
            order.append(investigation.question)
        self.assertEqual(order, ["high", "medium", "low"])

    def test_max_size_enforced(self):
        queue = InvestigationQueue(max_size=2)
        queue.push("q1", None, [], 0.9, depth=0)
        queue.push("q2", None, [], 0.9, depth=0)
        third = queue.push("q3", None, [], 0.9, depth=0)
        self.assertIsNone(third)

    def test_resolve_sets_status_and_outcome(self):
        queue = InvestigationQueue()
        investigation = queue.push("q1", None, [], 0.9, depth=0)
        queue.pop_next()
        queue.resolve(investigation.investigation_id, InvestigationStatus.DONE, "resolved via evidence")
        self.assertEqual(queue.all()[0].status, InvestigationStatus.DONE)
        self.assertEqual(queue.all()[0].outcome, "resolved via evidence")


if __name__ == "__main__":
    unittest.main()
