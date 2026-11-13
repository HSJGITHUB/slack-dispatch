"""P13 items 1, 2 and 5: `scope add|list|remove|import` and `sent` against a real temp ledger."""
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from slack_dispatch.inbox.cli import main
from slack_dispatch.ledger.store import LedgerStore

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "instance-sample" / "config.yaml"

HARVEST = """\
channel_defaults_add:
  C0000000011: {default: support}  # dummy comment
  C0000000012: {default: null}
  C0000000013: {default: unknown-lane}
channel_defaults_change:
  C0000000002: {default: content}
dm_defaults_add:
  D0000000011: {default: owner}
dm_defaults_change:
  D0000000012: {default: support}
person_rules_add:
  - {person: U0000000011, lane: content}
thread_bindings_add:
  - {source: slack, channel: C0000000011, thread_ts: '1000.000100', lane: support}
  - {source: slack, channel: C0000000011, thread_ts: '1000.000200', lane: support}
thread_bindings_rebind:
  - {source: slack, channel: C0000000002, thread_ts: '1000.000300', lane: content}
conflicts:
  - {kind: ch_add_conflict, id: C0000000019, current: none, claimants: 'support s1 / content s1', suggested: support}
unused_in_config:
  - {kind: ch, id: C0000000018, lane: owner}
"""


class ScopeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p13-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "config.yaml"
        shutil.copyfile(SAMPLE, self.config)
        self.store = LedgerStore(self.root / "ledger.sqlite3")
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in ("owner", "support", "content"):
            self.store.upsert_lane(lane)
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("SLACK_DISPATCH_SESSION", "CLAUDE_SESSION_ID", "SLACK_DISPATCH_CONFIG")}
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *args, config=True):
        out, err = io.StringIO(), io.StringIO()
        prefix = ["--inbox-root", str(self.root / "inbox")]
        if config:
            prefix += ["--config", str(self.config)]
        code = main([*prefix, *args], store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def claims(self, lane=None):
        return {(row["lane"], row["kind"], row["key"]): row for row in self.store.list_scope_claims(lane)}


class ScopeAddTests(ScopeCase):
    def test_add_records_claims_binds_threads_and_auto_applies_new_channel(self):
        os.environ["SLACK_DISPATCH_SESSION"] = "dummy-session"
        code, out, err = self.cli("scope", "add", "support", "--channel", "C0000000021",
                                  "--channel", "C0000000001", "--thread", "C0000000021:1000.000100",
                                  "--dm", "D0000000021", "--person", "U0000000021", "--note", "dummy")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("channel C0000000021 support auto-applied (scope:auto)", out)
        # C0000000001 already has a config default: the claim stays a proposal.
        self.assertIn("channel C0000000001 support claimed", out)
        self.assertIn("thread C0000000021:1000.000100 support bound", out)
        claims = self.claims("support")
        self.assertEqual(claims[("support", "channel", "C0000000021")]["confirmed_by"], "scope:auto")
        self.assertEqual(claims[("support", "channel", "C0000000001")]["status"], "claimed")
        self.assertEqual(claims[("support", "dm", "D0000000021")]["status"], "claimed")
        self.assertEqual(claims[("support", "person", "U0000000021")]["claimed_by"], "dummy-session")
        self.assertEqual(self.store.lookup_binding("slack", "C0000000021", "1000.000100"), "support")
        self.assertEqual(self.store.lookup_scope_claim("channel", "C0000000021")["lane"], "support")
        self.assertIsNone(self.store.lookup_scope_claim("dm", "D0000000021"))
        kinds = {(row["kind"], row["key"]): row for row in self.store.list_scope_proposals()}
        self.assertEqual(set(kinds), {("channel", "C0000000021"), ("channel", "C0000000001"),
                                      ("dm", "D0000000021"), ("person", "U0000000021")})
        self.assertEqual(kinds[("dm", "D0000000021")]["last_evidence"], "scope")
        # Re-adding refreshes without new evidence.
        code, out, _ = self.cli("scope", "add", "support", "--dm", "D0000000021")
        self.assertIn("dm D0000000021 support refreshed", out)
        self.assertEqual(self.store.get_scope_proposal("dm", "D0000000021", "support")["evidence_count"], 1)

    def test_no_auto_when_another_lane_claimed_or_without_config(self):
        self.cli("scope", "add", "content", "--channel", "C0000000022")
        self.store.remove_scope_claim("content", "channel", "C0000000022")
        self.store.decide_scope_proposal("channel", "C0000000022", "content", "reject")
        code, out, _ =self.cli("scope", "add", "support", "--channel", "C0000000022")
        self.assertIn("channel C0000000022 support claimed", out)
        self.assertIsNone(self.store.lookup_scope_claim("channel", "C0000000022"))
        code, out, err = self.cli("scope", "add", "support", "--channel", "C0000000023", config=False)
        self.assertEqual(code, 0)
        self.assertIn("claimed", out)
        self.assertIn("scope:auto is not applied", err)
        self.assertIsNone(self.store.lookup_scope_claim("channel", "C0000000023"))

    def test_list_remove_and_exit_codes(self):
        self.cli("scope", "add", "support", "--dm", "D0000000031", "--note", "dummy note")
        self.cli("scope", "add", "content", "--person", "U0000000031")
        code, out, _ = self.cli("scope", "list", "support")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "lane kind key status confirmed_by claimed_by claimed_at note")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("support dm D0000000031 claimed - cli "))
        self.assertTrue(lines[1].endswith("dummy note"))
        self.assertEqual(len(self.cli("scope", "list")[1].splitlines()), 3)
        code, out, _ = self.cli("scope", "remove", "support", "--dm", "D0000000031")
        self.assertEqual((code, out.strip()), (0, "dm D0000000031 support removed"))
        removed = self.store.list_scope_claims("support", include_removed=True)
        self.assertEqual(len(removed), 1)
        self.assertIsNotNone(removed[0]["removed_at"])
        self.assertEqual(self.cli("scope", "remove", "support", "--dm", "D0000000031")[0], 3)
        self.assertEqual(self.cli("scope", "add", "missing", "--dm", "D0000000031")[0], 3)
        for bad in (("scope", "add", "support"), ("scope", "add", "support", "--channel", "D0000000001"),
                    ("scope", "add", "support", "--thread", "C0000000001"),
                    ("scope", "add", "support", "--person", "C0000000001"),
                    ("scope", "remove", "support"), ("scope", "add", "Bad Lane", "--dm", "D0000000031")):
            self.assertEqual(self.cli(*bad)[0], 2, bad)
        bad_config = self.root / "bad.yaml"
        bad_config.write_text("lanes: []\n", encoding="utf-8")
        self.config = bad_config
        self.assertEqual(self.cli("scope", "add", "support", "--dm", "D0000000032")[0], 2)
        keys = {row["key"] for row in self.store.list_scope_claims(None, include_removed=True)}
        self.assertNotIn("D0000000032", keys)


