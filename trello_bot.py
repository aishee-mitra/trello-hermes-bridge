#!/usr/bin/env python3
"""Trello -> Hermes bridge.

The service receives Trello board webhooks, filters explicit assignment/@mention
triggers, and launches a detached Hermes worker.  It also exposes small CLI
commands that workers can use for Trello write-back without receiving secrets in
their prompt.

No third-party dependencies are required.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Config:
    api_key: str
    token: str
    webhook_secret: str
    callback_url: str
    board_id: str
    agent_member_id: str
    agent_username: str
    manager_member_id: str
    manager_username: str
    list_doing: str
    list_stuck: str
    list_done: str
    list_dropped: str
    bind_host: str = "0.0.0.0"
    bind_port: int = 8787
    hermes_bin: str = "/home/aishee/.local/bin/hermes"
    hermes_model: str = ""
    project_dir: str = ""
    dedup_window_seconds: int = 300
    max_card_comments: int = 20

    @classmethod
    def from_env_file(cls, path: str | Path) -> "Config":
        values: dict[str, str] = {}
        with open(path, encoding="utf-8") as stream:
            for raw in stream:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value or value.startswith("REPLACE_WITH_"):
                raise ValueError(f"missing required configuration: {name}")
            return value

        def optional(name: str, default: str) -> str:
            return values.get(name, default).strip()

        return cls(
            api_key=required("TRELLO_API_KEY"),
            token=required("TRELLO_TOKEN"),
            webhook_secret=required("TRELLO_WEBHOOK_SECRET"),
            callback_url=required("TRELLO_CALLBACK_URL"),
            board_id=required("TRELLO_BOARD_ID"),
            agent_member_id=required("AGENT_TRELLO_MEMBER_ID"),
            agent_username=required("AGENT_TRELLO_USERNAME"),
            manager_member_id=required("MANAGER_TRELLO_MEMBER_ID"),
            manager_username=required("MANAGER_TRELLO_USERNAME"),
            list_doing=required("LIST_ID_DOING"),
            list_stuck=required("LIST_ID_STUCK"),
            list_done=required("LIST_ID_DONE"),
            list_dropped=required("LIST_ID_DROPPED"),
            bind_host=optional("BIND_HOST", "0.0.0.0"),
            bind_port=int(optional("BIND_PORT", "8787")),
            hermes_bin=optional("HERMES_BIN", "/home/aishee/.local/bin/hermes"),
            hermes_model=optional("HERMES_MODEL", ""),
            project_dir=optional("PROJECT_DIR", str(Path(__file__).parent)),
            dedup_window_seconds=int(optional("DEDUP_WINDOW_SECONDS", "300")),
            max_card_comments=int(optional("MAX_CARD_COMMENTS", "20")),
        )


def verify_webhook_signature(raw_body: bytes, header: str, secret: str, callback_url: str) -> bool:
    """Verify Trello's base64 HMAC-SHA1(body + exact callback URL) signature."""
    if not header:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw_body + callback_url.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, header.strip())


def action_type(action: dict[str, Any]) -> str:
    return str(action.get("type") or action.get("data", {}).get("action", {}).get("type") or "")


def card_id(action: dict[str, Any]) -> str:
    data = action.get("data") or {}
    card = data.get("card") or {}
    return str(card.get("id") or data.get("card_id") or "")


def board_id(action: dict[str, Any]) -> str:
    data = action.get("data") or {}
    board = data.get("board") or {}
    return str(board.get("id") or data.get("idBoard") or "")


def member_id_from_action(action: dict[str, Any]) -> str:
    data = action.get("data") or {}
    member = data.get("idMember") or data.get("member") or {}
    if isinstance(member, dict):
        return str(member.get("id") or member.get("idMember") or "")
    return str(member)


def comment_text(action: dict[str, Any]) -> str:
    data = action.get("data") or {}
    text = data.get("text") or data.get("comment") or ""
    return str(text)


def normalize_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap Trello's {action, model} webhook envelope."""
    nested = payload.get("action")
    if not isinstance(nested, dict):
        return payload
    action = dict(nested)
    data = dict(action.get("data") or {})
    model = payload.get("model") or {}
    if model.get("id") and not data.get("board"):
        data["board"] = {"id": model.get("id"), "name": model.get("name", "")}
    action["data"] = data
    return action


