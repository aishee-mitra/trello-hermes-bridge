# 🤖 Trello-Hermes Bridge: Agent Installation Guide

**Target Audience:** Hermes agents tasked with installing the Trello-Hermes bridge on any system.

This guide walks you through a complete, automated installation with validation at each step. **Ask the user for required information as needed.** Never hardcode or guess credentials.

---

## Overview

The Trello-Hermes bridge is a local HTTP service that:
1. Listens for Trello board webhooks
2. Filters for explicit triggers (card assignment or @mention)
3. Spawns a detached Hermes worker to complete the task
4. Updates the card automatically with results

**Requirements:**
- **Hermes Agent** (installed and configured — checked FIRST)
- **Python 3.11+** (stdlib only, no external dependencies)
- **Linux with systemd** user service support
- **Network tunnel** (Cloudflare, Tailscale, ngrok, etc.) for webhook callback
- **Trello board** with API access

---

## ⚠️ Critical Prerequisite: Verify Hermes is Installed

**Before doing ANYTHING else, verify Hermes is available on this system.**

```bash
which hermes
hermes --version
```

✅ **Required:** Both commands must succeed. If either fails, Hermes is not installed or not in PATH.

**If Hermes is not installed:**
> Please install Hermes Agent first. See: https://hermes-agent.nousresearch.com/docs/installation

**If Hermes is installed but not in PATH:**
```bash
# Find where Hermes is installed
find ~ -name "hermes" -type f -executable 2>/dev/null

# Then use the full path when specifying HERMES_BIN later (Phase 3)
```

---

## Phase 1: Pre-Installation Environment Checks

### 1.1 Verify Python Version

```bash
python3 --version
```

✅ **Required:** Python 3.11 or later

**If Python is too old:**
```bash
python3.11 --version  # or python3.12, python3.13, etc.
```

If you have a newer version available, you can use it in Phase 3 when configuring.

### 1.2 Verify systemd User Service Support

```bash
systemctl --user status
```

✅ **Required:** Should return a status line (may show "active", "inactive", or a recent state). If this fails with "Connection refused" or "System not found", you're likely on a non-systemd system (macOS, some BSD, older Linux distributions). 

**If systemd is not available:**
> This guide only covers systemd-based installations (Linux with systemd user services). For alternative deployments (Docker, systemd --system, manual process management), see the README's advanced sections or contact your deployment team.

### 1.3 Verify Network Access for Public Callback

Ask the user:
- **Do you have a way to expose a service publicly?** (Cloudflare Tunnel, Tailscale, ngrok, SSH reverse tunnel, etc.)
- **Can you get a public URL that routes HTTPS POST requests to a local port?**

✅ **Required:** You must be able to expose `http://127.0.0.1:8787` (or your configured port) on the public internet for Trello to send webhooks to.

---

## Phase 2: Gather Trello Credentials & Identifiers

Ask the user to provide the following. **Never hardcode or guess these values.**

### 2.1 Trello API Key

> "Go to https://trello.com/app-key → copy your **API Key** (first box on the page)"

**Validation:**
```bash
API_KEY="<paste-here>"
TOKEN="<paste-from-next-step>"

# Test the key (you'll validate the token in the next step)
curl -s -X GET "https://api.trello.com/1/members/me?key=$API_KEY&token=$TOKEN" | head -c 100
```

### 2.2 Trello Personal Access Token

> "On the same page (https://trello.com/app-key), find the **Token** section → click 'Token' link → copy your Personal Access Token"

### 2.3 Trello Webhook Secret

> "For webhook signature verification, use your **API Key** as the secret. Trello standard practice: the API Key serves both as public identifier and shared secret for HMAC-SHA1 signatures."

Save this: `WEBHOOK_SECRET = <same as API_KEY>`

**Validation:**
```bash
curl -s -X GET "https://api.trello.com/1/members/me?key=$API_KEY&token=$TOKEN" | jq '.id, .username'
# Should show: "user_id" and "username"
```

### 2.4 Target Trello Board ID

> "Open your Trello board in a browser → copy the board ID from the URL: `https://trello.com/b/{BOARD_ID}/...` (copy that 8+ character hex string)"

**Validation:**
```bash
BOARD_ID="<paste-here>"
curl -s -X GET "https://api.trello.com/1/boards/$BOARD_ID?key=$API_KEY&token=$TOKEN" | jq '.name'
# Should show the board name
```

### 2.5 Agent Account: Member ID & Username

