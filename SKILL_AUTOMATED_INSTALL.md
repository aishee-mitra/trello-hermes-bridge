# 🤖 Trello-Hermes Bridge: Agent Installation Guide

**Target Audience:** Hermes agents tasked with installing the Trello-Hermes bridge.

This guide walks you through a complete, automated installation with validation at each step. **Ask the user for required information as needed.** Never hardcode or guess credentials.

---

## Overview

The Trello-Hermes bridge is a local HTTP service that:
1. Listens for Trello board webhooks
2. Filters for explicit triggers (card assignment or @mention)
3. Spawns a detached Hermes worker to complete the task
4. Updates the card automatically with results

**Requirements:**
- Python 3.11+
- Hermes Agent (installed and configured)
- Trello board with API access
- Network tunnel (Cloudflare, Tailscale, etc.) for webhook callback
- `systemd` user service support (Linux only)

---

## Phase 1: Pre-Installation Checks

### 1.1 Verify Python Version

```bash
python3 --version
```

✅ **Required:** Python 3.11 or later

### 1.2 Verify Hermes is Installed

```bash
which hermes
hermes --version
```

✅ **Required:** Hermes agent must be in PATH

### 1.3 Verify systemd User Service Support

```bash
systemctl --user status
```

✅ **Required:** Should return active (or show recent service status). If this fails, you're likely on a non-systemd system (macOS, etc.). Contact the user for alternative deployment strategy.

### 1.4 Check Network Connectivity

Ask the user:
- **Do you have a public tunnel set up?** (Cloudflare Tunnel, Tailscale Funnel, ngrok, etc.)
- **What is the public callback URL you can expose?** (e.g., `https://myname.example.com/webhook`)

You'll need this URL later for Trello webhook registration.

---

## Phase 2: Gather Trello Credentials

Ask the user to provide:

### 2.1 Trello API Key
> "Go to https://trello.com/app-key → copy your API Key"

**Validation:**
```bash
# Test with curl
curl -X GET "https://api.trello.com/1/members/me?key=$API_KEY&token=$TOKEN" | head -20
```

### 2.2 Trello Personal Token
> "On the same page (https://trello.com/app-key), click 'Token' → copy your Personal Access Token"

**Validation:** Use the curl command above with both KEY and TOKEN.

### 2.3 Trello Application Secret
> "Still on https://trello.com/app-key → find 'App Name' at the top. You'll need to create or retrieve your application secret for HMAC-SHA1 verification. If you don't have one, use your API Key as a fallback (Trello allows this)."

### 2.4 Target Board ID
> "Open your Trello board → copy the board ID from the URL: https://trello.com/b/{BOARD_ID}/..."

**Validation:**
```bash
curl -X GET "https://api.trello.com/1/boards/$BOARD_ID?key=$API_KEY&token=$TOKEN" | head -20
```

### 2.5 Agent Member ID & Username
> "On the Trello board, find the member you want to use as the AI agent. Open their member card and copy the member ID from the URL: https://trello.com/c/{CARD_ID}/... or use the Trello API:"

```bash
curl -X GET "https://api.trello.com/1/members/me?key=$API_KEY&token=$TOKEN" | jq .id,.username
```

### 2.6 Manager Member ID & Username
> "Find your own member ID on the Trello board. This is who will be @mentioned when tasks are blocked or complete."

```bash
curl -X GET "https://api.trello.com/1/members/me?key=$API_KEY&token=$TOKEN" | jq .id,.username
```

### 2.7 List IDs (Doing, Stuck, Done, Dropped)
> "Create or identify 4 lists on your Trello board: 'Doing', 'Stuck', 'Done', 'Dropped'. Get their IDs:"

```bash
curl -X GET "https://api.trello.com/1/boards/$BOARD_ID/lists?key=$API_KEY&token=$TOKEN" | jq '.[] | {name, id}'
```

### 2.8 Public Callback URL (Webhook)
Ask again for confirmation:
> "What is your public callback URL? (e.g., https://myname.example.com) We'll append `/webhook` to this for Trello to POST to."

Store as: `https://myname.example.com/webhook` (note the `/webhook` suffix)

---

## Phase 3: Clone & Configure

### 3.1 Clone the Repository

```bash
cd /home/aishee/code  # or your preferred workspace
git clone https://github.com/aishee-mitra/trello-hermes-bridge.git
cd trello-hermes-bridge
```

### 3.2 Copy and Edit config.env

```bash
cp config.env.example config.env
chmod 600 config.env
$EDITOR config.env
```

**Fill in the template** with values from Phase 2:

