---
标题: Hermes Telex Plugin 测试计划
状态: draft
更新日期: 2026-06-26
参考材料:
  - Hermes Telex Plugin 技术设计 (../spec/td_hermes-telex-plugin_zh.md)
  - Hermes Telex Plugin 工作分解结构 (../spec/wbs_hermes-telex-plugin_zh.md)
文档摘要: 定义 Hermes Telex Plugin 的自动化测试、集成测试和真实联调用例。
---

# Hermes Telex Plugin 测试计划

## 1. 测试目标

本测试计划覆盖 Hermes Telex Plugin 的外部 plugin 安装、平台注册、Telex 收发、鉴权、subscribe 流处理、
出站（含可选流式）和真实联调。测试以离线自动化为主，真实 Telex 环境验证作为人工联调补充。

测试边界：

- 自动化测试不依赖真实 Telex 凭证（mock OpenAPI 与 subscribe 流）。
- hermes-agent 仓库零改动、零 monkey-patch；测试覆盖 `register(ctx)` 经 `ctx.register_platform` 的注册路径与 hooks。
- 入站只有 subscribe 单通道；测试覆盖 NDJSON 解析、心跳 stale、重连回填。
- Telex 无 thread；目标解析与 source 归一不含 thread 维度。
- **功能对标 openclaw-telex**：TD §12 对标清单逐项须有用例覆盖。
- 访问策略由插件内实现（`enforces_own_access_policy`）：dm(open/allowlist/pairing)+group(disabled/allowlist/open)+sender+require_mention；
  `pairing` 委托 hermes-agent 网关配对握手（测试覆盖"转发网关"这一契约，不重测网关内部）。
- 配置为 openclaw `channels.telex` 的 YAML 等效（`platforms.telex.extra`），含多账号。

## 2. 测试环境

| 类型 | 工具/方式 | 用途 |
| --- | --- | --- |
| 单元测试 | `pytest` | 配置、client、blocks、normalizer、registration、目标解析 |
| 异步测试 | `pytest-asyncio` | subscribe 流、async OpenAPI client、流式发送 |
| HTTP mock | `aiohttp` test server 或 fixture | Telex OpenAPI mock、upload/download |
| 流 mock | 可控字节流/`aiohttp` streaming response | subscribe NDJSON、心跳、断流、回填 |
| 环境隔离 | pytest fixture | env var、registry、缓存状态隔离 |
| 真实联调 | **本地 tunnel 到远端 Voyager 实例** + 真实 bot key + hermes gateway | 端到端验证（见 §2.1） |

### 2.1 联调环境：本地 tunnel + web dev（`scripts/local-test.sh`）

E2E 直接连一个**部署在远端的真实 Voyager 实例**（Telex 内含其中），不再依赖本地起栈或 mock。
本机通过 Voyager 自带 make 目标把远端服务端口 tunnel 到 localhost：

- `make deploy tunnel-test with-server` —— chisel 把远端各端口转发到本机，含 **Voyager API `127.0.0.1:8000`**（`with-server`）。
- `make web dev` —— 本机 Next.js 前端 `http://localhost:3000`，打向 `127.0.0.1:8000`。

一键封装为 [`scripts/local-test.sh`](../../scripts/local-test.sh)（自动定位 Voyager 仓库，可用 `VOYAGER_DIR` 覆盖）：

```bash
scripts/local-test.sh up            # 起 tunnel + web，等就绪，打印 URL 与 env
scripts/local-test.sh register-bot --token <voyager_session JWT>   # 在该实例上注册 bot，取一次性 key
scripts/local-test.sh env           # 打印 hermes-telex 的 env 块
scripts/local-test.sh down          # 停 web + tunnel
scripts/local-test.sh status|logs   # 巡检 / 看日志
```

