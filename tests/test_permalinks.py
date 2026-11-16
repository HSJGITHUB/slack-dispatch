"""P20 Slack permalinks: one helper for the console, the inbox CLI and the dispatch card."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from slack_dispatch.console import queries
from slack_dispatch.inbox import cli
from slack_dispatch.notify import dispatch_card
from tests.test_pipeline_integration import fixture_config

DOMAIN = "dummy-workspace"
CHANNEL = "C0000000002"


class PermalinkFormatTests(unittest.TestCase):
    def test_root_and_reply(self):
        self.assertEqual(queries.message_permalink(DOMAIN, CHANNEL, "1794528000.000100"),
                         f"https://{DOMAIN}.slack.com/archives/{CHANNEL}/p1794528000000100")
        self.assertEqual(queries.message_permalink(DOMAIN, CHANNEL, "1794528010.000200",
                                                   "1794528000.000100"),
                         f"https://{DOMAIN}.slack.com/archives/{CHANNEL}/p1794528010000200"
                         f"?thread_ts=1794528000.000100&cid={CHANNEL}")
        # A root whose thread_ts equals its ts is a root, not a reply.
        self.assertEqual(queries.message_permalink(DOMAIN, CHANNEL, "1794528000.000100",
                                                   "1794528000.000100"),
                         f"https://{DOMAIN}.slack.com/archives/{CHANNEL}/p1794528000000100")

    def test_no_domain_or_missing_parts(self):
        for args in ((None, CHANNEL, "1.0"), ("", CHANNEL, "1.0"), (DOMAIN, None, "1.0"),
                     (DOMAIN, CHANNEL, None)):
            with self.subTest(args=args):
                self.assertIsNone(queries.message_permalink(*args, "0.5"))

    def test_console_and_cli_build_on_the_card_helper(self):
        with patch.object(queries, "card_permalink", wraps=dispatch_card.permalink) as base:
            queries.message_permalink(DOMAIN, CHANNEL, "1.000100", "0.000100")
        base.assert_called_once_with(DOMAIN, CHANNEL, "1.000100")
        self.assertIs(queries.card_permalink, dispatch_card.permalink)
        with tempfile.TemporaryDirectory() as temp:
            config = fixture_config(Path(temp))
        config = replace(config, console=replace(config.console, workspace_domain=DOMAIN))
        self.assertEqual(cli.permalink_for(config, CHANNEL, "1.000100", "0.000100"),
                         queries.message_permalink(DOMAIN, CHANNEL, "1.000100", "0.000100"))
        self.assertIsNone(cli.permalink_for(None, CHANNEL, "1.000100"))
        view = queries.View(config)
        self.assertEqual(view.permalink(CHANNEL, "1.000100", "0.000100"),
                         queries.message_permalink(DOMAIN, CHANNEL, "1.000100", "0.000100"))

    def test_card_permalink_unchanged(self):
        self.assertEqual(dispatch_card.permalink("example", "C0000000002", "1000.000001"),
                         "https://example.slack.com/archives/C0000000002/p1000000001")
        self.assertIsNone(dispatch_card.permalink(None, "C0000000002", "1000.000001"))


if __name__ == "__main__":
    unittest.main()
