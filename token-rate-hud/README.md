# token-rate-hud —— 让 ZCode 显示 token 速率的小插件

给 ZCode 装上之后，你能随时看到“AI 这轮干活到底花了多少 token、跑多快”：

- **每条回答下面多一行小字**，比如：

  ```
  15:09 · 用时 2分32秒 · 首 token 6.9秒 · Decode速度 90 tok/s · 端到端 45 tok/s · GLM-5.3
  ```

  翻译成人话：这轮回答花了 2 分半，模型“开口”等了 6.9 秒；Decode速度 90 是按各次调用扣除首输出等待后的时长加权平均（只反映首输出之后的出 token 阶段），端到端 45 是整轮墙钟口径（把等待首包、执行工具的时间全算进分母，整轮平均每秒产出 45 个 token）。无效调用会从 Decode 的分子和分母中同时剔除；有效样本覆盖与剔除原因保留在 `/turns` 数据中，供统计与排障使用。往回翻历史回答，每条都会自动补上这行。

- **回答还在生成的时候**，这行字会实时跳动：

  ```
  21:25 · 进行中 42s · 首 token 6.2秒 · ~58 tok/s · GLM-5.3
  ```

  带 `~` 的是流式估算值（AI 正在打字，按本地用量库里该条回答文本的增长速度换算），回答结束后换成基于完整用量与时间口径的最终统计。

- **随时可查的报告和网页仪表盘**：输入 `/token-rate` 看详细账单（每次调用、每轮汇总），`/token-rate live` 打开每秒刷新的网页仪表盘。

统计与展示所需数据全部来自本机文件，运行过程不上传任何东西。**插件也绝不往 AI 的对话上下文里写任何东西**——所有展示都在界面层完成，对模型零干扰。安装或卸载页脚时，`npx` 可能需要联网获取 `@electron/asar` 打包工具。

## 装它（macOS）

先装插件本体（必装）：

1. ZCode → 设置 → 插件管理 → 发现 → 点 `+` → 选本仓库里的 `token-rate-hud` 的**上层目录**（即含 `marketplace.json` 的那个文件夹）
2. 列表里出现 token-rate-hud → 安装 → 重启会话

装完就有：`/token-rate` 报告和网页仪表盘。

再装回答下面的统计行（可选，只支持 macOS 桌面版）：

```
/token-rate ui
```

然后**完全退出 ZCode（Cmd+Q）再打开**，就能看到每条回答下面的统计行了。

## 日常怎么用

| 你想干什么 | 就这么做 |
|---|---|
| 看这轮花了多少 token、跑多快 | 直接看回答下面那行小字 |
| 查详细账单（每次调用、每轮汇总） | 输入 `/token-rate` |
| 打开网页仪表盘（每秒刷新的大数字） | 输入 `/token-rate live`，浏览器开 http://127.0.0.1:7864 |
| 不想要回答下面的统计行了 | `/token-rate ui-off` |
| 检查装没装好 | `/token-rate ui-status` |

## 两件要知道的事

1. **ZCode 升级后统计行会消失**——升级会覆盖 ZCode 的程序包，重跑一次 `/token-rate ui` 就回来了（半分钟的事）。任务窗口遥测和报告不受升级影响。
2. **那行小字有几种数字**：打字中带 `~` 的是流式估算（字符增速按校准比换算，随真实用量自动校准）；「已完成均值」是本轮已完成调用的 Decode 均值；回答结束后的 Decode速度 / 端到端来自完整用量与时间口径的统计。想看每次调用的明细，用 `/token-rate`。

## 它是怎么工作的（说人话版）

ZCode 干活时，每一次“想一下”（模型调用）都会在你电脑上留两本账：一本按轮记总账（用时、首字延迟、吐了多少字），一本逐条记每次调用。这个插件就干三件事：

- 派个小助手盯着这两本账（只翻看，从不乱写），算出速率和用量；
- 由本地数据服务向页脚、报告和仪表盘提供统计结果；
- 通过可选的 `app.asar` 注入加载页脚脚本，在每条回答下面画统计行，并在回答时实时刷新数字。

装 `/token-rate ui` 时会改 ZCode 的程序包（app.asar）——脚本会先自动备份原包，装坏了一键还原；ZCode 自带的完整性检查在本机已逐项确认不会拦。

## 详细技术说明

数据口径、风险分析、目录结构、排障命令见下面几个折叠块（面向想改代码或排查问题的人）。

<details>
<summary>附录 A：数据来源与统计口径</summary>