联调时 hermes-telex 配置：`TELEX_BASE_URL=http://127.0.0.1:8000`、`TELEX_API_KEY`/`TELEX_BOT_IDENTITY_ID`
取自 `register-bot`、`TELEX_ALLOWED_USERS` 填自己的真实账号 email。前置：本机具备 `chisel`/`pnpm`/`make`，
且 Voyager 仓库 `web` 依赖已装（`make -C <voyager> init`）。`register-bot` 的 session JWT 从
`http://localhost:3000` 登录后浏览器 `localStorage.voyager_session` 获取。

建议测试目录：

```text
tests/
  unit/
    test_registration.py      # 注册 + YAML/env 配置翻译 + 校验
    test_openapi_client.py
    test_blocks.py
    test_stream.py
    test_dispatcher.py
    test_access.py            # dm/group 策略 + pairing 转发 + 匹配规则
    test_accounts.py          # 多账号覆盖合并
    test_outbound_adapter.py
    test_targets.py
    test_streaming.py        # 可选，超出对标
  integration/
    test_inbound_flow.py
    test_send_flow.py
    test_cron_standalone.py
    test_reconnect_backfill.py
  e2e/
    README.md
```

## 3. 自动化测试用例

### T-00 Plugin 包骨架与安装入口

覆盖 WBS：W-00

| 用例 | 断言 |
| --- | --- |
| T-00-01 manifest 可解析 | `plugin.yaml` 字段完整，`name/kind/requires_env/optional_env` 符合 loader 要求 |
| T-00-02 未启用无副作用 | 未 enable 时不注册 `telex`、不触发任何运行时代码 |
| T-00-03 register 可 import | root `adapter.register` 可导入；缺依赖错误可诊断 |
| T-00-04 env 示例完整 | `env.example` 覆盖 TD 必需/可选配置，无真实 secret |

### T-01 Plugin 注册与平台状态语义

覆盖 WBS：W-01

| 用例 | 断言 |
| --- | --- |
| T-01-01 注册成功 | enable 后 `ctx.register_platform` 注册 `telex`，PlatformEntry 字段（hooks/callbacks/max_message_length/allowed_users_env）正确 |
| T-01-02 最小配置 configured | `TELEX_API_KEY` 完整时 configured/connected 为 true |
| T-01-03 缺 key 失败 | 缺 `TELEX_API_KEY` 时 `check_telex_requirements()`/`_validate_telex_config()` 返回 false |
| T-01-04 aiohttp 缺失失败 | `import aiohttp` 失败时 `check_telex_requirements()` 返回 false |
| T-01-05 `is_connected` 一致 | `_is_telex_connected(cfg)` 与 `_validate_telex_config(cfg)` 一致；不依赖 live adapter |
| T-01-06 env_enablement | 有 key 时 `_telex_env_enablement()` 返回含 `extra` 与（设置时）`home_channel`；无 key 返回 None |
| T-01-07 register 幂等 | 连续调用 `register(ctx)` 不重复注册、无异常 |
| T-01-08 零 patch | register 不修改 hermes-agent 任何模块属性（无 monkey-patch 痕迹） |

### T-02 Telex OpenAPI 客户端

覆盖 WBS：W-02

| 用例 | 断言 |
| --- | --- |
| T-02-01 鉴权头 | 每个请求带 `X-API-Key`；base URL 可配 |
| T-02-02 send_message 单目标 | 恰好传一个 conversation_id/peer_id/message_id；多目标或零目标报错 |
| T-02-03 list/get conversation | 解析 conversation 字段（kind/title/last_seq 等）正确 |
| T-02-04 batch-get-identities | 按 ids/emails 解析 identity；未知项跳过 |
| T-02-05 upload-file | multipart 字段名 `file`，>20 MiB 触发 `file_required_or_too_large` |
| T-02-06 download-file | 带 file_id/conversation_id/message_id；返回原始字节 |
| T-02-07 错误体映射 | `{code,message}` 映射统一异常；`not_a_member`/`message_too_long` 等可分支 |
| T-02-08 日志脱敏 | `X-API-Key` 不出现在日志/异常文本 |

### T-03 Block 模型与媒体

覆盖 WBS：W-03

