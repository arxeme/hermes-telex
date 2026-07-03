---
标题: Hermes Telex Plugin 真实联调 Runbook (W-12)
状态: draft
更新日期: 2026-07-02
参考材料:
  - 测试计划 (./tp_hermes-telex-plugin_zh.md) §2.1 / §5
  - 本地测试脚本 (../../scripts/local-test.sh)
  - 部署脚本 (../../deploy/README.md)
文档摘要: W-12 真实联调步骤。区分「测试者客户端(本机)」与「远端 hermes-agent 实例(部署)」两侧。
---

# Hermes Telex Plugin 真实联调 Runbook (W-12)

## 拓扑

hermes-agent **运行在远端**——即 Voyager 测试后端所在的机器上的一个 Incus VM（mate 实例）。
联调分两侧：

```
[本机: 测试者客户端]                         [远端: Voyager 测试服 + hermes-agent VM]
 local-test.sh up                             voyager API :8000
   ├─ chisel tunnel  ──────────────────────►  (被 tunnel 到本机 127.0.0.1:8000)
   └─ make web dev  (localhost:3000) ───────►  用真实账号登录、注册 bot、与 bot 对话
 deploy/deploy-telex-plugin.sh  ───SMC/incus──►  hermes-agent VM: 装 telex 插件 + 起 gateway
```

- **本机侧**：`local-test.sh` 提供浏览器可用的 Telex Web（经 tunnel 打到远端 voyager），用于**登录、注册 bot、发消息验证**。
- **远端侧**：hermes-telex 插件 + hermes-agent 实例跑在 VM 里；插件的 `TELEX_BASE_URL` 用 **VM 内可达的 voyager 地址**（Incus host 网关或内网 `voyager.ingarena.net`），**不是** tunnel 地址。

关键路径：

| 名称 | 路径 |
| --- | --- |
| 本插件 | `openclaw/hermes-telex`（本机源码，deploy 从此打包） |
| 部署脚本 | `openclaw/hermes-telex/deploy/` |
| 远端 hermes-agent | VM 内 `~/hermes-agent`（`REMOTE_HERMES_INSTALL_DIR`） |

## 0. 前置检查

| 项 | 现状 / 待办 |
| --- | --- |
| chisel / pnpm / make / node（本机） | ✅ 就绪（`local-test.sh` 可跑） |
| Voyager `web/node_modules` | ✅ 已装 |
| SMC 访问测试服 | 需 `smc` 配好 profile（`deploy/env.local`），能 `smc toc <server>` |
| 远端 hermes-agent 实例 | ⏳ 你稍后在 VM 内新建/起好（含 LLM provider key，agent 才能回复） |
| Telex bot key | 步骤 2 现场颁发 |

> 本机 `ref/hermes-agent`（那份损坏的 `.venv`）仅用于**读源码**，不参与运行。

## 1. 起测试者客户端（本机）

```bash
cd openclaw/hermes-telex
scripts/local-test.sh up          # chisel tunnel(127.0.0.1:8000) + web(:3000)
scripts/local-test.sh status
```

## 2. 注册 Telex bot（取一次性 key）

1. 浏览器开 `http://localhost:3000`，用**真实账号**登录。
2. 取 session JWT：DevTools → Application → Local Storage → `voyager_session`。
3. 颁发 bot（保存 `plaintext_key` 与 `bot.id`，key 仅此一次）：

```bash
scripts/local-test.sh register-bot --token '<voyager_session JWT>' --name hermes-telex-e2e
```

## 3. 运行时配置（在 VM 的 config.yaml，非本仓库）

运行时配置（bot key + 策略）由 **VM 的 `~/.hermes/config.yaml`** 承载，格式即 Voyager 现有格式
（每个账号在 `platforms.telex.extra.accounts.<id>` 下，单账号用 `accounts.default`）：

```yaml
platforms:
  telex:
    enabled: true
    extra:
      accounts:
        default:
          api_key: "<bot key>"
          base_url: "http://192.168.100.1:8000"   # VM 内可达的 voyager 地址（非 tunnel）
          bot_id: "<bot id>"
          dm_policy: allowlist
          allow_from: ["you@sea.com"]
          group_policy: open
          group_require_mention: false
          group_sender_allow_from: ["you@sea.com"]
```

把步骤 2 拿到的 `api_key`/`bot_id`/你的 email 填进去。此文件在服务器上维护，deploy 脚本**不改动**它。

## 4. 部署插件到远端 hermes-agent（VM）

```bash
cp deploy/env.example.local deploy/env.local   # 连接目标：SMC_PROFILE/SERVER_HOST/VM_NAME/REMOTE_USER
deploy/deploy-telex-plugin.sh
```

脚本会：打包插件 → `smc scp` + `incus file push` 进 VM → 停 gateway → 替换
`HERMES_HOME/plugins/telex-platform` → 装 `requirements.txt` → 校验 `register(ctx)` →
`hermes plugins enable telex-platform` → 重启 `hermes-gateway.service` → 打印 `hermes gateway status`。
**config.yaml 不被脚本改动**（在 VM 上维护）。

期望：status 中 **Telex connected**。

## 5. 联调用例（对应 TP §5）

在**本机浏览器**（`localhost:3000`，真实账号）与 bot 交互，观察远端 agent 回复。

| 用例 | 操作 | 期望 |
| --- | --- | --- |
| E-01 取 key + connected | 步骤 2/4 | status 显示 Telex connected |
| E-02 1:1 收发 | 私聊 bot | agent 回复到达；自发不回流 |
| E-03 channel @bot | 频道 @ / 不 @ | @ 触发、未 @ 不触发 |
| E-04 媒体 | 发图/文件；让 agent 回图 | 入站被接收、出站正确显示 |
| E-05 回填 | 远端重启 gateway 后补发的消息 | 回填不丢不重 |
| E-06 鉴权拒绝 | 非 `allow_from` 账号私聊 | 无响应 |
| E-07 pairing | `TELEX_DM_POLICY=pairing`，未批准账号私聊 | 配对码/提示；网关批准后可对话 |
| E-08 多账号 | `platforms.telex.extra.accounts` 两 bot | 各自独立 |
| E-10 key 轮换 | rotate 后更新 `deploy/.env` 重部署 | 旧 key 失效、新 key 生效 |

结果记入 `docs/test/tr_hermes-telex-plugin_e2e_zh.md`。

## 6. 收尾

```bash
deploy/cleanup-telex-plugin.sh     # 远端：禁用+移除插件，重启 gateway
scripts/local-test.sh down         # 本机：停 tunnel + web
# 可选：unregister-bot 清理测试 bot
```

## 故障排查

- **Telex 未 connected**：VM 内查 `hermes gateway status` 日志 `telex/subscribe`；确认 `TELEX_BASE_URL` VM 内可达、`TELEX_API_KEY` 有效。
- **401/403 频繁重连**：key 失效（轮换后未重部署 `deploy/.env`）。
- **agent 不回复**：VM 内 hermes 未配 LLM provider，或 `telex` 未 enable。
- **私聊/频道无响应**：查 `dm_policy`/`allow_from`、`group_policy`/`group_require_mention`。
- **deploy 脚本报 `missing SMC_PROFILE` 等**：`deploy/env.local` 未填全。
- **`command -v hermes` 失败（VM 内）**：远端 hermes-agent 实例未装好/未在 PATH。