两个本地数据源，服务只读打开，不写入 ZCode 任何文件：

- `~/.zcode/cli/db/db.sqlite`（官方用量库，页脚用它）：
  - `turn_usage`：按轮聚合，含 `user_message_id`（界面 `section[data-turn-id]` 的桥）、TTFT、状态
  - `model_usage`：按次调用明细，含 `time_to_first_token_ms`
- `~/.zcode/cli/rollout/model-io-sess_*.jsonl`（模型 I/O 日志，`/token-rate` 报告用它）

| 指标 | 定义 |
|---|---|
| 用时 | 整轮墙钟 `MAX(completed_at) − MIN(started_at)`，含工具执行时段 |
| 首 token | 本轮首输出延迟＝首个落 token 时刻 − 轮起点（轮起点取最新 user 消息时刻，与实时行同口径，轮结束瞬间数字不跳变） |
| 端到端 tok/s（页脚） | `Σoutput_tokens ÷ run_ms`（整轮墙钟），**固定显示完整墙钟值**。分子取轮账本（`turn_usage.output_tokens`），轮账本为 NULL 才回退主调用账本（完成 `main_turn` 调用 Σoutput），0 是合法值不回退；两账本不一致（真实库约 7% 的轮）以 `output_ledger_diff` 暴露（正数＝轮账本多出，通常来自旁路/未落明细行的消耗） |
| 剔等待（另列段） | `wait_ms` ＝ 轮内时间线上工具**开始执行前**的空闲段合计（单段 ≥2s 才计，滤除毫秒级调度噪声）。工具执行区间优先取 `part.state.time.start/end`（用户确认后的真实执行段）；`tool_usage.started_at` 是请求登记时刻（早于用户确认），仅在 part 缺失时按 `completed_at − duration_ms` 回推执行起点。剔等待值（`tps_e2e_active`）仅对「有主调用完成且空档无推断成分」的轮产出——part 区间缺失走回推、存在未知执行区间或轮未闭合时 `wait_inferred=true`，低置信不产出；页脚把它另列成段（「剔等待Xs N tok/s」，标签与数字一体不被截断），端到端主值永远是完整墙钟口径。失败/取消/子代理调用计入忙碌时间线不被误扣；AskUserQuestion 的用户思考时间在工具时长内、不剔除 |
| Decode速度 | `Σout ÷ Σ(duration − ttft)` 的调用加权均值（宿主时间代理口径，非服务端纯解码速率；混合模型轮为混合均值），分子分母同有效样本条件（ttft 齐备且 ≥0、`duration > ttft`、输出 >0），无效调用两侧同剔。样本不齐时页脚并排显示「有效样本 N/M」，悬停统计行可见剔除原因（缺/负 TTFT、零时长、零输出、未完成；原因可重叠，一次调用可同时命中多个） |
| tok/s（实时 ~） | 优先用服务端按消息统计的字符增量：逐 assistant 消息采样 text+reasoning 字符合计，**除以** chars/token 校准比换算（EMA 随调用完成自动校准，持久化于 `ui/calib.json`，v2 起按模型分桶、带有效样本数）；服务端瞬时值不可用时回退界面字符测算（剔除插件自插行与工具块后的文本增量，仅作估算） |
| 模型 | 本轮用过的模型，多个用 `/` 拼接 |
| 出速率（报告） | 最近一次调用 `outputTokens ÷ durationMs`（含首包等待，不含工具时段） |

过滤：`model_usage` 只取 `status='completed'` 且 `query_source='main_turn'`（排除标题生成等旁路调用与失败重试）。GLM 通道的 `inputTokens` 含缓存读、Anthropic 风格不含，按 `inputTokens >= cacheReadTokens` 自适应去重。

<details>
<summary>附录 A-2：宿主字段实证记录（2026-10-19，样本 = 最近 60~300 条 completed main_turn）</summary>

对 `db.sqlite` 的 `model_usage` 与 `part` 表交叉验证，四条结论：