def is_agent_trigger(action: dict[str, Any], cfg: Config) -> tuple[bool, str]:
    """Return whether an action is an explicit assignment or mention trigger."""
    payload_board_id = board_id(action)
    if payload_board_id and payload_board_id != cfg.board_id:
        return False, "other"
    kind = action_type(action)
    if kind == "addMemberToCard" and member_id_from_action(action) == cfg.agent_member_id:
        return True, "assigned"
    if kind == "commentCard" and re.search(
        rf"@{re.escape(cfg.agent_username)}\b", comment_text(action), re.IGNORECASE
    ):
        return True, "mentioned"
    return False, "other"


def dedup_key(action: dict[str, Any], signal: str) -> str:
    action_id = action.get("id")
    if action_type(action) == "commentCard" and action_id:
        return f"{card_id(action)}:{signal}:{action_id}"
    return f"{card_id(action)}:{signal}"


class Deduplicator:
    def __init__(self, window_seconds: int = 300, clock=time.monotonic):
        self.window_seconds = window_seconds
        self.clock = clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            previous = self._seen.get(key)
            self._seen[key] = now
            for old_key, timestamp in list(self._seen.items()):
                if now - timestamp > self.window_seconds:
                    del self._seen[old_key]
            return previous is None or now - previous > self.window_seconds


class TrelloClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base_url = "https://api.trello.com/1"

    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> Any:
        query = {"key": self.cfg.api_key, "token": self.cfg.token}
        query.update(params or {})
        url = f"{self.base_url}{path}?{urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "trello-hermes-bridge/0.1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Trello API {method} {path}: HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Trello API {method} {path}: {exc.reason}") from exc

    def get_card(self, card_id_value: str, max_comments: int) -> dict[str, Any]:
        card = self.request("GET", f"/cards/{quote(card_id_value)}", {
            "fields": "name,desc,idBoard,idList,url,shortUrl,labels,idMembers",
            "members": "true",
            "member_fields": "fullName,username",
            "checklists": "all",
        })
        actions = self.request("GET", f"/cards/{quote(card_id_value)}/actions", {
            "filter": "commentCard",
            "limit": str(max_comments),
        })
        card["comments"] = actions if isinstance(actions, list) else []
        return card

    def add_comment(self, card_id_value: str, text: str) -> Any:
        return self.request("POST", f"/cards/{quote(card_id_value)}/actions/comments", {"text": text})

    def move_card(self, card_id_value: str, list_id: str) -> Any:
        return self.request("PUT", f"/cards/{quote(card_id_value)}", {"idList": list_id})


