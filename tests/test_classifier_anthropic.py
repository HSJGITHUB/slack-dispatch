import builtins
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.router.classifier import AnthropicSuggester, NullSuggester


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.factory = Mock(return_value=self.client)
        self.suggester = AnthropicSuggester("dummy-model", "DUMMY_KEY", {"support": "Help requests"},
                                          "taxonomy-test", self.factory)

    def response(self, text):
        self.client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)])
        return self.suggester.suggest({"text": "Dummy request"})

    def test_valid_and_version(self):
        result = self.response('{"lane":"support","reason":"Needs help."}')
        self.assertEqual(result.lane, "support")
        self.assertEqual(result.classifier_version, "taxonomy-test+p1")
        self.factory.assert_called_once_with()
        request = self.client.messages.create.call_args.kwargs
        self.assertIn("support: Help requests", request["system"])
        self.assertIn("untrusted", request["system"])
        self.assertEqual(request["model"], "dummy-model")

    def test_strict_invalid_and_none(self):
        for text in ('{"lane":"none","reason":"No match."}', 'not JSON', '[]',
                     '{"lane":"unknown","reason":"Unknown."}',
                     '{"lane":"support","reason":""}',
                     '{"lane":"support","reason":5}',
                     '{"lane":"support","reason":"OK.","extra":1}',
                     '{"lane":"support","lane":"support","reason":"OK."}',
                     '```json\n{"lane":"support","reason":"OK."}\n```'):
            with self.subTest(text=text):
                self.assertIsNone(self.response(text))
        self.client.messages.create.return_value = SimpleNamespace(content=[])
        self.assertIsNone(self.suggester.suggest({}))
        self.assertIsNone(NullSuggester().suggest({}))

    def test_missing_optional_sdk_is_lazy(self):
        original = builtins.__import__
        def no_sdk(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("Fake unavailable SDK")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=no_sdk):
            suggester = AnthropicSuggester("dummy", "DUMMY_KEY", {}, "v1")
            with self.assertRaisesRegex(RuntimeError, "requires the anthropic SDK"):
                suggester.suggest({})
