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
- 🎯 **Explicit triggers only** — assignment or `@ai`/`@trello-username` mention only
- 🧠 **Async worker spawning** — detached Hermes worker gets full card context
- 📋 **Lifecycle management** — `Todo` → `Doing` → `Stuck` → `Done` → `Dropped`
- 🔁 **Deduplication** — same card/trigger ignored within a configurable window
- 🌐 **Cloudflare-tunnel friendly** — binds to configurable `BIND_HOST:<port>`
- 🏷️ **Per-task model override** — use labels like `model:provider/model-name` to override the default model for a specific card
- 📦 **Zero external dependencies** — stdlib only
- 🚀 **systemd user service** — auto-start + linger

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

Expose the bridge with a tunnel on `BIND_HOST:BIND_PORT`, register a Trello board webhook to `https://<host>/webhook`, and you’re live.

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
- Expose via tunnel and register the Trello webhook

---

## 🔧 Configuration

| Variable | Purpose |
|----------|---------|
| `TRELLO_API_KEY` | Your Trello API key |
| `TRELLO_TOKEN` | Your Trello user token |
| `TRELLO_WEBHOOK_SECRET` | Trello application secret for HMAC-SHA1 |
| `TRELLO_CALLBACK_URL` | Public callback URL, exact match incl. `/webhook` |
| `TRELLO_BOARD_ID` | Target Trello board ID |
| `AGENT_TRELLO_MEMBER_ID` | Member ID of the agent to trigger on |
| `AGENT_TRELLO_USERNAME` | Agent username for mentions |
| `MANAGER_TRELLO_MEMBER_ID` | Manager member ID |
| `MANAGER_TRELLO_USERNAME` | Manager username for mentions |
| `LIST_ID_DOING/STUCK/DONE/DROPPED` | Lifecycle list IDs |
| `BIND_HOST` | Bind address, defaults to `0.0.0.0` |
| `BIND_PORT` | Bind port, defaults to `8787` |

All identities are configurable. No hardcoded usernames, member IDs, board IDs, list IDs, or secrets in source.

---

## 🏗️ Architecture

```text
Trello Board
    │
    │  webhook (HMAC-SHA1)
    ▼
trello_bot.py ──► /webhook
    │
    │  verify, filter, dedup, enrich card
    ▼
Hermes worker (detached)
    │
    │  uses local CLI for write-back
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

| State | Action |
|-------|--------|
| `Todo` → pickup | Bridge moves card to **Doing** |
| Completed | Worker moves card to **Done** |
| Blocked | Worker comments + mentions manager + moves to **Stuck** |
| Cancelled | Worker comments briefly + moves to **Dropped** |

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