class SentTests(ScopeCase):
    def test_reply_binds_root_thread_and_duplicates_are_idempotent(self):
        self.store.bind_thread("slack", "C0000000002", "1000.000100", "owner")
        code, out, _ = self.cli("sent", "C0000000002", "1000.000200", "--lane", "support",
                                "--thread", "1000.000100")
        self.assertEqual(code, 0)
        self.assertIn("bound slack:C0000000002:1000.000100 (was owner)", out)
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000100"), "support")
        code, out, _ = self.cli("sent", "C0000000002", "1000.000200", "--lane", "content",
                                "--thread", "1000.000100")
        self.assertEqual((code, out.strip()), (0, "sent C0000000002 1000.000200 already recorded"))
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000100"), "support")
        sends = self.store.list_sends()
        self.assertEqual([(row["ts"], row["thread_ts"], row["lane"], row["kind"]) for row in sends],
                         [("1000.000200", "1000.000100", "support", "reply")])
        self.assertEqual(self.store.list_scope_proposals(), [])

    def test_root_post_binds_its_own_ts(self):
        code, _, _ = self.cli("sent", "C0000000002", "1000.000400", "--lane", "content")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000400"), "content")
        self.assertEqual(self.store.list_sends()[0]["kind"], "root")

    def test_dm_send_creates_dm_claim_and_evidence(self):
        code, out, _ = self.cli("sent", "D0000000041", "1000.000500", "--lane", "support")
        self.assertEqual(code, 0)
        self.assertIn("dm D0000000041 support claimed", out)
        self.assertEqual(self.claims("support")[("support", "dm", "D0000000041")]["status"], "claimed")
        self.cli("sent", "D0000000041", "1000.000500", "--lane", "support")
        self.assertEqual(self.store.get_scope_proposal("dm", "D0000000041", "support")["evidence_count"], 1)
        self.cli("sent", "D0000000041", "1000.000600", "--lane", "support")
        row = self.store.get_scope_proposal("dm", "D0000000041", "support")
        self.assertEqual((row["evidence_count"], row["status"]), (2, "suggested"))
        self.assertEqual(len(self.store.list_scope_claims("support")), 1)
        # A DM send never routes by itself: the claim is not confirmed.
        self.assertIsNone(self.store.lookup_scope_claim("dm", "D0000000041"))

    def test_invalid_and_missing_lane(self):
        for bad in (("sent", "C0000000002", "1000.000700", "--lane", "support", "--kind", "reply"),
                    ("sent", "C0000000002", "1000.000700", "--lane", "support", "--thread", "1",
                     "--kind", "root"),
                    ("sent", "X0000000002", "1000.000700", "--lane", "support"),
                    ("sent", "C0000000002", "not-a-ts", "--lane", "support"),
                    ("sent", "C0000000002", "1000.000700")):
            self.assertEqual(self.cli(*bad)[0], 2, bad)
        self.assertEqual(self.cli("sent", "C0000000002", "1000.000700", "--lane", "missing")[0], 3)
        self.assertEqual(self.store.list_sends(), [])


