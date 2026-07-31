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
    worker_timeout_seconds: int = 900
    worker_log_retention_days: int = 14
    worker_log_max_files: int = 50
    worker_log_max_size_bytes: int = 10 * 1024 * 1024
    bridge_state_max_bytes: int = 64 * 1024

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
            worker_timeout_seconds=int(optional("WORKER_TIMEOUT_SECONDS", "900")),
            worker_log_retention_days=int(optional("WORKER_LOG_RETENTION_DAYS", "14")),
            worker_log_max_files=int(optional("WORKER_LOG_MAX_FILES", "50")),
            worker_log_max_size_bytes=int(optional("WORKER_LOG_MAX_SIZE_BYTES", str(10 * 1024 * 1024))),
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


def normalize_pickup_text(text: str) -> str:
    return " ".join(text.replace("’", "'").replace("`", "'").split())


def is_agent_trigger(action: dict[str, Any], cfg: Config) -> tuple[bool, str]:
    """Return whether an action is an explicit assignment or mention trigger."""
    # Agent-authored comments should never trigger - checked before this function, but double-check
    if action_type(action) == "commentCard" and is_agent_authored_comment(action, cfg):
        return False, "other"
    
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


def is_agent_authored_comment(action: dict[str, Any], cfg: Config) -> bool:
    member = action.get("data") or {}
    member = member.get("member") or member.get("idMember") or {}
    if isinstance(member, dict) and member.get("id") == cfg.agent_member_id:
        return True
    author = action.get("memberCreator") or {}
    return bool(author.get("id") == cfg.agent_member_id)


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

    def assign_member(self, card_id_value: str, member_id: str) -> Any:
        return self.request("POST", f"/cards/{quote(card_id_value)}/idMembers", {"value": member_id})