```env
# Trello API credentials
TRELLO_API_KEY=<from 2.1>
TRELLO_TOKEN=<from 2.2>
TRELLO_WEBHOOK_SECRET=<from 2.3>
TRELLO_CALLBACK_URL=<from 2.8, e.g., https://myname.example.com/webhook>

# Board and lifecycle
TRELLO_BOARD_ID=<from 2.4>
LIST_ID_DOING=<from 2.7>
LIST_ID_STUCK=<from 2.7>
LIST_ID_DONE=<from 2.7>
LIST_ID_DROPPED=<from 2.7>

# Members
AGENT_TRELLO_MEMBER_ID=<from 2.5>
AGENT_TRELLO_USERNAME=<from 2.5>
MANAGER_TRELLO_MEMBER_ID=<from 2.6>
MANAGER_TRELLO_USERNAME=<from 2.6>

# Network
BIND_HOST=0.0.0.0
BIND_PORT=8787

# Hermes integration
HERMES_BIN=/usr/local/bin/hermes  # or wherever `which hermes` points to
HERMES_MODEL=openrouter:tencent/hy3:free  # or your preferred model
```

### 3.3 Validate config.env

```bash
python3 -c "
import os
from pathlib import Path

config_vars = [
    'TRELLO_API_KEY', 'TRELLO_TOKEN', 'TRELLO_WEBHOOK_SECRET',
    'TRELLO_CALLBACK_URL', 'TRELLO_BOARD_ID',
    'LIST_ID_DOING', 'LIST_ID_STUCK', 'LIST_ID_DONE', 'LIST_ID_DROPPED',
    'AGENT_TRELLO_MEMBER_ID', 'AGENT_TRELLO_USERNAME',
    'MANAGER_TRELLO_MEMBER_ID', 'MANAGER_TRELLO_USERNAME'
]

missing = [var for var in config_vars if not os.environ.get(var)]
if missing:
    print(f'❌ Missing required variables: {missing}')
    exit(1)
else:
    print('✅ All required variables are set.')
" < config.env
```

---

## Phase 4: Install systemd User Service

### 4.1 Create Service Directory

```bash
mkdir -p ~/.config/systemd/user
```

### 4.2 Copy Service Template

```bash
cp trello-bot.service.example ~/.config/systemd/user/trello-bot.service
```

### 4.3 Edit Service File (Optional)

If the working directory or Python path needs adjustment:

```bash
$EDITOR ~/.config/systemd/user/trello-bot.service
```

Typical content:
```ini
[Unit]
Description=Trello → Hermes Bridge
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/aishee/code/trello-hermes-bridge
ExecStart=/usr/bin/python3 trello_bot.py serve
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment="PATH=/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=default.target
```

### 4.4 Enable & Start Service

```bash
systemctl --user daemon-reload
systemctl --user enable trello-bot
systemctl --user start trello-bot
```

### 4.5 Verify Service is Running

```bash
systemctl --user status trello-bot
```

Should show:
```
● trello-bot.service - Trello → Hermes Bridge
     Loaded: loaded (...)
     Active: active (running) ...
```

### 4.6 Check for Startup Errors

```bash
journalctl --user -u trello-bot -n 50 --no-pager
```

Should show:
```
trello_bot: listening on 0.0.0.0:8787
```

---

## Phase 5: Expose the Bridge (Tunnel Setup)

The bridge runs locally on `BIND_HOST:BIND_PORT` (default `0.0.0.0:8787`). To receive Trello webhooks, it must be publicly accessible.

### 5.1 Set Up a Public Tunnel

Ask the user which tunnel they prefer:

**Option A: Cloudflare Tunnel**
```bash
cloudflared tunnel create trello-hermes
cloudflared tunnel route dns trello-hermes myname.example.com
cloudflared tunnel run trello-hermes --url http://127.0.0.1:8787
```

**Option B: Tailscale Funnel**
```bash
tailscale funnel on
tailscale funnel status
# Output shows: https://<your-machine-name>.ts.net/
# Use as callback URL
```

**Option C: Other (ngrok, etc.)**
```bash
ngrok http 8787
# Copy the public URL (e.g., https://abc123.ngrok.io)
```

### 5.2 Verify Bridge is Reachable

```bash
curl -X GET http://127.0.0.1:8787/health
# Should return 200 OK
```

Then test from the public URL:
```bash
curl -X GET https://myname.example.com/health
# Should return 200 OK
```

---

## Phase 6: Register Trello Webhook

### 6.1 Create the Webhook via Trello API

```bash
curl -X POST "https://api.trello.com/1/tokens/$TRELLO_TOKEN/webhooks?key=$API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"callbackURL\": \"https://myname.example.com/webhook\",
    \"idModel\": \"$TRELLO_BOARD_ID\",
    \"description\": \"Hermes Bridge Webhook\"
  }"
```

Expected response:
```json
{
  "id": "webhook_id",
  "idModel": "board_id",
  "callbackURL": "https://myname.example.com/webhook",
  "active": true
}
```

### 6.2 Verify Webhook is Active

```bash
curl -X GET "https://api.trello.com/1/tokens/$TRELLO_TOKEN/webhooks?key=$API_KEY" | jq .
```

Should list your webhook with `"active": true`.

---

## Phase 7: Test the Bridge

### 7.1 Create a Test Card

On your Trello board:
1. Create a card titled "Test: Agent Init"
2. Move it to the "Doing" list (or leave in "Todo")
3. Assign it to the agent member

