import unittest

from slack_dispatch.router.rules import Route, first_match


class FakeRule:
    def __init__(self, result, calls, label):
        self.result, self.calls, self.label = result, calls, label

    def match(self, message):
        self.calls.append(self.label)
        return self.result


class RouterTests(unittest.TestCase):
    def test_first_match_stops(self):
        calls = []
        route = Route("support", "first")
        rules = [FakeRule(route, calls, "first"), FakeRule(Route("owner", "second"), calls, "second")]
        self.assertEqual(first_match({}, rules), route)
        self.assertEqual(calls, ["first"])

    def test_miss_continues_in_order(self):
        calls = []
        route = Route("owner", "second")
        rules = [FakeRule(None, calls, "first"), FakeRule(route, calls, "second")]
        self.assertEqual(first_match({}, rules), route)
        self.assertEqual(calls, ["first", "second"])
        self.assertIsNone(first_match({}, [FakeRule(None, [], "miss")]))
