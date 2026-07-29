# Trello → Hermes Bridge

A small, dependency-free webhook bridge that turns explicit Trello work
allocation into a detached Hermes Agent run.

## Workflow

1. Add the configured agent member to a Trello card, or mention the configured
   agent username in a card comment.
2. Trello sends a signed board webhook to `/webhook`.
3. The bridge verifies the signature, filters and deduplicates the trigger,
   fetches the card context, moves it to **In Progress**, and posts one receipt.
4. A detached Hermes worker performs the task and uses the local CLI for
   comments and list transitions.
5. The worker moves completed cards to **Done**, or blocked cards to **Blocked**
   and mentions the configured manager username.

The bridge deliberately does not trigger on ordinary card moves, card creation,
or arbitrary comments. Assignment and explicit mention are the only first-class
triggers.

## Configuration

```bash
cp config.env.example config.env
chmod 600 config.env
$EDITOR config.env
```

All identities are configurable. `AGENT_TRELLO_MEMBER_ID`,
`AGENT_TRELLO_USERNAME`, `MANAGER_TRELLO_MEMBER_ID`, and
`MANAGER_TRELLO_USERNAME` are required. No personal usernames, member IDs,
board IDs, list IDs, or secrets are embedded in the source.

The Trello API key + token must belong to an account that can read and edit the
board. The webhook secret is the Trello application secret associated with the
API key. `TRELLO_CALLBACK_URL` must exactly match the public URL registered with
Trello, including `/webhook`.

## Run locally

```bash
python3 trello_bot.py serve --config ./config.env
curl http://192.168.0.99:8787/health
```

The webhook endpoint also answers `GET` and `HEAD` with 200, which lets Trello
validate the callback URL during webhook creation.

## Register the webhook

Create a board webhook through Trello's REST API using the API key and token:

```bash
curl -X POST \
  "https://api.trello.com/1/tokens/$TRELLO_TOKEN/webhooks/?key=$TRELLO_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "description": "Trello Hermes bridge",
    "callbackURL": "https://YOUR_PUBLIC_HOST/webhook",
    "idModel": "YOUR_BOARD_ID"
  }'
```

Use a Cloudflare tunnel or another HTTPS reverse proxy to expose the local
service. Keep the local service bound to the LAN address reachable by the
chosen tunnel. Trello sends all board actions, so filtering remains in this
service.

## Worker write-back CLI

These commands load credentials from `config.env`; secrets do not need to be
placed in the Hermes prompt:

```bash
python3 trello_bot.py comment CARD_ID 'Concise progress update'
python3 trello_bot.py move CARD_ID LIST_ID
```

## systemd user service

```bash
mkdir -p ~/.config/systemd/user
cp trello-bot.service.example ~/.config/systemd/user/trello-bot.service
systemctl --user daemon-reload
systemctl --user enable --now trello-bot
systemctl --user status trello-bot
```

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile trello_bot.py
```

## Security notes

- Never commit `config.env`, logs, tokens, or generated state.
- Verify Trello's `X-Trello-Webhook` signature over the raw request body plus
  the exact registered callback URL.
- Use a dedicated Trello account/token when possible.
- The bridge acknowledges a valid, signed webhook immediately and processes it
  asynchronously, so Trello does not wait for API calls or Hermes startup.