This is the Trello member account the bridge will use to interact with cards (pick them up, move them, post comments).

**Option A: If the agent has its own Trello account**

```bash
curl -s -X GET "https://api.trello.com/1/members/me?key=$API_KEY&token=$TOKEN" | jq '.id, .username'
```

Use these values as `AGENT_MEMBER_ID` and `AGENT_USERNAME`.

**Option B: If using a service account or bot member**

```bash
curl -s -X GET "https://api.trello.com/1/boards/$BOARD_ID/members?key=$API_KEY&token=$TOKEN" | jq '.[] | {id, fullName, username}' | head -20
```

Find the agent/bot account and copy its ID (a 24-character hex string like `507f1f77bcf86cd799439011`) and username.

### 2.6 Manager Account: Member ID & Username

This is the account of the person who will be @mentioned when tasks are blocked or completed. Usually your own account.

```bash
curl -s -X GET "https://api.trello.com/1/members/me?key=$API_KEY&token=$TOKEN" | jq '.id, .username'
```

Use these values as `MANAGER_MEMBER_ID` and `MANAGER_USERNAME`.

### 2.7 Lifecycle List IDs (Doing, Stuck, Done, Dropped)

The bridge moves cards through a workflow. Identify or create 4 lists on your Trello board:
- **Doing:** Cards currently being worked on
- **Stuck:** Cards blocked waiting for manager input
- **Done:** Completed cards
- **Dropped:** Cancelled/out-of-scope cards

Get their IDs:

```bash
curl -s -X GET "https://api.trello.com/1/boards/$BOARD_ID/lists?key=$API_KEY&token=$TOKEN" | jq '.[] | {name, id}'
```

Copy the ID for each list. Note them as:
- `LIST_ID_DOING=<id>`
- `LIST_ID_STUCK=<id>`
- `LIST_ID_DONE=<id>`
- `LIST_ID_DROPPED=<id>`

### 2.8 Public Callback URL (for Webhooks)

This is the public HTTPS URL where Trello will POST webhook events.

Ask the user:
> "What is your public callback URL? (e.g., `https://myname.example.com`, `https://my-server.ts.net`, or `https://abc123.ngrok.io`)"

**Important:** Trello requires HTTPS. The bridge will handle the `/webhook` path suffix automatically.

Store as: `CALLBACK_URL=https://myname.example.com/webhook` (note the `/webhook` suffix)

---

## Phase 3: Clone Repository & Configure

### 3.1 Clone the Repository

Choose a workspace directory. This is where the bridge code and config will live.

```bash
cd ~/workspace  # or wherever you keep projects
git clone https://github.com/aishee-mitra/trello-hermes-bridge.git
cd trello-hermes-bridge
REPO_DIR=$(pwd)  # Save this for later
echo "Bridge cloned to: $REPO_DIR"
```

### 3.2 Create & Configure config.env

```bash
cp config.env.example config.env
chmod 600 config.env
```

Edit the file:
```bash
$EDITOR config.env
```

**Fill in with values from Phase 2:**

```env
# Trello API credentials
TRELLO_API_KEY=<from 2.1>
TRELLO_TOKEN=<from 2.2>
TRELLO_WEBHOOK_SECRET=<from 2.3, same as API_KEY>
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
HERMES_BIN=$(which hermes)  # Auto-detect, or set manually to /path/to/hermes
HERMES_MODEL=openrouter:tencent/hy3:free  # or your preferred model
```

**Important config.env Format Rules:**
- ✅ Each line: `KEY=VALUE` (no spaces around `=`)
- ❌ No comments (no `#` characters)
- ❌ No quotes around values
- ❌ No trailing spaces
- 🔒 Keep chmod 600 (readable only by owner)

### 3.3 Validate config.env

Source the configuration and verify all required variables are set:

