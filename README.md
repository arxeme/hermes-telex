# Hermes Telex Plugin

Voyager **Telex** messaging platform plugin for Hermes Agent. It lets a Hermes
agent receive and reply to Telex direct chats and channels through the Telex
OpenAPI, using a Telex **bot** API key. Feature parity with
[openclaw-telex](../openclaw-telex).

The platform id inside Hermes is `telex`.

## Layout

The repository root is the Hermes plugin directory. Hermes imports the root
`adapter.py` shim; implementation lives in the `hermes_telex/` package.

```text
hermes-telex/
  plugin.yaml
  adapter.py            # root shim -> hermes_telex.adapter
  hermes_telex/
    adapter.py          # TelexAdapter + register(ctx) + hooks
    client.py           # Telex OpenAPI client (X-API-Key)
    monitor.py          # subscribe stream: heartbeat, reconnect, backfill
    dispatcher.py       # inbound normalization -> MessageEvent
    access.py           # dm/group policies (+ pairing delegated to gateway)
    accounts.py         # multi-account resolution
    blocks.py media.py send.py targets.py tools.py types.py log.py config.py
  scripts/local-test.sh # local test env (tunnel to a remote Voyager instance)
  docs/spec  docs/test
```

## Install

```bash
git clone <hermes-telex repo> ~/.hermes/plugins/telex
hermes plugins enable telex-platform
hermes gateway run
```

## Get a bot API key

1. In Voyager Telex, register a bot (owner account): `POST /voyager/v1/telex/register-bot`
   (web SDK `telexService.registerBot`). See [docs/spec TD §2](docs/spec/td_hermes-telex-plugin_zh.md).
2. Copy the one-time **plaintext API key** (shown only once) and the returned `bot.id`.
3. The base URL is your Voyager host (default `https://voyager.ingarena.net`).

The key authenticates as the bot identity; every message the plugin sends is
attributed to that bot.

## Configuration

Full config lives in `~/.hermes/config.yaml` under `platforms.telex.extra`
(a YAML translation of openclaw-telex's `channels.telex`):

```yaml
platforms:
  telex:
    enabled: true
    extra:
      api_key: "<plaintext bot api key>"
      base_url: "https://voyager.ingarena.net"
      bot_id: "0a1b2c3d4e5f6071"        # optional; hardens self-echo suppression
      dm_policy: allowlist               # open | allowlist | pairing
      allow_from: ["alice@company.com", "0a1b2c3d4e5f6071"]
      group_policy: disabled             # disabled | allowlist | open
      group_allow_from: ["<channel conversation id>"]
      group_sender_allow_from: ["alice@company.com"]
      group_require_mention: true        # in channels, only respond when @-mentioned
      processing_indicator: activity     # activity | off
      tools:
        search_identities: true
        get_identities: true
        list_conversations: true
        get_conversation_info: true
        list_members: true
        get_conversation_messages: true
      accounts:                          # optional multi-bot
        support:
          api_key: "<another bot key>"
          dm_policy: open
          allow_from: ["*"]
```

Defaults: `base_url=https://voyager.ingarena.net`, `dm_policy=allowlist`,
`group_policy=disabled`, `group_require_mention=true`,
`processing_indicator=activity`, all tools enabled.
`dm_policy: open` requires `allow_from` to include `"*"`.

### Access & pairing

Access is enforced in-plugin (`enforces_own_access_policy`): the plugin gates
`open`/`allowlist` DMs and all channel policies before dispatch. `dm_policy: pairing`
is **delegated to the Hermes gateway's built-in pairing handshake** — unknown DMs
are forwarded so the gateway can issue a pairing code and consult its pairing store.

### Env quickstart (single account)

```dotenv
TELEX_API_KEY=<plaintext bot api key>
TELEX_BASE_URL=https://voyager.ingarena.net
TELEX_BOT_ID=0a1b2c3d4e5f6071
TELEX_DM_POLICY=allowlist
TELEX_ALLOW_FROM=alice@company.com
TELEX_GROUP_POLICY=disabled
```

See `env.example`. Env seeds a single default account via the `env_enablement_fn`
hook; YAML is the full-featured (multi-account) config.

## How it works

- **Inbound** opens one long-lived `GET /openapi/telex/subscribe` stream (NDJSON,
  not SSE) covering every conversation the bot belongs to. Mention is derived
  client-side from `mention_ids`/`mention_all`. On reconnect the plugin backfills
  gaps per conversation with `list-messages(after_seq)`.
- **Outbound** posts text blocks to `send-message` (chunked, one-shot COMPLETED),
  with a `processing` activity indicator while the agent runs.
- **Media** flows through the OpenAPI file endpoints (`upload-file`/`download-file`,
  ≤ 20 MiB). Inbound image/file blocks are auto-downloaded into the Hermes cache
  and handed to the agent; outbound attachments are uploaded and sent as media blocks.
- **Self-echo suppression**: the subscribe stream fans the bot's own messages back
  to it, so the plugin drops messages from its own identity (learned from `bot_id`
  and every send response).

## Agent tool

When enabled, a read-only `telex` tool lets the agent inspect Telex:
`search_identities`, `get_identities`, `list_conversations`, `get_conversation_info`,
`list_members`, `get_conversation_messages`. Each can be disabled under
`platforms.telex.extra.tools`. It is **not** for sending — use
`send_message(target="telex:<chat>")`.

## send_message targets

| Target | `send_message` target |
| --- | --- |
| a conversation (chat or channel) | `telex:<conversation_id>` |
| a 1:1 chat with an identity | `telex:peer/<identity_id>` |
| a 1:1 chat by email | `telex:email/<email>` or `telex:<email>` |

## Local test environment

`scripts/local-test.sh` brings up a local Telex test environment by tunnelling to
a remote Voyager instance and running the local web frontend:

```bash
scripts/local-test.sh up            # tunnel (127.0.0.1:8000) + web (:3000)
scripts/local-test.sh register-bot --token <voyager_session JWT>
scripts/local-test.sh env           # print hermes-telex env for this environment
scripts/local-test.sh down
```

Then run the gateway with `TELEX_BASE_URL=http://127.0.0.1:8000`. See
[docs/test TP §2.1](docs/test/tp_hermes-telex-plugin_zh.md).

## Tests

Offline unit tests (no real credentials, no live Hermes) via fakes:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -q
```

## Limitations

- **No threads.** Each Telex conversation maps to one agent session.
- **Media ≤ 20 MiB** (server upload cap); inbound media is auto-downloaded.
- Reconnect backfill uses in-memory per-conversation cursors: a transient
  disconnect is recovered, but a full process restart starts cold.
- Gateway pairing is per-platform, so multi-account + pairing shares one approval
  namespace.
- Outbound is one-shot COMPLETED (matching openclaw-telex); native streaming
  (IN_PROGRESS→COMPLETED) is an optional future enhancement.
