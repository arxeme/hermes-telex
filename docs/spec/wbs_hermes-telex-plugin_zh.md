---
标题: Hermes Telex Plugin 工作分解结构
状态: draft
更新日期: 2026-06-26
参考材料:
  - Hermes Telex Plugin 技术设计 (./td_hermes-telex-plugin_zh.md)
  - Hermes Telex Plugin 测试计划 (../test/tp_hermes-telex-plugin_zh.md)
文档摘要: 将 Hermes Telex Plugin 技术设计拆解为可执行任务、依赖关系、完成条件和验证方式。
---

# Hermes Telex Plugin 工作分解结构

## 1. 范围

本 WBS 基于 [`td_hermes-telex-plugin_zh.md`](./td_hermes-telex-plugin_zh.md)，目标是在当前版本
hermes-agent 上以外部 platform plugin 方式提供 Voyager Telex 平台接入能力。实现边界：

- Telex 相关代码全部位于 plugin 包内；**hermes-agent 仓库零改动、零 monkey-patch**。
- 采用现代 Plugin Path：`register(ctx)` 内 `ctx.register_platform(PlatformEntry(...))` + hooks
  （`cron_deliver_env_var` / `env_enablement_fn` / `standalone_sender_fn` / `platform_hint`）。
  IRC 参考插件为零-patch 范本。
- **功能对标 openclaw-telex**（`openclaw/openclaw-telex`）：需实现其全部功能（TD §12 对标清单）。
- 入站唯一通道为 Telex `subscribe` NDJSON 长连接；无 webhook / relay / websocket 分支。
- 鉴权单一 `X-API-Key`（绑定一个 Telex BOT identity）。
- **访问策略在插件内实现**（`enforces_own_access_policy=True`）：dm_policy(open/allowlist/pairing) +
  group_policy(disabled/allowlist/open) + sender allowlist + require_mention；**pairing 委托 hermes-agent 网关内建配对**。
- 配置为 openclaw `channels.telex` 的 **YAML 等效**（`platforms.telex.extra`，snake_case），支持多账号 `accounts.<id>`。
- Telex 无 thread；出站一次性 COMPLETED（对标基线，openclaw 不用流式）；原生流式为可选增强。

当前没有单独 PRD，本 WBS 直接承接 TD 的技术方案与设计 review 已收敛的约束。

## 2. 任务清单

| ID | 任务 | 依赖 | 执行方式 | 覆盖范围 | 验收要点 |
| --- | --- | --- | --- | --- | --- |
| W-00 | Plugin 包骨架与安装入口 | 无 | AFK | plugin.yaml(富 env)、包结构、root shim、依赖声明、env.example | hermes discover/import plugin；未启用时无副作用 |
| W-01 | Plugin 注册、平台状态、YAML 配置 | W-00 | AFK | `register(ctx)`、`ctx.register_platform`、callback、hooks（`apply_yaml_config_fn` 翻译 `channels.telex`→`platforms.telex.extra`、`env_enablement_fn`）、`enforces_own_access_policy` 声明 | 注册 `telex`；YAML/env 解析与默认/校验（open 需 `*`）符合 TD §5；register 幂等 |
| W-02 | Telex OpenAPI 客户端 | W-00 | AFK | X-API-Key client、identities/conversations(缓存)/messages/files、错误映射、backfill watermark、自发抑制、dedup | 可 mock；不泄漏 API Key |
| W-03 | Block 模型与媒体 | W-02 | AFK | block 构造/解析、upload-file、download-file、cache、占位符 | TEXT 拼接；IMAGE/FILE 经 file_id；≤20MiB；失败降级 |
| W-04 | Subscribe 入站流 / monitor | W-02, W-03 | AFK | NDJSON、心跳 stale 60s、退避 1s→30s、readiness、`after_seq` 回填(100/页,≤50 页)、401/403 致命 | 半开可恢复；回填不丢不重 |
| W-05 | 入站标准化与调度 | W-04, W-13 | AFK | watermark、dedup、自发抑制、skip IN_PROGRESS/EVENT/FORK_PREFIX、内容抽取、identity 解析、build_source、per-conversation 串行、系统事件同步 | message→MessageEvent；自发不回流；串行正确 |
| W-06 | 出站适配器 | W-01, W-02, W-03 | AFK | `send`(一次性 COMPLETED)/`send_typing`(activity 3s 保活)/媒体、`_resolve_target`、文本分片(200k)、文本+媒体多 block | 三类 target；分片；email 失败可读错误 |
| W-07 | cron 与 out-of-process 投递 | W-01, W-06 | AFK | `cron_deliver_env_var`、`standalone_sender_fn`、home channel | `deliver="telex"` 路由 home；离线不依赖 live adapter |
| W-08 | 原生流式（增强，超出对标） | W-06 | AFK | `supports_draft_streaming`、`send_draft` 累计全文→追加、`send()` finalize | 增量到达；终稿不重复；失败降级一次性 |
| W-09 | telex 工具（6 只读 action，逐项开关） | W-01, W-02 | AFK | search_identities/get_identities/list_conversations/get_conversation_info/list_members/get_conversation_messages | 逐项开关；不用于发送；枚举转标签 |
| W-10 | 配置、安装与运维文档 | W-01..W-09,W-13..W-15 | AFK | README、env.example、YAML 样例、setup wizard、status、Bot 注册/Key 运维说明 | 按文档可注册 bot、取 key、YAML 配置、验证 |
| W-11 | 自动化测试集 | W-01..W-09,W-13..W-15 | AFK | pytest 单测、集成、mock OpenAPI/subscribe 流 | TP §3/§4 离线可验证 |
| W-12 | 真实 Telex 联调验证 | W-10, W-11 | HITL | `scripts/local-test.sh` + 真实 bot key、1:1/channel/pairing/媒体/回填 | 端到端；至少一条路径通过；用毕 unregister |
| W-13 | 访问控制与配对 | W-02 | AFK | `access.py`：dm(open/allowlist/pairing)+group(disabled/allowlist/open)+sender allowlist+require_mention；`enforces_own_access_policy=True`+`_dm_policy`；pairing 委托网关 | 匹配规则(`*`/id/email)；pairing DM 转发网关；`open` 需 `*` |
| W-14 | 多账号 | W-01 | AFK | `accounts.py`：`accounts.<id>` 覆盖合并、enabled/configured 判定、per-account stream | 覆盖正确；多账号并行订阅 |
| W-15 | fork 与 mention 上下文回填 | W-05 | AFK | fork 继承父会话 + fork 前历史(50)；require_mention 命中后回填漏消息(50)作 thread | 上下文正确注入 |

