"""P14 console staging: one GO commit, ui_language, exclude channel hint (dummy ids only)."""
from dataclasses import replace
import json
from pathlib import Path
import re
import unittest

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.console import queries
from tests.test_console_app import ConsoleCase, stamp, ts
from tests.test_console_core import sample_data, seed
from tests.test_pipeline_integration import SAMPLE

STATIC = Path(queries.__file__).with_name("static")


def dictionary_keys(script, language):
    """Key paths of one UI_TEXT dictionary in console.js (string literals blanked first)."""
    text = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', script)
    start = text.index(language + ": Object.freeze({") + len(language + ": Object.freeze(")
    depth, end = 0, start
    for end in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[end], 0)
        if depth == 0:
            break
    body = text[start + 1:end]
    paths, names, depth = set(), {}, 0
    for match in re.finditer(r"([{,])\s*(\w+)\s*:|[{}]", "," + body):
        opener = match.group(1) or match.group(0)
        if opener == "{":
            depth += 1
        elif opener == "}":
            depth -= 1
            continue
        if match.group(2):
            names[depth] = match.group(2)
            paths.add(".".join(names[level] for level in range(depth + 1)))
    return paths


class CommitTests(ConsoleCase):
    def commit(self, decisions, status=200):
        return self.post("/api/queue/commit", {"decisions": decisions}, status)

    def test_mixed_results_in_order_and_no_write_for_unstaged(self):
        ids = [self.pending(offset, text=f"DUMMY_{offset}") for offset in (1, 2, 3, 4, 5, 6)]
        handled = ids[5]
        self.store.set_route(handled, "support", "test")  # someone else handled it meanwhile
        untouched = ids[4]
        before = dict(self.store.get_message(untouched))
        data = self.commit([
            {"id": ids[3], "action": "done", "note": " dummy done "},
            {"id": "slack:C0000000002:1.000100", "action": "exclude"},
            {"id": ids[0], "action": "assign", "lane": "support"},
            {"id": handled, "action": "assign", "lane": "content"},
            {"id": ids[1], "action": "direct"},
            {"id": ids[2], "action": "exclude"},
        ])
        rows = data["results"]
        self.assertEqual([row["id"] for row in rows],
                         [ids[3], "slack:C0000000002:1.000100", ids[0], handled, ids[1], ids[2]])
        self.assertEqual([row["status"] for row in rows],
                         ["applied", "not_found", "applied", "conflict", "applied", "applied"])
        self.assertEqual(data["counts"], {"applied": 4, "conflict": 1, "not_found": 1, "error": 0})
        self.assertEqual((rows[3]["detail"], rows[2]["lane"], rows[4]["lane"]),
                         ("state is assigned", "support", "owner"))
        self.assertEqual((self.store.get_message(ids[3])["state"], self.store.get_message(ids[3])["note"]),
                         ("done", "dummy done"))
        self.assertEqual((self.store.get_message(ids[0])["lane"], self.store.get_message(ids[0])["route_reason"]),
                         ("support", "human:console"))
        self.assertEqual(self.store.get_message(ids[1])["lane"], "owner")
        self.assertEqual(self.store.get_message(ids[2])["state"], "excluded")
        self.assertEqual(self.store.get_message(handled)["lane"], "support")
        self.assertEqual(dict(self.store.get_message(untouched)), before)
        queue = self.get("/api/queue")
        self.assertEqual(([row["id"] for row in queue["items"]], queue["pending_ids"]), ([untouched], [untouched]))
        records = [json.loads(line) for line in
                   (self.inbox / "support.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["id"] for row in records], [ids[0]])

    def test_per_decision_errors_do_not_stop_the_batch(self):
        mid = self.pending(1)
        other = self.pending(2)
        third = self.pending(3)
        data = self.commit([
            {"id": mid, "action": "assign"},
            {"id": third, "action": "assign", "lane": "missing"},
            {"id": other, "action": "assign", "lane": "support"},
            {"id": other, "action": "done"},
        ])
        self.assertEqual([(row["status"], row["detail"]) for row in data["results"]],
                         [("error", "lane is required"), ("error", "unknown lane"),
                          ("applied", None), ("error", "duplicate id")])
        self.assertEqual(self.store.get_message(mid)["state"], "human_pending")
        self.assertEqual(self.store.get_message(third)["state"], "human_pending")
        self.assertEqual(self.store.get_message(other)["state"], "assigned")
        no_direct = self.make_client(replace(self.config, console=replace(self.config.console,
                                                                          direct_lane="owner-direct")))
        response = no_direct.post("/api/queue/commit", json={"decisions": [{"id": mid, "action": "direct"}]})
        self.assertEqual(response.json()["results"][0]["detail"], "direct lane is not configured")
        self.commit([], 422)
        self.commit([{"id": mid, "action": "ack"}], 422)
        self.commit([{"id": mid, "action": "done"}] * 201, 422)
        self.assertEqual(self.store.get_message(mid)["state"], "human_pending")

    def test_exclude_channel_hint(self):
        no_default = self.pending(1)  # C0000000002 has a null (multi-lane) default
        with_default = seed(self.store, ts(2), channel="C0000000001", received=stamp(2))
        unknown = seed(self.store, ts(3), channel="C0000000009", received=stamp(3))
        for mid in (with_default, unknown):
            self.store.set_state(mid, "human_pending", note="none")
        rows = self.commit([{"id": mid, "action": "exclude"} for mid in (no_default, with_default, unknown)])
        self.assertEqual([row["channel_hint"] for row in rows["results"]], [True, False, True])
        self.assertEqual(rows["results"][0]["channel"], {"id": "C0000000002", "name": "dummy-general"})
        again = self.commit([{"id": no_default, "action": "exclude"}])["results"][0]
        self.assertEqual((again["status"], again["channel_hint"]), ("conflict", False))
        legacy_id = self.pending(4)
        legacy = self.post("/api/queue/actions", {"action": "exclude", "ids": [legacy_id]})
        self.assertTrue(legacy["results"][0]["channel_hint"])
        assigned = self.pending(5)
        plain = self.post("/api/queue/actions", {"action": "done", "ids": [assigned]})
        self.assertNotIn("channel_hint", plain["results"][0])

    def test_legacy_immediate_action_still_applies(self):
        mid = self.pending(1)
        result = self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "content"})
        self.assertEqual((result["applied"], result["failed"]), (1, 0))
        self.assertEqual(self.store.get_message(mid)["lane"], "content")