| 用例 | 断言 |
| --- | --- |
| T-03-01 TEXT 拼接 | 多 TEXT block 按 seq 顺序拼接为正文 |
| T-03-02 入站媒体落 cache | IMAGE/FILE 经 download-file → `cache_*_from_bytes`，路径进 `media_urls`/`media_types` |
| T-03-03 出站媒体 | 本地文件 → upload-file → IMAGE/FILE block，引用 file_id |
| T-03-04 EVENT 分流 | `type=21` / `flags&1` 进入系统事件分支，不当作正文 |
| T-03-05 THINKING/TOOL 忽略 | 入站默认忽略，不污染正文 |

### T-04 Subscribe 入站流

覆盖 WBS：W-04

| 用例 | 断言 |
| --- | --- |
| T-04-01 逐行解析 | NDJSON 每行独立 `json.loads`；非 SSE |
| T-04-02 心跳重置 | `result.message=null` 空帧重置 stale 计时器，不当作消息 |
| T-04-03 stale 检出 | 超过 60s 无任何帧 → 判半开 → 触发重连 |
| T-04-04 error 帧重连 | `{"error":...}` 后流结束并重连 |
| T-04-05 退避重连 | 重连指数退避 + jitter；恢复后继续消费 |
| T-04-06 回填 | 重连后对受影响会话 `list-messages?after_seq=<最高 seq>` 回填 |
| T-04-07 回填去重 | 回填与流内消息按 id/seq 去重，不重复投递 |

### T-05 入站标准化与调度

覆盖 WBS：W-05

| 用例 | 断言 |
| --- | --- |
| T-05-01 dm source | 1:1 chat → `chat_type=dm`、`chat_id=conversation_id`、`thread_id=None` |
| T-05-02 channel source | channel → `chat_type=group`；`user_id`=sender email（解析），`user_id_alt`=identity_id |
| T-05-03 去重 | 同 `(conversation_id,seq)`/`id` 只投递一次 |
| T-05-04 自我过滤 | `sender_id==self_id`（bot_id 或首发学习）或 id∈已发 → drop |
| T-05-05 identity 解析缓存 | `sender_id`→email/display_name 经 batch-get-identities 解析并缓存 |
| T-05-06 skip 标志 | IN_PROGRESS / `flags&EVENT` / `flags&FORK_PREFIX` 均不进 agent（EVENT 走系统事件同步） |
| T-05-07 内容抽取 | 拼 TEXT block；IMAGE/FILE 下载落 cache 进 media_urls；空文本用占位符 |
| T-05-08 per-conversation 串行 | 同 `account:conversation` 排队一次一轮；不同会话并发 |
| T-05-09 系统事件 | member_added/renamed 等更新本地缓存，不作内容回复；未知 kind 忽略 |
| T-05-10 mention 标记 | channel 命中 @（mention_ids 含自身 或 mention_all）标记 `telex_was_mentioned`；dm 始终触发 |

> 访问策略（dm/group/pairing 门控）见 T-13；fork/mention 上下文回填见集成 I-08。

### T-06 出站适配器

覆盖 WBS：W-06

| 用例 | 断言 |
| --- | --- |
| T-06-01 发文本 | `send(content)` → send-message(status=0)，目标正确 |
| T-06-02 target: conversation_id | `telex:<hex>` → `conversation_id` |
| T-06-03 target: peer | `telex:peer/<id>` → `peer_id` |
| T-06-04 target: email | `telex:email/<x>` → batch-get-identities → peer_id |
| T-06-05 email 失败 | 无活跃 identity 时返回可读 `SendResult.error`，负向缓存，不抛异常 |
| T-06-06 分片 | 超 `TELEX_TEXT_CHUNK_LIMIT`（默认 200000）拆多条 COMPLETED |
| T-06-07 typing 保活 | `processing_indicator=activity` 时 `send_typing`→set-activity 每 ~3s 保活；`off` 不发 |
| T-06-08 图片/文件 | `send_image_file`/`send_document` 经 upload-file（≤20MiB）+ media block；失败降级占位文本 |
| T-06-09 文本+媒体 | 一条消息含 [TEXT, MEDIA...] 多 block |