```bash
set -a
source config.env
set +a

python3 << 'EOF'
import os
import sys

required_vars = [
    'TRELLO_API_KEY', 'TRELLO_TOKEN', 'TRELLO_WEBHOOK_SECRET',
    'TRELLO_CALLBACK_URL', 'TRELLO_BOARD_ID',
    'LIST_ID_DOING', 'LIST_ID_STUCK', 'LIST_ID_DONE', 'LIST_ID_DROPPED',
    'AGENT_TRELLO_MEMBER_ID', 'AGENT_TRELLO_USERNAME',
    'MANAGER_TRELLO_MEMBER_ID', 'MANAGER_TRELLO_USERNAME',
    'BIND_HOST', 'BIND_PORT', 'HERMES_BIN', 'HERMES_MODEL'
]

missing = [var for var in required_vars if not os.environ.get(var)]

if missing:
    print(f'❌ Missing required variables: {missing}')
    sys.exit(1)

# Additional checks
hermes_bin = os.environ.get('HERMES_BIN')
import subprocess
try:
    subprocess.run([hermes_bin, '--version'], capture_output=True, check=True, timeout=5)
except Exception as e:
    print(f"❌ HERMES_BIN '{hermes_bin}' is not executable: {e}")
    sys.exit(1)

print('✅ All required variables are set and validated.')
EOF
```

If this fails, review config.env for typos or missing values.

---

## Phase 4: Install systemd User Service

### 4.1 Create systemd User Service Directory

```bash
mkdir -p ~/.config/systemd/user
```

### 4.2 Copy and Customize Service Template

```bash
cp trello-bot.service.example ~/.config/systemd/user/trello-bot.service
```

### 4.3 **MANDATORY: Update WorkingDirectory**

The service file contains a hardcoded working directory. You MUST update it to match where you cloned the repository.

```bash
REPO_DIR=$(pwd)  # Run this in the cloned repository directory
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" ~/.config/systemd/user/trello-bot.service

# Verify the change:
grep "WorkingDirectory=" ~/.config/systemd/user/trello-bot.service
```

**Example:** If you cloned to `/home/myuser/workspace/trello-hermes-bridge`, the file should show:
```ini
WorkingDirectory=/home/myuser/workspace/trello-hermes-bridge
```

### 4.4 **MANDATORY: Update Python/Hermes Paths (if needed)**

If `python3` or `hermes` are not in your standard PATH, update the service file:

```bash
$EDITOR ~/.config/systemd/user/trello-bot.service
```

Change:
```ini
ExecStart=/usr/bin/python3 trello_bot.py serve
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
```

To:
```ini
ExecStart=/path/to/python3 trello_bot.py serve
Environment="PATH=/custom/path:/usr/local/bin:/usr/bin:/bin"
```

### 4.5 Enable & Start Service

```bash
systemctl --user daemon-reload
systemctl --user enable trello-bot
systemctl --user start trello-bot
```

### 4.6 Verify Service is Running

```bash
systemctl --user status trello-bot
```

✅ Should show:
```
● trello-bot.service - Trello → Hermes Bridge
     Loaded: loaded (...)
     Active: active (running) ...
```

### 4.7 Check Service Logs

```bash
journalctl --user -u trello-bot -n 50 --no-pager
```

✅ Should show:
```
trello_bot: listening on 0.0.0.0:8787
```

---

## Phase 5: Expose the Bridge (Public Tunnel Setup)

The bridge runs locally on `BIND_HOST:BIND_PORT` (default `0.0.0.0:8787`). Trello must be able to POST webhooks to your public callback URL.

### 5.0 Choose Your Tunnel Provider

Pick ONE based on your setup:

| Option | Best For | Setup Time | Reliability |
|--------|----------|-----------|-------------|
| **Cloudflare Tunnel** | Production, permanent setup | 10 min | High |
| **Tailscale Funnel** | Already using Tailscale | 2 min | High |
| **ngrok** | Testing, temporary setup | 2 min | Medium |
| **SSH Reverse Tunnel** | Server with SSH access | 5 min | Medium |

### 5.1a: Cloudflare Tunnel Setup

```bash
# Install cloudflared if not present
# https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

cloudflared tunnel create trello-hermes
cloudflared tunnel route dns trello-hermes myname.example.com
cloudflared tunnel run trello-hermes --url http://127.0.0.1:8787
```

Your callback URL: `https://myname.example.com/webhook`

**Important:** Keep this command running (in a tmux/screen session or systemd service).

### 5.1b: Tailscale Funnel Setup

```bash
tailscale funnel on
tailscale funnel status
```

Output shows: `https://<your-machine-name>.ts.net/`

Your callback URL: `https://<your-machine-name>.ts.net/webhook`

### 5.1c: ngrok Setup

```bash
ngrok http 8787
```

Output shows: `Forwarding https://abc123.ngrok.io -> http://localhost:8787`

Your callback URL: `https://abc123.ngrok.io/webhook`

**Important:** ngrok free tier expires after 2 hours. Keep the tunnel running or restart it periodically.

### 5.2 **CRITICAL: Update config.env with New Callback URL**

