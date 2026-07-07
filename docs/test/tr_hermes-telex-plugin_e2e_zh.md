---
标题: Hermes Telex Plugin 真实联调测试报告
状态: draft
更新日期: 2026-07-03
参考材料:
  - Hermes Telex Plugin 测试计划 (./tp_hermes-telex-plugin_zh.md)
  - Hermes Telex Plugin 真实联调 Runbook (./e2e_hermes-telex_runbook_zh.md)
  - Hermes Telex Plugin 工作分解结构 (../spec/wbs_hermes-telex-plugin_zh.md)
文档摘要: 记录 hermes-telex 真实联调（W-12）的执行结果、证据摘要、缺陷及修复情况、待执行项。
---

# TR: Hermes Telex Plugin 真实联调测试报告

## 1. 当前结论

核心链路已在真实环境端到端打通：插件经一行命令从 GitHub 安装、连接远端 Voyager 实例的 Telex、
1:1 会话收发正常、telex 只读工具调用正常、home channel 设置与投递生效。联调过程中发现 6 个插件缺陷，
全部修复并发布（publish `v2026.7.3` @ `53ba4b1`），修复后由测试者复测通过。

2026-07-02 ~ 07-03 又以 voyager 仓库 bot 套件（`voyager/test/bot`，46 用例）对同一实例做了全量真实回归，
覆盖文本收发、echo 抑制、6 个只读工具、fork、typing 指示、全部配置门控（mention / 群策略 / 白名单 /
dmPolicy）与媒体收发。全部插件路径用例通过；唯一残留失败是 3 个"读图内容"用例在主模型为 `gpt-5.5`
时失败（模型视觉能力问题，非插件缺陷，见 §8；同图 claude 主模型下通过）。E-03 / E-04 / E-06 据此闭环。

剩余用例（断连回填、pairing、多账号、key 轮换）待后续执行，见 §7。

本报告只记录结果，不重复执行步骤。操作步骤见 `e2e_hermes-telex_runbook_zh.md`。

## 2. 总体状态

| 类别 | 数量 | 结果 |
| --- | ---: | --- |
| 自动化准备项 | 3 | PASS |
| 人工环境准备项 | 6 | PASS |
| 真实 E2E 用例（已执行） | 9 | PASS（其中 2 项为缺陷修复后复测通过；E-04 带模型注记） |
| 真实 E2E 用例（待执行） | 4 | PENDING（E-05 部分覆盖、E-07、E-08、E-10） |
| bot 套件全量回归（46 用例） | 46 | 插件路径全部 PASS；3 个读图用例仅在 claude 主模型下 PASS（§6a/§8） |
| 发现缺陷 | 6 | 全部 FIXED |
| BLOCKED 项 | 0 | 无 |

## 3. 测试环境摘要

| 项目 | 记录 | 结果 |
| --- | --- | --- |
| 测试日期 | 2026-07-02 ~ 2026-07-03 | - |
| Voyager 实例 | voyager-test-0（远端测试服务器） | 运行中 |
| hermes-agent 实例 | 测试服务器 Incus VM `vm-38e2206954dee62f`，用户 `hermes`，安装于 `~/.hermes/hermes-agent`（venv Python 3.11） | 运行中 |
| Plugin 安装 | `~/.hermes/plugins/telex-platform`，自 GitHub `arxeme/hermes-telex` publish 分支（`v2026.7.3` @ `53ba4b1`） | 已启用 |
| 运行时配置 | VM `~/.hermes/config.yaml` → `platforms.telex.extra.accounts.default`（`base_url=http://192.168.100.1:8000`、`dm_policy=allowlist`、`group_policy=open`、`group_require_mention=false`） | 生效 |
| 测试者客户端 | 本机 `scripts/local-test.sh`（chisel tunnel `127.0.0.1:8000` + web `localhost:3000`），真实账号登录 | 可用 |
| 测试 bot | display name "Xiaoyu TT"，`bot_id=38e2206954dee62f`；home channel `b7cbc72a481784b0` | 在线 |
| 测试会话 | "Hermes-telex bot testing" | - |

## 4. 自动化准备项结果

| 项目 | 结果 | 证据摘要 |
| --- | --- | --- |
| 本地 pytest 回归 | PASS | `42 passed`（tests/unit，全离线 mock，含本轮缺陷的回归用例） |
| register(ctx) 导入校验 | PASS | 部署脚本内 `register(ctx) OK`；安装后网关无 "No adapter available for telex" |
| 发布链路 | PASS | publish 分支 fast-forward 发布，tag `v2026.7.3`；`git pull` 升级路径实测可用 |

## 5. 人工环境准备项结果