### T-07 cron 与 out-of-process 投递

覆盖 WBS：W-07

| 用例 | 断言 |
| --- | --- |
| T-07-01 cron 路由 | `deliver="telex"` 解析到 `TELEX_HOME_CHANNEL`（经 `cron_deliver_env_var`） |
| T-07-02 cron 覆盖目标 | `deliver="telex:<target>"` 覆盖 home |
| T-07-03 standalone 发送 | `standalone_sender_fn` 不依赖 live adapter 完成文本/媒体发送，返回 message_id |
| T-07-04 home 缺失 | 未设 `TELEX_HOME_CHANNEL` 时 `deliver="telex"` 行为明确（拒绝/报错可诊断） |

### T-08 原生流式（可选 v2）

覆盖 WBS：W-08

| 用例 | 断言 |
| --- | --- |
| T-08-01 supports 开关 | `TELEX_STREAMING=false` → `supports_draft_streaming()` 返回 false |
| T-08-02 首帧建 IN_PROGRESS | 首个 `send_draft` → send-message(status=1) 并记 message_id |
| T-08-03 增量追加 | 后续帧计算 delta，仅追加新增文本（status=1），verbatim 拼接 |
| T-08-04 finalize 不重复 | 终稿 `send()` finalize 现有 in-progress 消息（status=0），不新建重复消息 |
| T-08-05 segment 边界 | draft_id 自增 → 上一段 status=0 收尾，新段起新 IN_PROGRESS |
| T-08-06 失败降级 | `send_draft` 失败返回 success=false，不抛；流程回退一次性 COMPLETED |

### T-09 telex 工具（6 只读 action，逐项开关）

覆盖 WBS：W-09

| 用例 | 断言 |
| --- | --- |
| T-09-01 注册 | 工具名 `telex` 经 ctx 注册；schema 明确"不用于发送" |
| T-09-02 六 action | search_identities/get_identities/list_conversations/get_conversation_info/list_members/get_conversation_messages 映射正确 |
| T-09-03 逐项开关 | `tools.<name>=false` 时对应 action 不注册/拒绝；默认全开 |
| T-09-04 输出格式 | 枚举转小写标签、flags 转标签列表；messages 升序 |
| T-09-05 错误可诊断 | 无效参数/无权限返回可读错误 |

### T-12 多账号

覆盖 WBS：W-14

| 用例 | 断言 |
| --- | --- |
| T-12-01 覆盖合并 | `accounts.<id>` 覆盖顶层；未覆盖字段继承顶层默认 |
| T-12-02 enabled/configured | enabled=顶层&&账号；configured=有 api_key；未配置账号被跳过 |
| T-12-03 单账号退化 | 仅顶层配置时等价单账号，行为不变 |
| T-12-04 并行订阅 | 多启用账号各自 client/stream 并行，日志带 account 维度 |

### T-13 访问控制与配对

覆盖 WBS：W-13（+ W-15 mention 部分）

| 用例 | 断言 |
| --- | --- |
| T-13-01 匹配规则 | `is_sender_allowed`：`*` 全通过、id 精确、email 大小写不敏感 |
| T-13-02 dm allowlist | dm_policy=allowlist：allow_from 命中放行，否则 drop |
| T-13-03 dm open 校验 | dm_policy=open 缺 `*` → 配置校验失败 |
| T-13-04 dm pairing 转发 | dm_policy=pairing：DM **不**在插件预过滤，转发网关（`enforces_own_access_policy` 但 `_dm_policy=pairing`） |
| T-13-05 group disabled | group_policy=disabled → 所有 channel 消息 drop |
| T-13-06 group allowlist | group_policy=allowlist：conversation_id∈group_allow_from 才继续 |
| T-13-07 group sender | group_sender_allow_from 非空时 sender 须匹配，否则 drop |
| T-13-08 require_mention | group_require_mention=true：未 @bot drop；@bot 放行 |
| T-13-09 enforces flag | `TelexAdapter.enforces_own_access_policy is True`；`_dm_policy` 暴露解析值 |

