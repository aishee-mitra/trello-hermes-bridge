# Trello → Hermes Bridge Install

Use this skill when installing or verifying the trello-bot full stack for a new host or fresh checkout.

## Preflight

- Python 3.11+
- Hermes Agent installed and on PATH (`hermes chat --help` should work)
- User-level systemd enabled: `systemctl --user status` succeeds
- Linger enabled for persistence: `loginctl enable-linger`

## Install Steps

1. Clone and enter repo: `git clone https://github.com/aishee-mitra/trello-hermes-bridge.git && cd trello-hermes-bridge`. Do not use `--add-readme`.

2. Copy config: `cp config.env.example config.env && chmod 600 config.env`.

3. Fill required values in `config.env`:
   - Trello: `TRELLO_API_KEY`, `TRELLO_TOKEN`, `TRELLO_WEBHOOK_SECRET`
   - Callback: `TRELLO_CALLBACK_URL=https://<host>/webhook`
   - Board: `TRELLO_BOARD_ID`
   - Identities: `AGENT_TRELLO_MEMBER_ID`, `AGENT_TRELLO_USERNAME`, `MANAGER_TRELLO_MEMBER_ID`, `MANAGER_TRELLO_USERNAME`
   - Lists: `LIST_ID_DOING`, `LIST_ID_STUCK`, `LIST_ID_DONE`, `LIST_ID_DROPPED`
   - Runtime: `HERMES_BIN`, `HERMES_MODEL` (optional), `PROJECT_DIR`, `DEDUP_WINDOW_SECONDS`, `MAX_CARD_COMMENTS`

4. Install systemd user service:
   - `mkdir -p ~/.config/systemd/user`
   - `cp trello-bot.service.example ~/.config/systemd/user/trello-bot.service`
   - `systemctl --user daemon-reload`
   - `systemctl --user enable --now trello-bot`
   - `systemctl --user status trello-bot`

5. Verify listening address/port from logs:
   - `journalctl --user -u trello-bot --no-pager -n 20`

6. Tunnel + webhook:
   - Expose `BIND_HOST:BIND_PORT` via Cloudflare Tunnel or equivalent
   - Register Trello board webhook to `https://<public-host>/webhook`
   - Use `TRELLO_WEBHOOK_SECRET` for HMAC-SHA1 verification

7. Optional per-model labels: use Trello label `model:<provider>:<model-id>`, e.g. `model:openrouter:tencent/hy3:free`.

## Verification

- Run unit tests: `python3 -m unittest discover -s tests -v`
- Compile check: `python3 -m py_compile trello_bot.py`
- Create a Trello test card, assign/mention the agent, watch logs with `journalctl --user -u trello-bot -f`