class Bridge:
    def __init__(self, cfg: Config, client: TrelloClient | None = None):
        self.cfg = cfg
        self.client = client or TrelloClient(cfg)
        self.dedup = Deduplicator(cfg.dedup_window_seconds)
        self._active_workers: dict[str, subprocess.Popen[Any]] = {}
        self._retry_counts: dict[str, int] = {}
        self._log_handles: dict[str, Any] = {}
        self._state_path = Path(self.cfg.project_dir or Path(__file__).parent) / "bridge_state.json"
        self.logger = logging.getLogger("trello-bot")
        self._load_state()

    def _cancel_keywords(self, action: dict[str, Any]) -> bool:
        text = (comment_text(action) or "").lower()
        return any(token in text for token in ("cancel this", "drop this", "abort", "stop this", "disregard"))

    def _already_picked_up(self, card: dict[str, Any]) -> bool:
        normalized_pickups = {
            normalize_pickup_text(f"Picked up by @{self.cfg.agent_username}. I'll work this and report back here."),
            normalize_pickup_text(f"Picked up by @{self.cfg.agent_username}. I’ll work this and report back here."),
        }
        for comment in card.get("comments") or []:
            text = normalize_pickup_text(str(comment.get("data", {}).get("text") or comment.get("text") or ""))
            if text.strip() in normalized_pickups:
                return True
        return False

    def _terminal_list_ids(self) -> set[str]:
        return {self.cfg.list_stuck, self.cfg.list_done, self.cfg.list_dropped}

    def _is_terminal_state(self, card: dict[str, Any]) -> bool:
        return str(card.get("idList", "")) in self._terminal_list_ids()

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.logger.warning("failed to load bridge state from %s", self._state_path)
            return
        for card_id_value, entry in (data or {}).items():
            if isinstance(entry, dict):
                retry_count = entry.get("retry_count")
                if isinstance(retry_count, int):
                    self._retry_counts[str(card_id_value)] = retry_count

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {card_id_value: {"retry_count": retry_count} for card_id_value, retry_count in self._retry_counts.items()}
            payload = dict(sorted(payload.items(), key=lambda item: item[0]))
            content = json.dumps(payload, indent=2, sort_keys=True)
            if len(content.encode("utf-8")) > self.cfg.bridge_state_max_bytes:
                limited_payload = {}
                for card_id_value, entry in list(payload.items()):
                    limited_payload[card_id_value] = entry
                    candidate = json.dumps(limited_payload, indent=2, sort_keys=True)
                    if len(candidate.encode("utf-8")) > self.cfg.bridge_state_max_bytes:
                        limited_payload.pop(card_id_value)
                        break
                payload = limited_payload
                content = json.dumps(payload, indent=2, sort_keys=True)
            self._state_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.logger.warning("failed to persist bridge state to %s: %s", self._state_path, exc)

    def _prune_worker_logs(self) -> None:
        log_dir = Path(self.cfg.project_dir or Path(__file__).parent) / "workers"
        if not log_dir.exists():
            return
        try:
            now = time.time()
            cutoff = now - (self.cfg.worker_log_retention_days * 24 * 60 * 60)
            log_files = [path for path in log_dir.glob("*.log") if path.is_file()]
            for path in log_files:
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        continue
                except FileNotFoundError:
                    continue
            remaining = sorted(log_files, key=lambda path: path.stat().st_mtime, reverse=True)
            while len(remaining) > self.cfg.worker_log_max_files:
                oldest = remaining.pop()
                try:
                    oldest.unlink()
                except FileNotFoundError:
                    continue
        except Exception as exc:
            self.logger.error("failed to prune worker logs: %s", exc)

    def _rotate_worker_log(self, log_path: Path) -> None:
        if not log_path.exists() or log_path.stat().st_size < self.cfg.worker_log_max_size_bytes:
            return
        rotated_path = log_path.with_suffix(log_path.suffix + ".1")
        if rotated_path.exists():
            rotated_path.unlink()
        log_path.rename(rotated_path)

    def _handle_worker_exit(self, card_id_value: str) -> None:
        try:
            card = self.client.get_card(card_id_value, 0)
        except Exception as exc:
            self.logger.error("could not inspect card %s after worker exit: %s", card_id_value[:8], exc)
            return
        if self._is_terminal_state(card):
            self.logger.info("worker completed for card %s; current list %s is terminal", card_id_value[:8], card.get("idList"))
            self._retry_counts.pop(card_id_value, None)
            self._save_state()
            return
        retries = self._retry_counts.get(card_id_value, 0)
        if retries < 1:
            self._retry_counts[card_id_value] = retries + 1
            self._save_state()
            self.logger.warning("worker exited without moving card %s to a terminal list; retrying once", card_id_value[:8])
            self.spawn_worker(card_id_value, "retry")
            return
        self.logger.warning("worker exited without moving card %s to a terminal list; no retries remain", card_id_value[:8])
        self._retry_counts.pop(card_id_value, None)
        try:
            self.client.add_comment(
                card_id_value,
                "Worker exited before the card reached a terminal state. The card did not reach a terminal state; please review it manually.",
            )
        except Exception as exc:
            self.logger.error("failed to comment on incomplete run for card %s: %s", card_id_value[:8], exc)
        self._save_state()

    def process(self, action: dict[str, Any]) -> str:
        # First check if this is an agent-authored comment - ignore immediately to prevent loops
        if action_type(action) == "commentCard" and is_agent_authored_comment(action, self.cfg):
            self.logger.info("ignored agent-authored comment for card %s", card_id(action)[:8])
            return "ignored"
        
        triggered, signal = is_agent_trigger(action, self.cfg)
        if not triggered:
            self.logger.info(
                "ignored webhook type=%s card=%s board=%s text=%r",
                action_type(action), card_id(action), board_id(action), comment_text(action)[:120],
            )
            return "ignored"
        card_id_value = card_id(action)
        key = dedup_key(action, signal)
        if not self.dedup.accept(key):
            self.logger.info("duplicate trigger ignored: %s", key)
            return "duplicate"
        if not card_id_value:
            self.logger.warning("trigger had no card id")
            return "invalid"
        if self._worker_in_flight(card_id_value):
            self.logger.info("worker already running for card %s", card_id_value[:8])
            return "duplicate"
        if signal == "mentioned" and self._cancel_keywords(action):
            self.logger.info("cancellation detected, spawning worker to handle")
        self.spawn_worker(card_id_value, signal)
        return "spawned"

    def spawn_worker(self, card_id_value: str, signal: str) -> subprocess.Popen[Any] | None:
        # Fetch card details to check for model override in labels and record origin list
        card = self.client.get_card(card_id_value, 5)
        if self._is_terminal_state(card):
            self.logger.info(
                "skipping worker for card %s because it is already in terminal list %s",
                card_id_value[:8],
                card.get("idList"),
            )
            return None
        origin_list = card.get("idList")
        
        # Check for model override in labels: look for label with pattern "model:<provider>:<model>"
        provider_override = None
        model_override = None
        for label in (card.get("labels") or []):
            label_name = label.get("name", "")
            if label_name.lower().startswith("model:"):
                model_value = label_name[6:].strip()  # extract after "model:"
                parts = model_value.split(":", 1)
                if len(parts) == 2 and parts[0].strip():
                    provider_override = parts[0].strip()
                    model_override = parts[1].strip()
                else:
                    provider_override = None
                    model_override = model_value.strip(": ")
                self.logger.info("model override from label: provider=%s model=%s", provider_override, model_override)
                break
        
        command_hint = str(Path(__file__).resolve())
        prompt = f"""Work the Trello card below (card id: {card_id_value}). This run was triggered by {signal}.

Card description: {card.get('desc', '')}
Origin list id: {origin_list}

First, fetch the card details using the CLI:
  python3 {command_hint} get-card {card_id_value}

Required first actions (execute these after fetching card details):
1. Post one concise pickup comment on the card based on the card name/context; do NOT use a fixed template sentence. A good pattern is: start with the card title or intent in your own words, then note the trigger and next step.
2. Move the card to the configured Doing list:
   python3 {command_hint} move {card_id_value} {self.cfg.list_doing}

After completing the required first actions, continue with the actual work. Use the local Trello bridge CLI for all write-back; it reads credentials from local config.env and does not require secrets in this prompt:
  python3 {command_hint} comment CARD_ID TEXT
  python3 {command_hint} move CARD_ID LIST_ID
  python3 {command_hint} assign CARD_ID MEMBER_ID

Progress reporting:
- After completing meaningful chunks, post ONE concise progress comment:
  what changed, what’s blocked, and what’s next.
- Do not comment after every tiny step or flood the card.
- For very long tasks, you may post up to 5 progress comments in total; aim for 2 well-placed updates plus pickup and completion.

Configured lifecycle list IDs:
  Doing: {self.cfg.list_doing}
  Stuck: {self.cfg.list_stuck}
  Done: {self.cfg.list_done}
  Dropped: {self.cfg.list_dropped}

Configured member IDs:
  Manager: {self.cfg.manager_member_id} (@{self.cfg.manager_username})

Mandatory transition rules — these are hard constraints, not suggestions:
- If the task is cancelled or out of scope, explain briefly and move it to Dropped.
- If the task is blocked/stuck, you MUST perform ALL three steps in sequence:
  1. Post exactly one comment that clearly states what is blocking progress and what you need from @{self.cfg.manager_username}. Mention @{self.cfg.manager_username} inline so they are notified.
  2. Move the card to Stuck ({self.cfg.list_stuck})
  3. Assign the card to @{self.cfg.manager_username} using: python3 {command_hint} assign CARD_ID {self.cfg.manager_member_id}
- After completing any work (success, blocker, or cancel), your FINAL action must be one of these terminal sequences. No text-only response is a valid end state.
  Success: post one completion comment summarizing what was done, then move card to Done ({self.cfg.list_done}), then unassign yourself from the card.
  Blocker: execute the full Stuck sequence above.
  Cancel/drop: move card to Dropped ({self.cfg.list_dropped}), explain briefly.

Do NOT leave the card in Doing after reporting a blocker. Execute all three Stuck actions in sequence.
Keep comments concise and do not expose API keys, tokens, or internal IDs in manager-facing text.
"""
        cmd = [self.cfg.hermes_bin, "chat", "-q", prompt, "--cli", "-Q", "--accept-hooks", "--yolo"]
        if provider_override:
            cmd.extend(["--provider", provider_override])
        if model_override:
            cmd.extend(["--model", model_override])
            self.logger.info("spawning worker with provider=%s model=%s", provider_override, model_override)
        self.logger.info("spawning Hermes worker for card %s", card_id_value[:8])
        worker_log = Path(self.cfg.project_dir or Path(__file__).parent) / "workers" / f"{card_id_value[:8]}.log"
        worker_log.parent.mkdir(parents=True, exist_ok=True)
        self._prune_worker_logs()
        self._rotate_worker_log(worker_log)
        log_handle = worker_log.open("ab")
        proc = subprocess.Popen(
            cmd,
            cwd=self.cfg.project_dir or None,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "TRELLO_BOT_CONFIG": str(config_path())},
        )
        self._active_workers[card_id_value] = proc
        self._log_handles[card_id_value] = log_handle
        threading.Thread(
            target=self._wait_for_worker,
            args=(card_id_value, proc),
            name=f"trello-worker-wait-{card_id_value[:8]}",
            daemon=True,
        ).start()
        # Start timeout watcher — kill the worker if it exceeds the time limit
        threading.Thread(
            target=self._watch_worker_timeout,
            args=(card_id_value, proc),
            name=f"trello-timeout-watch-{card_id_value[:8]}",
            daemon=True,
        ).start()
        return proc

    def _watch_worker_timeout(self, card_id_value: str, proc: subprocess.Popen[Any]) -> None:
        deadline = time.monotonic() + self.cfg.worker_timeout_seconds
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(30)
        if proc.poll() is None:
            self.logger.warning(
                "worker for card %s exceeded timeout of %ds (started at %s), terminating",
                card_id_value[:8], self.cfg.worker_timeout_seconds,
                time.strftime("%H:%M:%S", time.gmtime(time.monotonic() - self.cfg.worker_timeout_seconds)),
            )
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            # Post a timeout comment on the card so the manager knows what happened
            try:
                self.client.add_comment(
                    card_id_value,
                    f"Worker timed out after {self.cfg.worker_timeout_seconds}s — card left in its current state. Please pick up manually if needed.",
                )
            except Exception as exc:
                self.logger.error("failed to post timeout comment for card %s: %s", card_id_value[:8], exc)

    def _wait_for_worker(self, card_id_value: str, proc: subprocess.Popen[Any]) -> None:
        try:
            proc.wait()
            self._handle_worker_exit(card_id_value)
        finally:
            self._active_workers.pop(card_id_value, None)

    def _worker_in_flight(self, card_id_value: str) -> bool:
        proc = self._active_workers.get(card_id_value)
        if proc and proc.poll() is None:
            return True
        if proc:
            self._active_workers.pop(card_id_value, None)
        return False


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
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "comment", "move", "assign", "get-card"))
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
    if not args.card_id:
        parser.error(f"{args.command} requires CARD_ID")
    if args.command == "comment":
        if not args.value:
            parser.error("comment requires VALUE")
        client.add_comment(args.card_id, args.value)
    elif args.command == "move":
        if not args.value:
            parser.error("move requires LIST_ID")
        client.move_card(args.card_id, args.value)
    elif args.command == "assign":
        if not args.value:
            parser.error("assign requires MEMBER_ID")
        client.assign_member(args.card_id, args.value)
    elif args.command == "get-card":
        max_comments = int(args.value) if args.value else 20
        card = client.get_card(args.card_id, max_comments)
        print(json.dumps(card, indent=2))
    else:
        parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
