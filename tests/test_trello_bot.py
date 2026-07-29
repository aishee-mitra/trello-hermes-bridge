import base64
import hashlib
import hmac
import json
import unittest

import trello_bot


class Config(trello_bot.Config):
    pass


def config():
    return trello_bot.Config(
        api_key="key",
        token="token",
        webhook_secret="secret",
        callback_url="https://example.test/webhook",
        board_id="board",
        agent_member_id="agent-id",
        agent_username="aishee",
        manager_member_id="manager-id",
        manager_username="sayan",
        list_in_progress="doing",
        list_blocked="blocked",
        list_done="done",
    )


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


if __name__ == "__main__":
    unittest.main()
