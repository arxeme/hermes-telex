# Deploy hermes-telex to the test environment

Publishes this plugin into a remote hermes-agent instance running in an Incus VM
on the Voyager test server, via SMC + `incus`. Same pattern as hermes-seatalk.

## Config split

- **Runtime config lives in the VM's `~/.hermes/config.yaml`** under
  `platforms.telex.extra.accounts.<id>` (Voyager's own format). It is managed on
  the server, not in this repo. The deploy script **does not touch config.yaml**,
  so it persists across redeploys. Canonical shape:

  ```yaml
  platforms:
    telex:
      enabled: true
      extra:
        accounts:
          default:
            api_key: "<bot key>"
            base_url: "http://192.168.100.1:8000"   # VM-reachable Voyager API
            bot_id: "<bot id>"
            dm_policy: allowlist
            allow_from: ["you@sea.com"]
            group_policy: open
            group_require_mention: false
            group_sender_allow_from: ["you@sea.com"]
  ```

- **`deploy/env.local`** (copy from `env.example.local`, gitignored) holds only
  **WHERE** to deploy — SMC profile, test server, Incus VM, user. `deploy-*.sh`
  sources it. This is the only local file you fill in.

Bot key comes from `scripts/local-test.sh register-bot` (see the runbook).

### One-time setup

```bash
cp deploy/env.example.local deploy/env.local     # fill SMC_PROFILE/SERVER_HOST/VM_NAME/REMOTE_USER
```

## Deploy / update

```bash
deploy/deploy-telex-plugin.sh                    # packages + uploads + enables + restarts gateway
```

It stops the gateway, replaces `HERMES_HOME/plugins/telex-platform`, installs
`requirements.txt` into the hermes venv, verifies `register(ctx)`, enables the
plugin, and restarts `hermes-gateway.service`. It **does not** modify
`config.yaml` — the `platforms.telex` config is maintained on the VM.

> The script still accepts `--runtime-env-file <path>` to push a `~/.hermes/.env`,
> but that is unnecessary when config lives in `config.yaml` (the normal case).

## Remove

```bash
deploy/cleanup-telex-plugin.sh                   # disable + remove plugin, restart gateway
```

## Verify (on the VM)

```
hermes gateway status      # expect: Telex connected
```

Full runbook: [../docs/test/e2e_hermes-telex_runbook_zh.md](../docs/test/e2e_hermes-telex_runbook_zh.md)