After setting up your tunnel, you MUST update `TRELLO_CALLBACK_URL` in config.env:

```bash
$EDITOR config.env
```

Update the line:
```env
TRELLO_CALLBACK_URL=https://your-new-url-from-tunnel/webhook
```

Then reload the service:
```bash
systemctl --user restart trello-bot
```

### 5.3 Verify Bridge is Publicly Reachable

**Test locally:**
```bash
curl -X GET http://127.0.0.1:8787/health
# Should return 200 OK
```

**Test publicly (from different network or machine):**
```bash
curl -X GET https://your-callback-url-without-webhook/health
# Should return 200 OK
```

---

## Phase 6: Register Trello Webhook

### 6.1 Create the Webhook

Before running the curl command, source your config:

```bash
set -a
source config.env
set +a
```

Then create the webhook:

```bash
curl -X POST "https://api.trello.com/1/tokens/$TRELLO_TOKEN/webhooks?key=$TRELLO_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"callbackURL\": \"$TRELLO_CALLBACK_URL\",
    \"idModel\": \"$TRELLO_BOARD_ID\",
    \"description\": \"Hermes Bridge Webhook\"
  }"
```

✅ Expected response:
```json
{
  "id": "webhook_id_12345",
  "idModel": "board_id",
  "callbackURL": "https://your-url/webhook",
  "active": true
}
```

### 6.2 Verify Webhook is Registered & Active

```bash
curl -s -X GET "https://api.trello.com/1/tokens/$TRELLO_TOKEN/webhooks?key=$TRELLO_API_KEY" | jq '.[] | {id, callbackURL, active}'
```

✅ Should list your webhook with `"active": true`.

---

## Phase 7: Test the Bridge

### 7.1 Create a Test Card