| 问题 | 结论 | 证据 |
|---|---|---|
| `duration_ms` 的端点 | ≡ `completed_at − started_at`（毫秒一致）；`completed_at` 为流+usage 完成时刻，末次 part 写入**早于**它约 130ms（P50 −130ms） | `duration_ms` 与两时间戳之差 60/60 条为 0；末次 part 写入 − `completed_at` P50 = −130ms |
| TTFT 判定规则 | 计时起点 = 首个内容增量（思考流即启动），与 DeepSeek Harness 的 `isTokenDelta`（非空 text/reasoning/tool-call delta）同族 | 思考字符量 0~500 / 500~3000 / 3000~10000 字的 ttft P50 = 9.2s / 9.0s / 8.9s，平坦无相关（r=+0.14）；若量到首正文，深思考调用 ttft 应随思考量增长 |
| GLM 通道 usage 细分 | `reasoning_tokens` 填报率 0/1343（细分缺失）——分子 = completion 总量（已含思考），与 DSH 对 OpenAI 系口径一致；Kimi-K3 通道有细分 | rollout 中 `reasoningText` 非空但库内 `reasoning_tokens=0` |
| part 表时间戳语义 | part 行是流初始化时批量创建的占位行（text/reasoning 同刻创建，早于首 token 约 8s）；`time_created` 不能当首内容信号，字符估算依赖内容增量不受影响 | 首 text part − 首 reasoning part = 0；`first_token_at` − 最早 part 创建 P50 = 8.2s |

与 DSH 口径的唯一系统性差异：DSH 的解码期量到「消息落盘」，本插件量到 `completed_at`——分母少约 0.1~0.2s 收尾，短调用下 Decode 略偏高几个百分点，长调用可忽略。rollout jsonl 含完整请求/响应头与 `reasoningText`，是后续逐请求记录（M2 后续）的现成数据源。

</details>

进行中的轮没有 `turn_usage` 行，实时数据从 `model_usage` 聚合当前轮；提问瞬间（`message` 表）即进入“进行中”显示（首步调用完成前显示“首步生成中…”）。

</details>

<details>
<summary>附录 B：安装/卸载 app.asar 注入的风险与保障</summary>

- 自动备份：原包备份为 `app.asar.token-rate-bak`（连同 `.unpacked`）。**应用升级后备份即过期**，仅作应急手动还原，卸载不走备份
- 原子替换：rename 换包，运行中的 ZCode 不受影响
- 替换前自检：①注入标记校验 ②全量解包 + 逐文件尺寸比对，任一失败即放弃替换
- 一键还原：`ui-uninstall` 从当前包剔除注入标签后干净重打包（任何 ZCode 版本下都正确）
- 已逐项核实本机不受签名失效影响：无 quarantine 标记；Electron `EnableEmbeddedAsarIntegrityValidation` fuse 为 Disabled；全部替换用 rename。可用 `npx @electron/fuses read --app /Applications/ZCode.app` 复核

</details>

<details>
<summary>附录 C：目录结构、配置与环境变量</summary>

```
token-rate-hud/
├── .zcode-plugin/plugin.json   # 插件清单
├── hooks/hooks.json            # SessionStart（仅运维：重置状态、拉起数据服务；不向模型输出任何内容）
├── hooks/token_rate_hook.py    # hook 入口（任何异常静默，绝不干扰会话）
├── lib/tokrate.py              # 核心库 + CLI（hook/报告/服务/UI 安装）
├── lib/usage_db.py             # SQLite 用量库只读折叠层
├── ui/inject.js                # 渲染层页脚脚本（安装时烘焙端口）
└── commands/token-rate.md      # /token-rate 命令
```

运行时状态（与源码解耦，插件升级不受影响）：

```
~/.zcode/token-rate-hud/
├── state-sess_*.json   # 各会话增量解析状态（14 天自动清理）
├── ui/inject.js        # 页脚脚本副本（端口已烘焙）
├── ui/enabled          # 页脚启用标记
├── ui/port             # 服务端口
├── ui/calib.json       # chars/token 校准比
├── ui/lib/             # launchd 常驻运行的服务代码副本（跨插件版本稳定）
└── ui/server.log       # 服务日志 + 页脚脚本诊断上报
```