class Bridge:
    def __init__(self, cfg: Config, client: TrelloClient | None = None):
        self.cfg = cfg
        self.client = client or TrelloClient(cfg)
        self.dedup = Deduplicator(cfg.dedup_window_seconds)
        self.logger = logging.getLogger("trello-bot")

    def _cancel_keywords(self, action: dict[str, Any]) -> bool:
        text = (comment_text(action) or "").lower()
        return any(token in text for token in ("cancel this", "drop this", "abort", "stop this", "disregard"))

    def process(self, action: dict[str, Any]) -> str:
        triggered, signal = is_agent_trigger(action, self.cfg)
        if not triggered:
            self.logger.info(
                "ignored webhook type=%s card=%s board=%s text=%r",
                action_type(action), card_id(action), board_id(action), comment_text(action)[:120],
            )
            return "ignored"
        key = dedup_key(action, signal)
        if not self.dedup.accept(key):
            self.logger.info("duplicate trigger ignored: %s", key)
            return "duplicate"
        card_id_value = card_id(action)
        if not card_id_value:
            self.logger.warning("trigger had no card id")
            return "invalid"
        card = self.client.get_card(card_id_value, self.cfg.max_card_comments)
        self.client.move_card(card_id_value, self.cfg.list_doing)
        if signal == "mentioned" and self._cancel_keywords(action):
            self.client.move_card(card_id_value, self.cfg.list_dropped)
            self.client.add_comment(
                card_id_value,
                f"Cancelled on request by @{self.cfg.manager_username}.",
            )
            return "cancelled"
        self.spawn_worker(card, signal)
        return "spawned"

    def spawn_worker(self, card: dict[str, Any], signal: str) -> subprocess.Popen[Any]:
        card_json = json.dumps(card, ensure_ascii=False, indent=2)
        command_hint = str(Path(__file__).resolve())
        prompt = f"""Work the Trello card below. This run was triggered by {signal}.

Card context:
{card_json}

Use the local Trello bridge CLI for write-back; it reads credentials from its local
config and does not require secrets in this prompt:
  python3 {command_hint} comment CARD_ID TEXT
  python3 {command_hint} move CARD_ID LIST_ID

Use these configured lifecycle list IDs with the move command:
  Doing: {self.cfg.list_doing}
  Stuck: {self.cfg.list_stuck}
  Done: {self.cfg.list_done}
  Dropped: {self.cfg.list_dropped}
Move the card to the configured Done list when complete. If blocked, add a
concise comment explaining the blocker, mention @{self.cfg.manager_username},
and move it to the configured Stuck list. If the work is cancelled or out of
scope, explain why briefly and move it to Dropped. Keep comments concise and
do not expose API keys, tokens, or internal IDs in manager-facing text.
"""
        cmd = [self.cfg.hermes_bin, "chat", "-q", prompt, "--cli", "-Q", "--accept-hooks", "--yolo"]
        if self.cfg.hermes_model:
            cmd.extend(["--model", self.cfg.hermes_model])
        self.logger.info("spawning Hermes worker for card %s", card.get("id", "")[:8])
        return subprocess.Popen(
            cmd,
            cwd=self.cfg.project_dir or None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "TRELLO_BOT_CONFIG": str(config_path())},
        )


def config_path() -> Path:
    return Path(os.environ.get("TRELLO_BOT_CONFIG", Path(__file__).with_name("config.env")))


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    bridge: Bridge

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.getLogger("trello-bot.http").info(fmt, *args)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path in ("/", "/health", "/webhook"):
            payload = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/webhook":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        signature = self.headers.get("X-Trello-Webhook", "")
        if not verify_webhook_signature(
            raw_body, signature, self.bridge.cfg.webhook_secret, self.bridge.cfg.callback_url
        ):
            self.send_error(403, "invalid webhook signature")
            return
        try:
            action = normalize_webhook_payload(json.loads(raw_body))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "invalid JSON")
            return

        # Trello expects a fast successful callback. Do not hold its request open
        # while we fetch the card, write the receipt, and launch Hermes.
        threading.Thread(
            target=self._process_async,
            args=(action,),
            name="trello-webhook-work",
            daemon=True,
        ).start()
        payload = b'{"status":"accepted"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _process_async(self, action: dict[str, Any]) -> None:
        try:
            result = self.bridge.process(action)
            self.bridge.logger.info("webhook processed: %s", result)
        except Exception:
            self.bridge.logger.exception("webhook processing failed")


def run_server(cfg: Config) -> None:
    bridge = Bridge(cfg)
    handler_type = type("ConfiguredWebhookHandler", (WebhookHandler,), {"bridge": bridge})
    server = http.server.ThreadingHTTPServer((cfg.bind_host, cfg.bind_port), handler_type)
    logging.info("listening on %s:%s", cfg.bind_host, cfg.bind_port)
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Trello -> Hermes bridge")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "comment", "move"))
    parser.add_argument("card_id", nargs="?")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--config", default=None, help="path to config.env")
    args = parser.parse_args()
    if args.config:
        os.environ["TRELLO_BOT_CONFIG"] = args.config
    cfg = Config.from_env_file(config_path())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = TrelloClient(cfg)
    if args.command == "serve":
        run_server(cfg)
        return 0
    if not args.card_id or not args.value:
        parser.error(f"{args.command} requires CARD_ID and VALUE")
    if args.command == "comment":
        client.add_comment(args.card_id, args.value)
    else:
        client.move_card(args.card_id, args.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