On your Trello board:
1. Create a new card titled "Test: Agent Initialization"
2. Leave it in the **Todo** list (don't move it yet)
3. **Assign it to the agent member** (trigger the bridge)

The bridge should:
1. Receive the webhook
2. Pick up the card
3. Move it to Doing
4. Spawn a Hermes worker
5. Complete and move to Done

### 7.2 Monitor Bridge Logs in Real-Time

```bash
journalctl --user -u trello-bot -f
```

Watch for:
```
worker spawned for card {card_id}
worker exited for card {card_id}; status=done
```

### 7.3 Check Worker Output

Worker logs are saved in the cloned repository:

```bash
cd $REPO_DIR  # Navigate to where you cloned the repo
ls -lah workers/
tail -50 workers/*.log  # View worker output
```

Each file is named `{card_id_first_8_chars}.log` and contains the worker's complete stdout/stderr.

### 7.4 Verify Card Updates on Trello

1. Refresh the card on your Trello board
2. ✅ Should have a "Picked up by @agent" comment
3. ✅ Card should be moved to Done list
4. ✅ Card should be unassigned (agent released it after completion)

---

## Phase 8: Troubleshooting

### Problem: Service fails to start

**Check:**
```bash
journalctl --user -u trello-bot -n 100 --no-pager
```

**Common issues:**
- `config.env not found` → Verify file exists: `ls -la config.env` (in the repo directory)
- `config.env: Permission denied` → Fix permissions: `chmod 600 config.env`
- `python3: command not found` → Update ExecStart in service file with full path
- `trello_bot.py: No such file or directory` → Update WorkingDirectory in service file (Phase 4.3)
- `hermes: command not found` → Update HERMES_BIN in config.env

### Problem: Service is inactive/dead after starting

```bash
systemctl --user status trello-bot
systemctl --user start trello-bot
journalctl --user -u trello-bot -n 100 --no-pager
```

Check the error messages in journalctl. Common causes:
- Config file errors (YAML parsing, missing variables)
- Port already in use: `lsof -i :8787` (and kill the process or change BIND_PORT)
- Hermes not found: Update HERMES_BIN

### Problem: Webhook not received by bridge

**Check:**
1. Bridge is listening: `systemctl --user status trello-bot` (should show "active (running)")
2. Public URL is reachable: `curl https://your-callback-url/health`
3. Webhook is registered: `curl -s -X GET "https://api.trello.com/1/tokens/$TOKEN/webhooks?key=$KEY" | jq .`
4. Bridge logs for errors: `journalctl --user -u trello-bot -f`
5. Firewall/tunnel is not blocking: Try manually POSTing a test webhook (Phase 7.4 in original guide)

### Problem: Card not updating after assignment

**Check:**
1. Worker log exists: `ls -lah workers/` (should have new `*.log` file)
2. Worker log contains errors: `tail -100 workers/{card_id_first_8}.log`
3. Service logs for worker spawn: `journalctl --user -u trello-bot | grep worker`
4. Trello API access: `curl -s -X GET "https://api.trello.com/1/cards/$CARD_ID?key=$KEY&token=$TOKEN" | jq '.name'`

---

## Phase 9: Post-Installation Verification Checklist

Run through this checklist to confirm everything is working:

- [ ] Hermes is installed: `which hermes && hermes --version`
- [ ] Service is active: `systemctl --user status trello-bot` shows "active (running)"
- [ ] Local bridge reachable: `curl http://127.0.0.1:8787/health` → 200 OK
- [ ] Public URL reachable: `curl https://your-callback-url/health` → 200 OK
- [ ] Webhook registered: `curl -s -X GET "https://api.trello.com/1/tokens/$TOKEN/webhooks?key=$KEY" | jq '.[] | select(.active==true)'`
- [ ] Test card created and assigned to agent
- [ ] Worker logs created: `ls workers/` (should have new log file)
- [ ] Card moved to Done on Trello (refresh the page)
- [ ] Card has "Picked up by @agent" comment
- [ ] Service logs show successful run: `journalctl --user -u trello-bot -n 50 | grep -E "(worker|error)"`

---

## Phase 10: If Installation Fails — Rollback

To cleanly remove the bridge and start over:

```bash
# Stop the service
systemctl --user stop trello-bot
systemctl --user disable trello-bot

# Remove service file
rm ~/.config/systemd/user/trello-bot.service

# Reload systemd
systemctl --user daemon-reload

# (Optional) Delete the cloned repository
rm -rf $REPO_DIR

# Fix the issue in your notes
# Then re-run from Phase 3 (clone) or Phase 4 (service setup)
```

---

## Phase 11: Production Monitoring

### Monitor Bridge in Real-Time

```bash
journalctl --user -u trello-bot -f
```

### Check Service Health Periodically

```bash
systemctl --user status trello-bot
```

### Review Worker Logs

```bash
cd $REPO_DIR
tail -20 workers/*.log
```

### Collect Logs for Debugging

```bash
journalctl --user -u trello-bot -n 500 --no-pager > /tmp/trello-bot-logs.txt
cat /tmp/trello-bot-logs.txt  # Share if reporting issues
```

---

## Need Help?

If installation fails at any stage:

1. **Capture error logs:**
   ```bash
   journalctl --user -u trello-bot -n 200 --no-pager > /tmp/trello-bot-logs.txt
   ```

2. **Test Trello API access:**
   ```bash
   set -a; source config.env; set +a
   curl -s -X GET "https://api.trello.com/1/members/me?key=$TRELLO_API_KEY&token=$TRELLO_TOKEN" | jq .
   ```

3. **Verify all config variables:**
   ```bash
   set -a; source config.env; set +a
   env | grep -E "^(TRELLO_|LIST_|AGENT_|MANAGER_|HERMES_)" | sort
   ```

4. **Check worker output:**
   ```bash
   ls -lah $REPO_DIR/workers/
   tail -100 $REPO_DIR/workers/*.log
   ```

5. **Check Python and Hermes versions:**
   ```bash
   python3 --version
   which hermes
   hermes --version
   ```

Share these outputs when opening an issue.

---

## Next Steps

1. **Read the Repository Documentation**  
   - `README.md` — Lifecycle management, features, architecture
   - `config.env.example` — All tunable parameters explained

2. **Understand the Worker Lifecycle**  
   - Cards move: Todo → Doing → (Stuck | Done | Dropped)
   - Worker must end with a terminal action (move card + optional comment)

3. **Configure Per-Card Model Overrides**  
   - Add label to card: `model:openrouter:anthropic/claude-3.5-sonnet`
   - Worker spawns with that model instead of default

4. **Set Up Log Rotation** (Optional)  
   - Logs grow over time; configure retention in config.env
   - Default: keep logs for 14 days

5. **Monitor in Production**  
   ```bash
   journalctl --user -u trello-bot -f  # Live tail
   ```

---

**Installation Complete!** 🎉

The Trello-Hermes bridge is now running and ready to process cards. Assign a card to the agent on your board and watch it execute in real-time.

For questions, issues, or feature requests, see the repository's README and GitHub issues.