| 项目 | 结果 | 证据摘要 |
| --- | --- | --- |
| 测试者客户端起环境 | PASS | `local-test.sh up` 后 `:8000`/`:3000` 就绪，web HTTP 200（修复 `exec cd` 启动缺陷后） |
| bot 注册 / key 颁发 | PASS | 经 register-bot 颁发；key 存于 VM config.yaml，未入库/未入 repo |
| 一行命令安装 | PASS | `hermes plugins install arxeme/hermes-telex --enable && hermes gateway restart` 全程无交互中断，exit 0（修复 `requires_env` 提问后） |
| plugin enable + 网关重启 | PASS | `telex-platform enabled`；`hermes-gateway.service` active |
| subscribe 建连 | PASS | 网关进程到 `192.168.100.1:8000` 的 ESTAB 长连接（多次重启后均重建） |
| `git pull` 升级 | PASS | 插件目录 fast-forward 到 `53ba4b1` → 重启 → `✓ telex connected` |

## 6. 真实 E2E 结果（已执行）

| 用例 | 验证目标 | 结果 | 证据摘要 |
| --- | --- | --- | --- |
| E-00 起环境 | tunnel + web 就绪、真实账号可登录 | PASS | `local-test.sh status` 两端口 open；浏览器登录成功 |
| E-01 取 key + connected | bot key 生效、Telex connected | PASS | subscribe ESTAB；gateway.log `✓ telex connected` |
| E-02 1:1 收发 | 私聊触发 agent 回复、自发不回流 | PASS | "Hermes-telex bot testing" 会话中 hello → agent 回复；无自激循环 |
| E-02a home channel | `/sethome` 设置并被 send_message/通知使用 | PASS（修复后复测） | `/sethome` 写入 `TELEX_HOME_CHANNEL=b7cbc72a481784b0`；修复 env_enablement 后 `get_home_channel` 解析正确，gateway.log 显示启动/关闭通知送达 `telex:b7cbc72a481784b0` |
| E-02b telex 工具 | agent 调用 telex 只读工具 | PASS（修复后复测） | 首测报 `task_id` TypeError 与 aiohttp loop 错误；修复后测试者复测"工具调用正常" |
| E-11 一行安装（新增用例） | README 安装命令顺畅无中断 | PASS | 移除旧插件后原样执行，无 env 提问，安装+enable+重启一次完成 |
| E-03 channel @mention 门控 | `require_mention=true` 未 @ 不触发；@ / @all 触发；关闭后无需 @ | PASS | bot 套件 CFG-A1~A3（两轮串行全过）+ CFG-B1；经 Mate API 切换配置并等待 reconcile 后验证 |
| E-04 媒体收发确证 | 入站各块型送达 agent；出站自生成 / 回传附件送达 | PASS（带模型注记） | 入站 file/pdf/video/audio 双面通过（BOT-MED）；出站自生成 file/image/mixed 与回传 file/image 双面通过（BOT-SEND，DM 回传亦通过——openclaw 的 `media://` 已知缺陷未在 hermes 复现）；入站**图片内容读取**仅 claude 主模型通过，`gpt-5.5` 失败（§8，非插件问题） |
| E-06 鉴权拒绝 | 非 `allow_from` 账号私聊无响应；群发送者白名单拒绝 | PASS | CFG-A5、CFG-F2（第二真实账号 driver2 实测无响应）；反向 `dm_policy=open` 由 CFG-G1 覆盖（07-02 首日两次超时，当晚复跑通过，未再复现） |

### 6a. bot 套件全量回归证据（2026-07-02 ~ 07-03）

执行方式：`voyager/test` 的 live bot 套件（`pnpm test:bot`，`playwright.bot.config.ts`，46 用例，
驱动账号=实例 owner + 第二账号，目标即本实例 `38e2206954dee62f`）。执行轨迹：

1. 07-02 全量首跑：38/46 通过（1 flaky），7 个确认失败。
2. 修复 D-3（telex 工具 `task_id` 崩溃，见 §8）后：tools 6/6 通过（BOT-T1~T6，含首跑失败的 list_members）。
3. VM 安装 ImageMagick（镜像缺依赖，见 §8）后：config 13/13（含 CFG-G1）、channel 自生成图片通过。
4. 残留 3 个读图用例（DM/channel `test-image.png`、mixed）：claude 主模型下 3/3 通过；
   `gpt-5.5` 主模型下复现失败并完成根因定位（§8）。
5. typing 指示（BOT-TYP1）、fork（BOT-FORK）、基础收发（BOT-A1/A2/ECHO）均通过；
   tools 6/6 亦对 E-02b 做了自动化复证。

## 7. 真实 E2E 待执行项