### 7.2 Check Logs

```bash
journalctl --user -u trello-bot -f
```

You should see:
```
worker exited for card abc12345; status=done
```

### 7.3 Verify Card was Updated

1. Refresh the Trello card
2. Should have a pickup comment from the agent
3. Card should be in the "Done" list (or still in Doing if work is pending)
4. Check `/workers/{card_id[:8]}.log` for full worker output:
   ```bash
   cat workers/abc12345.log
   ```

### 7.4 Test Manual Trigger

Send a test webhook manually:

```bash
# Generate a test signature
python3 << 'EOF'
import hmac
import hashlib
import base64
import json

api_key = "YOUR_API_KEY"
webhook_secret = "YOUR_WEBHOOK_SECRET"
callback_url = "https://myname.example.com/webhook"
card_id = "abc123"

# Minimal webhook payload
payload = {
  "action": {
    "type": "addMemberToCard",
    "idMemberCreator": "agent_member_id",
    "data": {
      "card": {"id": card_id, "name": "Test Card"},
      "idMember": "agent_member_id",
      "board": {"id": "board_id"}
    }
  },
  "model": {"id": "board_id", "name": "Board Name"}
}

body = json.dumps(payload).encode()
sig = base64.b64encode(hmac.new(
    webhook_secret.encode(),
    body + callback_url.encode(),
    hashlib.sha1
).digest()).decode()

print(f"Signature: {sig}")
print(f"Body: {body.decode()}")
EOF
```

Then POST:
```bash
curl -X POST http://127.0.0.1:8787/webhook \
  -H "Content-Type: application/json" \
  -H "X-Trello-Webhook-Signature: $SIG" \
  -d '{...payload...}'
```

---

## Phase 8: Troubleshooting

### Problem: Service fails to start

**Check:**
```bash
journalctl --user -u trello-bot -n 100 --no-pager
```

**Common issues:**
- `config.env` not found → Check file exists and is readable
- Python import error → Verify `python3 --version` and stdlib availability
- `hermes` not in PATH → Check `which hermes` and update HERMES_BIN in config.env

### Problem: Webhook not received

**Check:**
1. Bridge is listening: `systemctl --user status trello-bot` (active)
2. Public URL is reachable: `curl https://myname.example.com/health`
3. Webhook is registered: `curl -X GET "https://api.trello.com/1/tokens/$TOKEN/webhooks?key=$KEY"`
4. Check logs for signature errors: `journalctl --user -u trello-bot -f`

### Problem: Card not updated after assignment

**Check:**
1. Worker log file: `cat workers/{card_id[:8]}.log`
2. Service logs: `journalctl --user -u trello-bot -n 50 --no-pager`
3. Trello API access: `curl -X GET "https://api.trello.com/1/cards/$CARD_ID?key=$KEY&token=$TOKEN"`

---

## Phase 9: Post-Installation Verification

Run this checklist:

- [ ] Service is active: `systemctl --user status trello-bot` → "active (running)"
- [ ] Bridge is reachable: `curl http://127.0.0.1:8787/health` → 200 OK
- [ ] Public URL works: `curl https://myname.example.com/health` → 200 OK
- [ ] Webhook registered: `curl -X GET "https://api.trello.com/1/tokens/$TOKEN/webhooks?key=$KEY"` → lists your webhook with `"active": true`
- [ ] Test card created and assigned → Worker logs show execution
- [ ] Worker output appears in logs: `journalctl --user -u trello-bot -n 50` shows worker activity
- [ ] Card was updated (comment, list change) after assignment

---

## Phase 10: Next Steps

1. **Read the Full Documentation**  
   Check `README.md` for lifecycle management, model override patterns, and security best practices.

2. **Understand the Worker Lifecycle**  
   Workers follow a strict state machine: `Todo` → `Doing` → `Stuck`/`Done`/`Dropped`. Review the SKILL.md in the repo for prompt patterns.

3. **Monitor in Production**  
   ```bash
   journalctl --user -u trello-bot -f  # Live tail of logs
   ```

4. **Tune Configuration**  
   Adjust `WORKER_TIMEOUT_SECONDS`, `MAX_RETRIES`, etc. as needed. All knobs are in `config.env`.

---

## Need Help?

If installation fails at any stage:

1. **Capture error logs:**
   ```bash
   journalctl --user -u trello-bot -n 200 --no-pager > /tmp/trello-bot-logs.txt
   cat /tmp/trello-bot-logs.txt
   ```

2. **Test Trello API access:**
   ```bash
   curl -X GET "https://api.trello.com/1/members/me?key=$KEY&token=$TOKEN" | jq .
   ```

3. **Check worker logs:**
   ```bash
   ls -lah workers/
   tail -50 workers/*.log
   ```

4. **Verify Python + systemd:**
   ```bash
   python3 --version
   systemctl --user status
   which python3
   ```

Share these outputs when reporting issues.

---

**Installation Complete!** 🎉

The Trello-Hermes bridge is now running and ready to process cards. Assign a card to the agent on your board and watch it execute in real-time.