class LanguageTests(ConsoleCase):
    def test_config_default_and_value(self):
        self.assertEqual(self.get("/api/config")["ui_language"], "en")
        korean = replace(self.config, console=replace(self.config.console, ui_language="ko"))
        self.assertEqual(self.get("/api/config", client=self.make_client(korean))["ui_language"], "ko")
        data = sample_data()
        self.assertEqual(load_config(SAMPLE).console.ui_language, "en")
        del data["console"]["ui_language"]
        self.assertEqual(parse_config(data).console.ui_language, "en")
        data["console"]["ui_language"] = "ko"
        self.assertEqual(parse_config(data).console.ui_language, "ko")
        for value in ("de", "", 1, None):
            with self.subTest(value=value):
                data["console"]["ui_language"] = value
                with self.assertRaises(ConfigError):
                    parse_config(data)

    def test_dictionaries_have_the_same_keys(self):
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        english, korean = dictionary_keys(script, "en"), dictionary_keys(script, "ko")
        self.assertGreater(len(english), 150)
        self.assertEqual(english, korean)
        for key in ("goButton", "cancelAll", "applyNow", "legend", "queueColumns.suggestion",
                    "columns.inConfig", "units.mib"):
            self.assertIn(key, english)

    def test_korean_labels(self):
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        korean = script[script.index("ko: Object.freeze({"):]
        for text in ('assign: "배정"', 'direct: "내가 처리"', 'done: "끝(처리됨·답 불필요)"',
                     'exclude: "제외(큐에서 빼기)"', 'cancelAll: "모두 취소"', 'applyNow: "즉시 적용"',
                     'tabQueue: "큐"', 'tabLanes: "레인"', 'tabProposals: "범위"', 'tabService: "서비스"',
                     'when: "시각"', 'channel: "채널"', 'author: "작성자"', 'attention: "주의"',
                     'suggestion: "제안"', "이 채널 글이 계속 오면 Scope 탭에서 채널 제외를 제안할 수 있음",
                     '"배정 = 세션에 맡김 · 내가 처리 = " + direct + " 인박스로 · 끝 = 이미 처리했거나 답 불필요'
                     ' · 제외 = 이런 글은 큐에 안 와도 됨"'):
            self.assertIn(text, korean)
        english = script[:script.index("ko: Object.freeze({")]
        self.assertIsNone(re.search(r"[가-힣]", english))
        page = (STATIC / "console.html").read_text(encoding="utf-8")
        self.assertIn('data-text="keysHelp"', page)
        self.assertNotIn("bulk", script + page + (STATIC / "console.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