class ImportTests(ScopeCase):
    def write(self, text=HARVEST):
        path = self.root / "scope_proposals.yaml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_import_loads_suggested_proposals_bindings_and_reports(self):
        self.store.bind_thread("slack", "C0000000011", "1000.000200", "owner")
        self.store.bind_thread("slack", "C0000000002", "1000.000300", "owner")
        self.store.record_scope_evidence("channel", "C0000000002", "support", "reroute")
        code, out, err = self.cli("scope", "import", self.write())
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "proposals added=5 kept=0 skipped_null=1 skipped_unknown_lane=1")
        self.assertEqual(lines[1], "bindings added=1 rebound=1 kept=1 skipped_unknown_lane=0")
        self.assertIn("Unknown lanes: unknown-lane", out)
        self.assertIn("conflict channel C0000000002 lanes=content,support", out)
        self.assertIn("file_conflicts=1 (not imported)", out)
        self.assertIn("file_conflict ch_add_conflict C0000000019 suggested=support", out)
        self.assertIn("unused_in_config=1 (ignored)", out)
        rows = {(row["kind"], row["key"], row["lane"]): row for row in self.store.list_scope_proposals()}
        for key in (("channel", "C0000000011", "support"), ("dm", "D0000000011", "owner"),
                    ("dm", "D0000000012", "support"), ("person", "U0000000011", "content")):
            self.assertEqual(rows[key]["status"], "suggested", key)
            self.assertEqual(rows[key]["evidence_count"], 1)
        self.assertEqual(rows[("channel", "C0000000002", "content")]["status"], "open")
        self.assertTrue(rows[("channel", "C0000000002", "content")]["conflict"])
        self.assertEqual(self.store.lookup_binding("slack", "C0000000011", "1000.000100"), "support")
        self.assertEqual(self.store.lookup_binding("slack", "C0000000011", "1000.000200"), "owner")
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000300"), "content")
        # Imports never confirm anything.
        self.assertEqual(self.store.confirmed_scope_claims(), [])
        code, out, _ = self.cli("scope", "import", self.write())
        self.assertTrue(out.startswith("proposals added=0 kept=5 "))
        self.assertIn("bindings added=0 rebound=0 kept=3", out)
        self.assertEqual(self.store.get_scope_proposal("dm", "D0000000011", "owner")["evidence_count"], 1)

    def test_dry_run_and_invalid_files(self):
        code, out, _ = self.cli("scope", "import", self.write(), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("proposals added=5 kept=0", out)
        self.assertIn("dry run: nothing changed", out)
        self.assertEqual(self.store.list_scope_proposals(), [])
        self.assertIsNone(self.store.lookup_binding("slack", "C0000000011", "1000.000100"))
        for text in ("- just a list\n", "dm_defaults_add:\n  X0000000001: {default: owner}\n",
                     "thread_bindings_add:\n  - {source: slack, channel: C0000000001, thread_ts: 1000.5,"
                     " lane: owner}\n", "channel_defaults_add:\n  C0000000001: {default: Bad Lane}\n",
                     "channel_defaults_add: [1, 2]\n", "{unclosed\n"):
            self.assertEqual(self.cli("scope", "import", self.write(text))[0], 2, text)
        self.assertEqual(self.cli("scope", "import", str(self.root / "missing.yaml"))[0], 2)
        self.assertEqual(self.store.list_scope_proposals(), [])


if __name__ == "__main__":
    unittest.main()