> W-08 为可选增强（超出对标基线）；**W-13/W-14/W-15 为对标必备**。任务以依赖为准排期（不设固定发布相位）。

## 3. 依赖关系

```text
W-00
  -> W-01 -> W-14 (multi-account)
       -> W-06 -> W-07
               -> W-08            (optional, beyond parity)
       -> W-09
  -> W-02 -> W-03
          -> W-13 (access + pairing)
          -> W-04 -> W-05 -> W-15 (fork + mention context)
          -> W-06
W-05 depends on W-04 and W-13
W-10/W-11 depend on W-01..W-09 + W-13..W-15
W-12 depends on W-10 and W-11
```

执行顺序以依赖为准；若实现中发现 TD 与 hermes-agent 实际 plugin API 不一致（尤其 §0.1 两处待核对：
默认 `_parse_target_ref` 行为、`_send_via_adapter` 的 media 转发链路），应先在 W-01/W-06 记录差异并更新 TD，再继续。

## 4. 任务明细

### W-00 Plugin 包骨架与安装入口

目标：建立可被 hermes-agent plugin loader 识别的外部 plugin 包。

交付物：

- root shim：`adapter.py`、`__init__.py`（re-export `hermes_telex.adapter.register`）。
- `hermes_telex/` package 骨架（`adapter.py`、`client.py`、`stream.py`、`dispatcher.py`、`blocks.py`、`targets.py`）。
- `plugin.yaml`（`kind: platform`、`name: telex-platform`、`requires_env`/`optional_env` 富描述）。
- `pyproject.toml`、`requirements.txt`（`aiohttp>=3.9`）。
- `env.example`，仅含 Telex 必需/可选配置，无真实 secret。

完成条件：未 enable 时 hermes 启动不 import/执行 Telex 运行时代码；enable 后可被 discover；import 失败信息可定位；无需改 hermes 仓库。

验证方式：T-00（见 TP）。

### W-01 Plugin 注册与平台状态语义

目标：以现代 Plugin Path 注册 `telex` 平台，确立配置/连接状态语义，**不使用任何 monkey-patch**。

交付物：

- `register(ctx)`：`ctx.register_platform(PlatformEntry(...))`，含 `adapter_factory`、`check_fn`、
  `validate_config`、`is_connected`、`setup_fn`、`env_enablement_fn`、`cron_deliver_env_var`、
  `standalone_sender_fn`、`allowed_users_env`、`allow_all_env`、`max_message_length`、`platform_hint`。
- `check_telex_requirements()` / `_validate_telex_config(cfg)` / `_is_telex_connected(cfg)`。
- `_telex_env_enablement()`（seed extra + home_channel）。

完成条件：`hermes plugins enable telex-platform` 后注册 `telex`；credentials 完整 → configured/connected；
`Platform("telex")` 经 `_missing_` 动态创建；`register(ctx)` 可重复调用不重复注册。