数据服务自 v1.5.2 起由 launchd 常驻（`~/Library/LaunchAgents/com.zcode.token-rate-hud.plist`，
KeepAlive 保活 + 开机自启），`ui-install` 安装、`ui-uninstall` 卸载；`ui-status` 可查状态。
此前「空闲 6h 自杀 + SessionStart 钩子拉起」的设计因钩子触发不可靠已被取代。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TOKEN_RATE_KEEP` | `128` | 状态保留的最近调用条数（长轮完整性由按轮独立累计量保证，不受此窗口截断） |
| `TOKEN_RATE_APP` | `/Applications/ZCode.app` | 应用路径覆盖 |
| `TOKEN_RATE_IDLE_EXIT` | `21600` | 服务空闲退出秒数（launchd 常驻进程固定为不退出） |

（`TOKEN_RATE_INTERVAL` 已随 HUD 行上下文注入的下线一并移除：本插件 hook 全
路径承诺零上下文注入，PostToolUse/Stop 注入不再存在。）

</details>

<details>
<summary>附录 D：排障</summary>

| 现象 | 处理 |
|---|---|
| 页脚不出现 | `ui-status` 看注入标记；重跑 `/token-rate ui`；确认已 Cmd+Q 重启 |
| 页脚出现但无数字 | 看 `~/.zcode/token-rate-hud/ui/server.log` 的 `[inject]` 诊断行 |
| ZCode 启动异常 | `python3 <插件>/lib/tokrate.py ui-uninstall`；仍异常时手动换回备份（注意版本是否匹配） |
| HUD 行不出现 | `python3 <插件>/lib/tokrate.py line` 手测；确认 rollout 目录有文件 |
| 数据服务没起 | `python3 <插件>/lib/tokrate.py serve --port 7864` 前台启动看报错 |

</details>

## 已知边界

- 实时估算值优先来自本地库中该条回答 text+reasoning 字符的增长（思考内容折叠与否不影响计数）；
  仅在服务端瞬时值不可用时回退界面可见文本测算，此时思考未展开会使 ~ 值偏低。真实值不受影响。
- **工作流子代理**（v1.6.0 起纳入，v1.6.1 起按会话隔离，v1.6.2 起显示总消耗）：
  `model_usage.query_source='workflow_child'` 的调用经
  `dwf_run(parent_session_id) → dwf_actor(名字/子会话)` 关联——运行中由 `workflow_live()`
  按代理聚合进 `/live?cid=` 的 workflow 块（页脚 ⟪ 工作流 ⟫ 汇总段 + 第二行逐代理明细）；
  轮次结束后按「运行创建时刻落在哪个主轮时间窗」把用量归并进 `wf_*` 字段（合计展示，
  不并入主代理 tps，避免并行失真）。**消耗口径**：`✓` 后与 `wf_total_tokens` 均为
  Σcomputed_total_tokens（每调用入+出，GLM 通道 input 已含缓存读）——编码类子代理
  输出仅占 ~1%，只看输出会严重低估；该公式与 `dwf_run.spent_tokens` 逐分对齐（实测
  23,291,706 全等）。渲染层把本窗口 DOM 的 `data-turn-id`（本轮 user 消息 id）作为
  `cid` 上报，服务端 `session_of_message()` 解析成会话后**整套实时数据（实时行 +
  工作流块）都限定在该会话内**——多窗口/后台自动化工作流并存时不串台；cid 解析失败时
  宁可不显示工作流。主轮结束后仍在后台跑的工作流，明细行挂到最新一轮区块下继续刷新。
  `live_turn` 的 phase-0 检测排除 `sess_dwf*` / `sess_subagent*` 子会话消息（否则实时行
  计时被劫持），主代理超 20 分钟无调用时只要该会话有 running 的工作流仍视为进行中；
  首次调用进行中（尚无完成行）出骨架行显示「首步生成中」。
  运行汇总段区分两种口径：`~X tok/s` 为并行瞬时合计（各活跃子代理瞬时值相加），
  `均 X tok/s` 为均值口径（无瞬时值时依次回退 run 均值、调用均值），两者勿混读。
- **普通 Agent 工具子代理**（`query_source='subagent'`，本版本起纳入）：由 `subagent_live()`
  按「子会话 × 父轮」批次聚合（同一 agent 跨轮续跑会出多行）。归属三级，界面明示：
  父会话 Agent 工具 part 输出尾注 `agentId` 直接命中 → `agent_ref`（直接归属，常态不标注）；
  退化按 Agent 工具执行窗口推断 → `inferred`（名字后标「(推断)」）；窗口多义或父会话
  缺失 → `undetermined`（标「(未定)」，用量照出、归属缺位不冒充）。显示与工作流一致：
  运行中批次带 `~` 瞬时速率（无瞬时值时「均」调用均值），完成批次 `✓` 后为总消耗
  （Σcomputed_total_tokens，口径与工作流相同）；实时行有并行瞬时合计时另列
  「⟪ 子代理 ⟫ ~X tok/s」汇总段。
- 页脚数字在每轮结束后稳定；实时行绑定界面“运行中”节点，轮结束后 1～3 秒内由静态页脚接管。
  后台工作流的残余归并、晚到用量与账本校正值都能回填刷新：页脚静态行按数据版本比对，
  展示内容有变化才重写 DOM（数据没变时零 DOM 写）。

## 许可

MIT
