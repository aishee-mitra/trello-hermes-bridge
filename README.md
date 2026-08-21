# 🤖 Trello → Hermes Bridge

<p align="center">
  <img src="https://img.shields.io/badge/status-active-success" alt="status">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="license">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/webhooks-HMAC--SHA1-orange" alt="webhooks">
</p>

Turn explicit Trello work allocation into a **detached Hermes Agent run** — no Butler, no polling, no noisy automation.

> **TL;DR:** Assign the agent or mention it on a card → Trello webhook fires → bridge verifies + queues work → Hermes executes asynchronously → card is updated automatically.

---

## ✨ Features

- ✅ **Fire-and-forget** — webhook acknowledges immediately; Hermes runs in the background
- 🔒 **HMAC-SHA1 signature verification** — only valid Trello webhooks are accepted
- 🎯 **Explicit triggers only** — assignment or `@agent-username` mention only
- 🧠 **Async worker spawning** — detached Hermes worker gets full card context
- 📋 **Lifecycle management** — `Todo` → `Doing` → `Stuck` → `Done` → `Dropped`
- 🔁 **Deduplication** — same card/trigger ignored within a configurable window
- 🌐 **Tunnel friendly** — binds to configurable `BIND_HOST:<port>`; works with Cloudflare Tunnel, ngrok, Tailscale Funnel, etc.
- 🏷️ **Per-task model override** — use labels like `model:provider/model-name` to override the default model for a specific card
- 📦 **Zero external dependencies** — stdlib only
- 🚀 **systemd user service** — auto-start + linger
- 🗂️ **Multi-board** — one service can serve multiple Trello boards

---

## 🚀 Quick Start

```bash
git clone https://github.com/aishee-mitra/trello-hermes-bridge.git
cd trello-hermes-bridge
cp config.env.example config.env
chmod 600 config.env
$EDITOR config.env
```

Fill `config.env`, then install the systemd user service:

```bash
mkdir -p ~/.config/systemd/user
cp trello-bot.service.example ~/.config/systemd/user/trello-bot.service
systemctl --user daemon-reload
systemctl --user enable --now trello-bot
systemctl --user status trello-bot
```

Expose the bridge with a tunnel on `BIND_HOST:BIND_PORT`, register Trello board webhook(s) to `https://<host>/webhook`, and you’re live.

## 🛠️ Automated Install (For Hermes Agents)

If you're another Hermes agent tasked with installing this bridge, **do not follow the Quick Start above**. Instead, read the comprehensive automated installation guide designed specifically for agents:

> **→ [Read the Agent Installation Guide](https://raw.githubusercontent.com/aishee-mitra/trello-hermes-bridge/main/SKILL_AUTOMATED_INSTALL.md)**

That guide walks you through:
- Dependency and environment checks
- Safe credential capture from the user
- Cloning and configuration (with validation)
- systemd service setup
- Bridge verification and webhook registration
- Troubleshooting and next steps

---

## 🛠️ Interactive Human Install

If you're following this manually:
- Clone the repo, copy `config.env.example` to `config.env`
- Fill in your Trello API credentials and member IDs (see Configuration section below)
- Install as a systemd user service (see Quick Start above)
- Expose via tunnel and register Trello webhook(s)

---

## 🗂️ Multi-Board Setup

The bridge can serve multiple Trello boards from one process.

### Legacy single-board mode

```env
TRELLO_BOARD_ID=<board-id>
LIST_ID_DOING=<doing-list-id>
LIST_ID_STUCK=<stuck-list-id>
LIST_ID_DONE=<done-list-id>
LIST_ID_DROPPED=<dropped-list-id>
```

### Multi-board mode

Add numbered board blocks. Any numbers work; they do not need to be sequential.

```env
BOARD2_BOARD_ID=<second-board-id>
BOARD2_LIST_ID_DOING=<doing-list-id>
BOARD2_LIST_ID_STUCK=<stuck-list-id>
BOARD2_LIST_ID_DONE=<done-list-id>
BOARD2_LIST_ID_DROPPED=<dropped-list-id>

BOARD5_BOARD_ID=<fifth-board-id>
BOARD5_LIST_ID_DOING=<doing-list-id>
BOARD5_LIST_ID_STUCK=<stuck-list-id>
BOARD5_LIST_ID_DONE=<done-list-id>
BOARD5_LIST_ID_DROPPED=<dropped-list-id>
```

Rules:
- Keep one `TRELLO_CALLBACK_URL`; register each board’s webhook to the same `https://<host>/webhook`.
- Identities (`AGENT_TRELLO_MEMBER_ID`, `MANAGER_TRELLO_MEMBER_ID`, etc.) are shared across boards.
- State and worker logs are scoped per board automatically.

## 🔧 Configuration

| Variable | Purpose |
|----------|---------|
| `TRELLO_API_KEY` | Your Trello API key |
| `TRELLO_TOKEN` | Your Trello user token |
| `TRELLO_WEBHOOK_SECRET` | Trello application secret for HMAC-SHA1 |
| `TRELLO_CALLBACK_URL` | Public callback URL, exact match incl. `/webhook` |
| `TRELLO_BOARD_ID` | Target Trello board ID (legacy single-board mode) |
| `BOARD<n>_BOARD_ID` | Additional board IDs (`BOARD2_`, `BOARD3_`, …) |
| `BOARD<n>_LIST_ID_DOING/STUCK/DONE/DROPPED` | Per-board lifecycle list IDs |
| `AGENT_TRELLO_MEMBER_ID` | Member ID of the agent to trigger on |
| `AGENT_TRELLO_USERNAME` | Agent username for mentions |
| `MANAGER_TRELLO_MEMBER_ID` | Manager member ID |
| `MANAGER_TRELLO_USERNAME` | Manager username for mentions |
| `LIST_ID_DOING/STUCK/DONE/DROPPED` | Lifecycle list IDs (legacy single-board mode) |
| `BIND_HOST` | Bind address, defaults to `0.0.0.0` |
| `BIND_PORT` | Bind port, defaults to `8787` |

All identities are configurable. No hardcoded usernames, member IDs, board IDs, list IDs, or secrets in source.

---

## 🏗️ Architecture

```text
Trello Board A ──┐
                │
Trello Board B ─┤── webhook (HMAC-SHA1) ──► /webhook
                │                              │
Trello Board N ─┘                              ▼
                                    verify, filter, dedup
                                    route to matching board config
                                             │
                                             ▼
                                   Hermes worker (detached)
                                             │
                                             ▼
                                   Trello card + lists
```

### Trigger rules

- `addMemberToCard` for the configured agent member → **assigned**
- `commentCard` containing `@<agent-username>` → **mentioned**
- Everything else → ignored

### Per-task model override

Add a label to any Trello card with the pattern `model:<provider>:<full model id>` to override the default model for that task:

- `model:openrouter:anthropic/claude-3.5-sonnet` — uses OpenRouter with model ID `anthropic/claude-3.5-sonnet`
- `model:openrouter:tencent/hy3:free` — uses OpenRouter with model ID `tencent/hy3:free`
- `model:openai:gpt-4` — uses OpenAI provider with model ID `gpt-4`  
- `model::gpt-4` — uses the default provider with model ID `gpt-4`

The label format is simpler than CLI syntax: `model:<provider>:<model>`. Empty provider means “use the configured default provider”.

**Example:** Add label `model:openrouter:anthropic/claude-3.5-sonnet` to a card, then assign it to the agent. The worker will spawn with:
```bash
hermes chat -q "..." --provider openrouter --model anthropic/claude-3.5-sonnet
```

If no model label is present, the worker uses the default model from `HERMES_MODEL` in `config.env`.

### Lifecycle

|| State | Action |
||-------|--------|
|| `Todo` → pickup | Bridge moves card to **Doing** |
|| Completed | Worker posts completion comment → moves card to **Done** → unassigns itself |
|| Blocked | Worker posts comment stating what is blocked and what is needed from @manager → moves card to **Stuck** → assigns to manager |
|| Cancelled | Manager explicitly cancels or task is clearly out of scope → worker posts brief comment → moves card to **Dropped** → unassigns itself |

### Comment Tags

Bridge comments are plain text and do not use Trello markdown tags.

**Examples:**
- `FYI Picked up by @agent. I'll work this and report back here.`
- `FYA The worker run became stale and needs manual review. @manager can you take a look when you have a moment?`
- `FYA Worker timed out after 120s. @manager please review manually.`

---

## 🧪 Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile trello_bot.py
```

---

## 🔐 Security

- `config.env` is git-ignored and must remain `chmod 600`
- All write-back is done through the local CLI; secrets are never passed to the worker prompt
- Signature verification uses raw body + exact callback URL
- Acknowledge first, process asynchronously — keeps Trello happy

---

## 📜 License

MIT — do anything, but don’t blame us if the cards move themselves. 🤷‍♂️

---

<p align="center">
  Built with 🐍 + 🤖 · Pairs nicely with <a href="https://trello.com">Trello</a> + <a href="https://github.com/aishee-mitra/trello-hermes-bridge">this repo</a>
</p>