验证方式：T-01。

### W-02 Telex OpenAPI 客户端

目标：实现带 `X-API-Key` 的 Telex OpenAPI HTTP 客户端。

交付物：`client.py` —— identities（search/batch-get）、conversations（list/get/create-chat/create-channel）、
members（list/add）、messages（list/send/set-activity）、files（upload/download）；错误体 `{code,message,details}`
映射为统一异常；base URL 可配；日志脱敏。

完成条件：`send_message` 恰好一个目标参数（conversation_id|peer_id|message_id）；upload-file multipart ≤20 MiB；
稳定错误串可分支；API Key 不出现在日志/异常文本。

验证方式：T-02。

### W-03 Block 模型与媒体

目标：集中 block 构造/解析与媒体上传下载。

交付物：`blocks.py` —— 出站 text/thinking/image/file/tool block 构造；入站按 seq 拼 TEXT、
IMAGE/VIDEO/AUDIO/FILE 经 `download-file` 下载并用 base 的 `cache_*_from_bytes` 落 cache、EVENT 走系统事件；
出站媒体经 `upload-file` 得 file_id 再引用。

完成条件：文本拼接顺序正确；媒体往返经 file_id；落盘路径进入 `media_urls`/`media_types`；THINKING/TOOL 入站默认忽略。

验证方式：T-03。

### W-04 Subscribe 入站流

目标：实现唯一入站通道——subscribe NDJSON 长连接。

交付物：`stream.py` —— 逐行 `json.loads`；`result.message=null` 心跳；`result.message` 真实消息→dispatcher；
`error` 帧→重连；60s stale 计时器（任意帧 reset）；指数退避+jitter 重连；重连后对受影响会话 `after_seq` 回填。

完成条件：不使用 EventSource；半开连接被 stale 计时器检出并重连；回填覆盖断连窗口且与去重协同不重复投递。

验证方式：T-04。

### W-05 入站标准化与调度

目标：把 subscribe 消息归一为 `MessageEvent` 并调度进 gateway。

交付物：`dispatcher.py` —— `(conversation_id,seq)`/`id` 去重；系统事件（`flags&1`）状态同步；
自我过滤（`sender_id==自身`）；`sender_id`→`batch-get-identities` 解析 email/display_name（缓存）；
会话类型判定（get-conversation 缓存）；channel 准入（`TELEX_CHANNEL_ALLOWED`）；@mention 门控
（`TELEX_REQUIRE_MENTION`，标记 `telex_was_mentioned`）；`build_source` + `handle_message`。

完成条件：1:1 与 channel 的 source 字段符合 TD 表；自发消息不回流；未授权 channel/未 @bot 被 drop；用户授权交由 gateway。

验证方式：T-05。

### W-06 出站适配器

目标：实现 Telex 出站发送与目标解析。

交付物：`adapter.py` 的 `send`/`send_typing`/`send_image`/`send_image_file`/`send_document`/`get_chat_info`；
`_resolve_target`（conversation_id / `peer/<id>` / `email/<x>`→peer_id）；一次性 COMPLETED；
可选 per-conversation coalescer；超长按 `TELEX_TEXT_CHUNK_LIMIT` 分片；email 解析失败返回可读 `SendResult.error`（负向缓存）。

完成条件：文本/图片/文件/分片符合 TD；三类 target 正确路由；set-activity typing 周期重发；媒体经 file_id。

验证方式：T-06。

### W-07 cron 与 out-of-process 投递

目标：支持 cron 投递与 gateway 进程外发送。

交付物：`cron_deliver_env_var="TELEX_HOME_CHANNEL"`；`standalone_sender_fn=_telex_standalone_send`
（文本+媒体，不依赖 live adapter）；home channel 经 `env_enablement_fn` seed。

完成条件：`deliver="telex"` 路由到 `TELEX_HOME_CHANNEL`；`deliver="telex:<target>"` 覆盖；离线 cron 实际送达不报 "No live adapter"。

验证方式：T-07。

### W-08 原生流式（增强，可选）

目标：把 hermes draft 流式映射为 Telex 追加式 IN_PROGRESS→COMPLETED。

交付物：`streaming.py` —— `supports_draft_streaming()`（v2 真）；`send_draft` 维护 per-(chat_id,draft_id)
状态，累计全文帧→计算 delta→`status=1` 追加；改写 `send()` 在存在 open draft 时 `status=0` finalize 现有消息
（避免终稿重复）；segment 边界收尾上一段、起新 IN_PROGRESS；任意失败返回 `success=False` 触发降级。

完成条件：流式增量按序到达且 verbatim 拼接；不产生重复终稿消息；续写约束（仅自己、仍 IN_PROGRESS、≤1 MiB、保活）满足；失败自动回退一次性 COMPLETED。

