import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trello_bot


class Config(trello_bot.Config):
    pass


def config(**overrides):
    temp_dir = tempfile.mkdtemp(prefix="trello-hermes-tests-", dir=tempfile.gettempdir())
    values = dict(
        api_key="key",
        token="token",
        webhook_secret="secret",
        callback_url="https://example.test/webhook",
        board_id="board",
        agent_member_id="agent-id",
        agent_username="aishee",
        manager_member_id="manager-id",
        manager_username="sayan",
        list_doing="doing",
        list_stuck="stuck",
        list_done="done",
        list_dropped="dropped",
        project_dir=temp_dir,
    )
    values.update(overrides)
    return trello_bot.Config(**values)


class FakeTrelloClient:
    def __init__(self, card):
        self.card = card
        self.comments = []
        self.moves = []
        self.assignments = []

    def get_card(self, card_id_value, max_comments):
        return dict(self.card)

    def add_comment(self, card_id_value, text):
        self.comments.append((card_id_value, text))

    def move_card(self, card_id_value, list_id):
        self.moves.append((card_id_value, list_id))

    def assign_member(self, card_id_value, member_id):
        self.assignments.append((card_id_value, member_id))


class TrelloBotTests(unittest.TestCase):
    def test_trello_signature_uses_raw_body_and_callback_url(self):
        body = b'{"id":"action"}'
        secret = "secret"
        callback = "https://example.test/webhook"
        signature = base64.b64encode(
            hmac.new(secret.encode(), body + callback.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertTrue(trello_bot.verify_webhook_signature(body, signature, secret, callback))
        self.assertFalse(trello_bot.verify_webhook_signature(body, signature, secret, callback + "/"))
        self.assertFalse(trello_bot.verify_webhook_signature(body, "bad", secret, callback))

    def test_assignment_is_an_explicit_trigger(self):
        action = {"type": "addMemberToCard", "data": {"card": {"id": "card"}, "idMember": "agent-id"}}
        self.assertEqual(trello_bot.is_agent_trigger(action, config()), (True, "assigned"))

    def test_other_member_and_other_actions_are_ignored(self):
        other_member = {"type": "addMemberToCard", "data": {"card": {"id": "card"}, "idMember": "other"}}
        moved = {"type": "updateCard", "data": {"card": {"id": "card"}}}
        foreign_board = {
            "type": "addMemberToCard",
            "data": {"board": {"id": "foreign-board"}, "card": {"id": "card"}, "idMember": "agent-id"},
        }
        self.assertEqual(trello_bot.is_agent_trigger(other_member, config()), (False, "other"))
        self.assertEqual(trello_bot.is_agent_trigger(moved, config()), (False, "other"))
        self.assertEqual(trello_bot.is_agent_trigger(foreign_board, config()), (False, "other"))

    def test_mention_uses_configured_agent_username(self):
        action = {"type": "commentCard", "data": {"card": {"id": "card"}, "text": "@aishee please investigate"}}
        self.assertEqual(trello_bot.is_agent_trigger(action, config()), (True, "mentioned"))
        wrong = {"type": "commentCard", "data": {"card": {"id": "card"}, "text": "@someone please investigate"}}
        self.assertEqual(trello_bot.is_agent_trigger(wrong, config()), (False, "other"))

    def test_deduplicator_accepts_after_window(self):
        now = [100.0]
        dedup = trello_bot.Deduplicator(window_seconds=5, clock=lambda: now[0])
        self.assertTrue(dedup.accept("card:assigned"))
        self.assertFalse(dedup.accept("card:assigned"))
        now[0] = 106.0
        self.assertTrue(dedup.accept("card:assigned"))

    def test_card_and_member_payload_helpers(self):
        action = {
            "type": "addMemberToCard",
            "data": {"card": {"id": "abc"}, "member": {"id": "agent-id"}},
        }
        self.assertEqual(trello_bot.card_id(action), "abc")
        self.assertEqual(trello_bot.member_id_from_action(action), "agent-id")
        self.assertEqual(trello_bot.dedup_key(action, "assigned"), "abc:assigned")

    def test_trello_webhook_envelope_is_unwrapped(self):
        payload = {
            "model": {"id": "board", "name": "Aishee and Me"},
            "action": {
                "type": "commentCard",
                "data": {
                    "card": {"id": "card"},
                    "text": "@aishee please investigate",
                },
            },
        }
        action = trello_bot.normalize_webhook_payload(payload)
        self.assertEqual(action["type"], "commentCard")
        self.assertEqual(trello_bot.board_id(action), "board")
        self.assertEqual(trello_bot.is_agent_trigger(action, config()), (True, "mentioned"))

    def test_dedup_uses_comment_id_for_comment_triggers(self):
        action = {
            "id": "comment-1",
            "type": "commentCard",
            "data": {"card": {"id": "card"}, "text": "@aishee first"},
        }
        self.assertEqual(
            trello_bot.dedup_key(action, "mentioned"), "card:mentioned:comment-1"
        )
        second = {
            "id": "comment-2",
            "type": "commentCard",
            "data": {"card": {"id": "card"}, "text": "@aishee cancel"},
        }
        self.assertEqual(
            trello_bot.dedup_key(second, "mentioned"), "card:mentioned:comment-2"
        )

    def test_already_picked_up_skips_duplicate_pickup_comment(self):
        cfg = config()
        bridge = trello_bot.Bridge(cfg)
        card = {
            "id": "card",
            "comments": [
                {"data": {"text": "Picked up by @aishee. I'll work this and report back here."}},
            ],
        }
        self.assertTrue(bridge._already_picked_up(card))

        card = {
            "id": "card",
            "comments": [
                {"data": {"text": "Picked up by @aishee. I'll work this and report back here."}},
                {"data": {"text": "Some other comment"}},
            ],
        }
        self.assertTrue(bridge._already_picked_up(card))

        card = {"id": "card", "comments": [{"data": {"text": "Some other comment"}}]}
        self.assertFalse(bridge._already_picked_up(card))

        self.assertFalse(bridge._already_picked_up({"id": "card"}))

    def test_agent_self_mention_does_not_trigger(self):
        cfg = config()
        # Agent-authored comment mentioning itself should not trigger
        agent_self_mention = {
            "type": "commentCard",
            "id": "comment-3",
            "data": {"card": {"id": "card-1"}, "text": "@aishee update: still working"},
            "memberCreator": {"id": "agent-id", "username": "aishee"}
        }
        self.assertEqual(trello_bot.is_agent_trigger(agent_self_mention, cfg), (False, "other"))
        self.assertTrue(trello_bot.is_agent_authored_comment(agent_self_mention, cfg))

    def test_model_label_parsing_edge_cases(self):
        cases = [
            ("model:openrouter:anthropic/claude-3.5-sonnet", "openrouter", "anthropic/claude-3.5-sonnet"),
            ("model::gpt-4", None, "gpt-4"),
            ("model:tencent:hy3:free", "tencent", "hy3:free"),
            ("model:openrouter:tencent/hy3:free", "openrouter", "tencent/hy3:free"),
            ("model:openai:gpt-4", "openai", "gpt-4"),
            ("model:gpt-4", None, "gpt-4"),
        ]
        for label, expected_provider, expected_model in cases:
            model_value = label[6:].strip()
            parts = model_value.split(":", 1)
            if len(parts) == 2 and parts[0].strip():
                provider = parts[0].strip()
                model = parts[1].strip()
            else:
                provider = None
                model = model_value.strip(": ")
            self.assertEqual(provider, expected_provider, msg=f"label={label}")
            self.assertEqual(model, expected_model, msg=f"label={label}")

    def test_spawn_worker_skips_cards_already_in_terminal_list(self):
        cfg = config()
        client = FakeTrelloClient({"id": "card", "idList": cfg.list_done, "desc": "", "labels": []})
        bridge = trello_bot.Bridge(cfg, client=client)

        with patch("trello_bot.subprocess.Popen") as popen:
            result = bridge.spawn_worker("card", "assigned")

        self.assertIsNone(result)
        popen.assert_not_called()

    def test_wait_for_worker_comments_when_card_stays_in_doing(self):
        cfg = config()
        client = FakeTrelloClient({"id": "card", "idList": cfg.list_doing, "desc": "", "labels": []})
        bridge = trello_bot.Bridge(cfg, client=client)

        with patch.object(bridge, "spawn_worker", return_value=None) as spawn_worker:
            bridge._handle_worker_exit("card")
            bridge._handle_worker_exit("card")

        self.assertEqual(spawn_worker.call_count, 1)
        self.assertEqual(len(client.comments), 1)
        self.assertIn("did not move to Done, Stuck, or Dropped", client.comments[0][1])

    def test_incomplete_run_retries_once(self):
        cfg = config()
        client = FakeTrelloClient({"id": "card", "idList": cfg.list_doing, "desc": "", "labels": []})
        bridge = trello_bot.Bridge(cfg, client=client)

        with patch.object(bridge, "spawn_worker", return_value=None) as spawn_worker:
            bridge._handle_worker_exit("card")

        self.assertEqual(spawn_worker.call_count, 1)

    def test_stale_run_posts_manual_review_comment(self):
        cfg = config(stale_run_timeout_seconds=0)
        client = FakeTrelloClient({"id": "card", "idList": cfg.list_doing, "desc": "", "labels": []})
        bridge = trello_bot.Bridge(cfg, client=client)

        bridge._mark_stale_run("card")

        self.assertEqual(len(client.comments), 1)
        self.assertIn("manual review", client.comments[0][1].lower())

    def test_configurable_retry_limit_is_respected(self):
        cfg = config(max_retries=2)
        client = FakeTrelloClient({"id": "card", "idList": cfg.list_doing, "desc": "", "labels": []})
        bridge = trello_bot.Bridge(cfg, client=client)

        with patch.object(bridge, "spawn_worker", return_value=None) as spawn_worker:
            bridge._handle_worker_exit("card")
            bridge._handle_worker_exit("card")
            bridge._handle_worker_exit("card")

        self.assertEqual(spawn_worker.call_count, 2)

    def test_bridge_loads_retry_state_from_disk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "bridge_state.json"
            state_path.write_text(json.dumps({"card": {"retry_count": 1, "status": "running"}}), encoding="utf-8")
            cfg = trello_bot.Config(
                api_key="key",
                token="token",
                webhook_secret="secret",
                callback_url="https://example.test/webhook",
                board_id="board",
                agent_member_id="agent-id",
                agent_username="aishee",
                manager_member_id="manager-id",
                manager_username="sayan",
                list_doing="doing",
                list_stuck="stuck",
                list_done="done",
                list_dropped="dropped",
                project_dir=temp_dir,
            )
            bridge = trello_bot.Bridge(cfg, client=FakeTrelloClient({"id": "card", "idList": cfg.list_doing, "desc": "", "labels": []}))
            self.assertEqual(bridge._retry_counts.get("card"), 1)
            self.assertEqual(bridge._run_state_status.get("card"), "running")

    def test_bridge_state_file_is_pruned_to_size_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = trello_bot.Config(
                api_key="key",
                token="token",
                webhook_secret="secret",
                callback_url="https://example.test/webhook",
                board_id="board",
                agent_member_id="agent-id",
                agent_username="aishee",
                manager_member_id="manager-id",
                manager_username="sayan",
                list_doing="doing",
                list_stuck="stuck",
                list_done="done",
                list_dropped="dropped",
                project_dir=temp_dir,
                bridge_state_max_bytes=80,
            )
            bridge = trello_bot.Bridge(cfg, client=FakeTrelloClient({"id": "card", "idList": cfg.list_doing, "desc": "", "labels": []}))
            bridge._retry_counts = {f"card-{index}": 1 for index in range(10)}
            bridge._save_state()

            state_path = Path(temp_dir) / "bridge_state.json"
            self.assertTrue(state_path.exists())
            self.assertLessEqual(state_path.stat().st_size, cfg.bridge_state_max_bytes)


if __name__ == "__main__":
    unittest.main()