## 4. 集成测试

覆盖 WBS：W-01..W-09, W-13..W-15

| 用例 | 断言 |
| --- | --- |
| I-01 入站全链路 | mock subscribe 流推一条 dm/channel 消息 → 产出正确 `MessageEvent` 并 `handle_message` |
| I-02 出站全链路 | mock OpenAPI + `send()` 到 conversation / peer，payload 与协议一致 |
| I-03 媒体出站 | `send_message` 带 `MEDIA:<image>` → upload-file + IMAGE block 到达 mock |
| I-04 cron home | 设 `TELEX_HOME_CHANNEL`，`deliver="telex"` 经 standalone_sender_fn 送达 home |
| I-05 重连回填 | 断流后重连，`after_seq` 回填断窗消息且去重不重复 |
| I-06 访问拒放（in-plugin） | dm_policy=allowlist 未命中 sender 不进 agent；group_policy 生效——由插件预过滤（enforces_own_access_policy） |
| I-07 pairing 委托网关 | dm_policy=pairing 时未批准 DM 转发网关触发配对握手；`pairing_store` 批准后放行 |
| I-08 fork/mention 上下文 | fork 会话继承父会话 + fork 前历史注入；require_mention channel @ 命中后回填漏消息作 thread |
| I-09 多账号 | 两账号并行订阅，各按自身策略处理，互不串扰 |
| I-10 流式全链路（可选，超出对标） | mock OpenAPI，draft 帧序列 → Telex IN_PROGRESS 追加 → finalize 单条 COMPLETED |

## 5. 真实联调（HITL）

覆盖 WBS：W-12，结果落 `docs/test/tr_hermes-telex-plugin_*_zh.md`。
环境按 §2.1 用 `scripts/local-test.sh up` 起（tunnel 到远端 Voyager 实例）；hermes gateway 配
`TELEX_BASE_URL=http://127.0.0.1:8000`。收尾务必 `scripts/local-test.sh down`，并按需 `unregister-bot` 清理测试 bot。

| 场景 | 验收 |
| --- | --- |
| E-00 起环境 | `scripts/local-test.sh up` 后 `:8000`/`:3000` 就绪；`http://localhost:3000` 可用真实账号登录 |
| E-01 取 key | `scripts/local-test.sh register-bot` 颁发 bot key；hermes gateway 配置后 `hermes gateway status` 显示 Telex connected |
| E-02 1:1 收发 | 真人向 bot 私聊，agent 回复到达；自发消息不回流 |
| E-03 channel @bot | channel 中 @bot 触发，未 @ 不触发；channel 准入生效 |
| E-04 媒体 | 入站图片/文件被 agent 接收；出站图片/文件正确显示 |
| E-05 回填 | 断网/重启 gateway 后，断窗消息经回填不丢不重 |
| E-06 鉴权拒绝 | dm_policy=allowlist 下非 allow_from 用户消息被拒，无 agent 响应 |
| E-07 pairing | dm_policy=pairing：未批准用户 DM 收到配对码/提示；批准后可正常对话 |
| E-08 多账号 | 两 bot 各自私聊/频道独立工作 |
| E-09 流式（可选，超出对标） | Telex UI 出现增量打字效果，终稿单条且内容完整 |
| E-10 key 轮换 | `TelexRotateBotKey` 后旧 key 失效、新 key 生效（更新配置 + 重启） |

## 6. 测试数据与 mock 约定

- **mock OpenAPI**：按 OpenAPI guide 返回结构（snake_case、enum 为整数、零值字段照常输出、id 为 16-hex）。
- **mock subscribe 流**：发出 `{"result":{"message":null}}` 心跳与 `{"result":{"message":{...}}}` 消息，
  支持注入 `{"error":...}` 模拟断流；用于 stale/重连/回填测试。
- **身份**：固定一组 identity（含 bot 自身、若干 user），覆盖 email 有/无、active/retired。
- **不得**在仓库内提交真实 `TELEX_API_KEY`；联调 key 仅置于本地未跟踪配置。