验证方式：T-08。

### W-09 telex_query 只读工具（可选）

目标：给 agent 暴露 Telex 只读查询。

交付物：`tools.py` —— `conversation_list`/`conversation_info`/`message_history`/`get_message`/`member_list`/`identity_search`，
经 `ctx` 注册；明确不用于发送（发送走 `send_message`）。

完成条件：只读语义；错误可诊断；schema 描述清晰。

验证方式：T-09。

### W-10 配置、安装与运维文档

目标：使用者可端到端完成 bot 注册、取 key、安装、配置、验证。

交付物：README（安装/启用/配置/排查）、`env.example`、setup wizard 文案、status 展示说明；
**Bot 注册与 Key 颁发运维说明**（`TelexRegisterBot`/`TelexRotateBotKey` 流程、明文仅一次、轮换）。

完成条件：照文档可注册 bot 取得 64-hex key、`hermes plugins enable`、配置 env、重启 gateway、`hermes gateway status` 见 connected。

验证方式：T-10（文档自查 + W-12 联调佐证）。

### W-11 自动化测试集

目标：核心路径离线可验证。

交付物：`tests/unit/*`、`tests/integration/*`、mock OpenAPI / mock subscribe 流 fixture；env/registry 隔离。

完成条件：TP §3 全部 T-XX 用例可在无真实凭证下通过；CI 可离线运行。

验证方式：CI 全绿。

### W-12 真实 Telex 联调验证

目标：真实环境端到端验证（连远端 Voyager 实例，Telex 内含其中）。

交付物：`scripts/local-test.sh`（封装 `make deploy tunnel-test with-server` + `make web dev`，把远端
Voyager API tunnel 到 `127.0.0.1:8000`，并含 `register-bot`/`env`/`down` 辅助）；用其 `register-bot`
颁发 bot key；hermes gateway 配 `TELEX_BASE_URL=http://127.0.0.1:8000` 接入；1:1/channel 收发、
鉴权拒绝、媒体、回填、（v2）流式的联调记录（tr_ 报告）。

完成条件：`local-test.sh up` 一键起环境；至少一条端到端路径通过；测试 bot 用毕 `unregister-bot` 清理；问题闭环或登记。

验证方式：HITL，结果落 `docs/test/`。

### W-13 访问控制与配对

目标：复刻 openclaw-telex `access.ts` 的策略模型，并把 pairing 委托 hermes-agent 网关。

交付物：`access.py` —— `check_dm_access`(open/allowlist/pairing) + `check_group_access`(disabled/allowlist/open +
group_sender_allow_from + group_require_mention) + `is_sender_allowed`(`*`/id/email 大小写不敏感)；
`TelexAdapter.enforces_own_access_policy=True` + `_dm_policy`；dispatcher 对 dm_policy=pairing **不预过滤**（转发网关）。

完成条件：allowlist/open/group 在插件内正确拒放；`open` 缺 `*` 校验失败；pairing DM 全部转发，网关 `pairing_store` 承接握手；匹配规则与 openclaw 一致。

验证方式：T-13。

### W-14 多账号

目标：复刻 openclaw `accounts.<id>` 覆盖模型。

交付物：`accounts.py` —— 列举账号、`accounts.<id>` 覆盖顶层合并、enabled(顶层&&账号)/configured(有 api_key) 判定；
每账号独立 client/stream 并行订阅；日志带 account 维度。

完成条件：顶层+账号覆盖结果正确；多账号并行订阅互不干扰；单账号（仅顶层）退化正常。

验证方式：T-12。

### W-15 fork 与 mention 上下文回填

目标：复刻 openclaw 的 fork 继承与 mention 回填。

交付物：dispatcher 内——`fork_of_conversation_id` 存在时继承父会话 session（parentSessionKey）+ 首轮拉 fork 前历史
（limit 50，FORK_PREFIX，COMPLETED，按 seq）；require_mention channel 命中 @ 且有 gap 时回填漏消息（limit 50）作 thread 上下文。

完成条件：fork 会话正确归并父会话；mention 命中后上下文含此前漏掉的消息；两类历史注入 `supplemental.thread`。

验证方式：T-13（mention 部分）+ 集成 fork 用例。

## 5. 验证与完成标准

- 对标 openclaw-telex 全功能为验收基线：W-00..W-07、W-09、W-10、W-11、W-13、W-14、W-15 为必备；W-08 为超出对标的可选增强。
- 每个 AFK 任务以其对应 T-XX 自动化用例通过为完成标志。
- 全量完成标准：TP §3/§4 自动化用例全绿 + TD §12 对标清单逐项覆盖 + W-12 至少一条真实路径联调通过 + 文档可被独立使用者复现。
- 若 §0.1 待核对项与实现冲突，先更新 TD 再继续相关任务。