| 用例 | 验证目标 | 状态 | 备注 |
| --- | --- | --- | --- |
| E-05 断连回填 | 断窗消息 `after_seq` 回填不丢不重 | PENDING（部分覆盖） | mention 间隙回填已由 CFG-A6 验证（gap 内消息在下次 @ 时全部召回）；真实**断连窗口**回填仍未构造验证 |
| E-07 pairing | 未批准账号收到配对码、批准后可对话 | PENDING | 需切换 `dm_policy=pairing`；bot 套件 config 用例不含 pairing |
| E-08 多账号 / E-10 key 轮换 | accounts 覆盖、rotate 生效 | PENDING | 需第二个 bot / 轮换窗口 |

## 8. 缺陷记录（本轮发现，全部已修复并发布）

| # | 缺陷 | 现象 | 根因 | 修复 |
| --- | --- | --- | --- | --- |
| D-1 | check_fn 误判未配置 | 网关 "requirements not met / adapter creation failed"，adapter 不创建 | `check_telex_requirements()` 硬性要求 env `TELEX_API_KEY`，而 key 在 config.yaml | check_fn 只做依赖检查；配置校验交给 validate_config/is_connected 读 extra |
| D-2 | `accounts.default` 被忽略 | Voyager 现网 config 格式读不到配置 | `resolve_account` 对 `default` 特判为只读顶层 | 任一 id（含 default）均以 `accounts.<id>` 覆盖顶层 |
| D-3 | telex 工具崩溃 | `unexpected keyword argument 'task_id'` | handler 未接受注册表传入的 `**kwargs` | `telex_tool_handler(args, **_kwargs)` |
| D-4 | `/sethome` 对 send_message 无效 | "No home channel set for telex" | `env_enablement_fn` 在 env 无 api_key 时返回 None，home_channel 未被 seed | home_channel 不再以 api_key 为前提；返回扁平字段（对齐 IRC） |
| D-5 | telex 工具 aiohttp 报错 | "Timeout context manager should be used inside a task" | 全局 ClientSession 绑死网关 loop，工具经 `_run_async` 在新 loop 调用 | session 按当前 running loop 重建（含回归测试） |
| D-6 | 一行安装被打断 | 安装时交互式索要 `TELEX_API_KEY` | plugin.yaml `requires_env` 触发安装器逐项提问 | `requires_env: []`（对齐 hermes-seatalk），key 移入 `optional_env` |

辅助工具缺陷（非插件运行时）：`local-test.sh` web 启动失败（`exec cd` 吞掉命令）、
deploy 脚本 `set -u` 空参数数组报错，均已修复。

与插件无关、不计入本报告的失败：agent 执行环境缺 `PIL`/`fitz`/`file`、`execute_code` 等待用户确认超时、
`skill_manage` YAML 生成错误 —— 属 hermes-agent 通用环境问题。

bot 套件全量回归另发现两项**非插件**问题（供环境/上游跟进）：

1. **mate VM 镜像无任何图像生成工具**：ImageMagick / PIL / node-canvas 均缺失，"自生成图片附件"
   用例必然失败。本轮已在 `vm-38e2206954dee62f` 手工 `apt install imagemagick` 后通过；镜像层面未固化。
2. **`gpt-5.5` 主模型读图既慢又错（点阵字体图）**：套件资产 `test-image.png`（5×7 bitmap 字体、
   1104×176 灰度）在 claude 主模型下秒级读出正确码；`gpt-5.5` 经字节级全保真重放耗时 31~74s 且两次
   均读错码，生产内常超过 `auxiliary.vision.timeout=120s` 直接超时，随后 agent 以臆造文本回复。
   已逐项实测排除 resolver / vision 凭据 hotfix / Codex 适配器 / 网关并发与可达性 / 代理环境 /
   空闲连接复用等全部基础设施因素（含挂死现场 py-spy 栈与 socket 采样：请求送达、流已开、模型侧
   长时间无事件）。定性为 hermes-agent × 模型视觉能力问题；平滑抗锯齿字体的同码图片 `gpt-5.5`
   1~7s 即可正确读出。

## 9. 后续项

1. 按 §7 执行剩余 E2E（建议顺序：E-05 → E-07 → E-08/E-10），结果追加至本报告。
2. E-07 需要临时修改配置（`dm_policy=pairing`），测毕还原。
3. 可选增强 W-08（原生流式）未实现，对应 E-09 不适用。
4. 待决策项（§8 非插件问题）：ImageMagick 是否固化进 mate VM 镜像；`gpt-5.5` 实例的读图用例
   是记为已知限制，还是将 `auxiliary.vision` 显式指向 vision 更强的模型（与主模型解耦）。
