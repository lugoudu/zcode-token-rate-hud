# token-rate-hud —— 让 ZCode 显示 token 速率的小插件

给 ZCode 装上之后，你能随时看到“AI 这轮干活到底花了多少 token、跑多快”：

- **每条回答下面多一行小字**，比如：

  ```
  15:09 · 用时 2分32秒 · 首 token 6.9秒 · 68 tok/s · ctx 155.7k · GLM-5.3
  ```

  翻译成人话：这轮回答花了 2 分半，模型“开口”等了 6.9 秒，吐字速度 68 token/秒，这轮对话已经吃掉 15.6 万 token 的“记忆容量”，用的是 GLM-5.3。往回翻历史回答，每条都会自动补上这行。

- **回答还在生成的时候**，这行字会实时跳动：

  ```
  21:25 · 进行中 42s · 首 token 6.2秒 · ~58 tok/s · ctx 254k · GLM-5.3
  ```

  带 `~` 的是实时估算值（AI 正在打字，数的是屏幕上文字的增长速度），回答结束后自动变成上面那种精确值。

- **任务窗口里**每次干完一步活，还会有一行更细的遥测（出/入速率、缓存命中、本轮累计），以及随时可查的报告和网页仪表盘。

全部数据来自你电脑上的本地文件，不联网、不上传任何东西。

## 装它（macOS）

先装插件本体（必装）：

1. ZCode → 设置 → 插件管理 → 发现 → 点 `+` → 选本仓库里的 `token-rate-hud` 的**上层目录**（即含 `marketplace.json` 的那个文件夹）
2. 列表里出现 token-rate-hud → 安装 → 重启会话

装完就有：任务窗口遥测行、`/token-rate` 报告、仪表盘。

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
| 觉得任务窗口那行遥测多余 | `/token-rate hud-off`（想恢复就 `hud-on`） |
| 不想要回答下面的统计行了 | `/token-rate ui-off` |
| 检查装没装好 | `/token-rate ui-status` |

## 两件要知道的事

1. **ZCode 升级后统计行会消失**——升级会覆盖 ZCode 的程序包，重跑一次 `/token-rate ui` 就回来了（半分钟的事）。任务窗口遥测和报告不受升级影响。
2. **那行小字有三种数字**：打字中带 `~` 的是估算（用真实数据自动校准，越用越准）；段与段之间显示的是本轮累计的精确值；回答结束后是最终精确值。想看每次调用的精确明细，用 `/token-rate`。

## 它是怎么工作的（说人话版）

ZCode 干活时，每一次“想一下”（模型调用）都会在你电脑上留两本账：一本按轮记总账（用时、首字延迟、吐了多少字），一本逐条记每次调用。这个插件就干三件事：

- 派个小助手盯着这两本账（只翻看，从不乱写），算出速率和用量；
- 通过 ZCode 的官方插件接口，把遥测行塞进任务窗口；
- 往 ZCode 的界面里放了一行小脚本，负责在每条回答下面画统计行、回答时实时刷新数字。

装 `/token-rate ui` 时会改 ZCode 的程序包（app.asar）——脚本会先自动备份原包，装坏了一键还原；ZCode 自带的完整性检查在本机已逐项确认不会拦。

## 详细技术说明

数据口径、风险分析、目录结构、排障命令见下面几个折叠块（面向想改代码或排查问题的人）。

<details>
<summary>附录 A：数据来源与统计口径</summary>

两个本地数据源，服务只读打开，不写入 ZCode 任何文件：

- `~/.zcode/cli/db/db.sqlite`（官方用量库，页脚用它）：
  - `turn_usage`：按轮聚合，含 `user_message_id`（界面 `section[data-turn-id]` 的桥）、TTFT、状态
  - `model_usage`：按次调用明细，含 `time_to_first_token_ms`
- `~/.zcode/cli/rollout/model-io-sess_*.jsonl`（模型 I/O 日志，任务窗口 HUD 用它）

| 指标 | 定义 |
|---|---|
| 用时 | 整轮墙钟 `MAX(completed_at) − MIN(started_at)`，含工具执行时段 |
| 首 token | 本轮最早发起那一步的 TTFT |
| tok/s（页脚） | `Σoutput_tokens ÷ Σ(duration_ms − ttft_ms)`——剔除首包等待的纯解码速率 |
| tok/s（实时 ~） | 渲染层每秒测界面文本字符增速 × chars/token 校准比（EMA，随调用完成自动校准，持久化于 `ui/calib.json`）；仅在流式输出时显示 |
| 模型 | 本轮用过的模型，多个用 `/` 拼接 |
| ctx | 本轮最后一次调用的上下文占用（`computed_total_tokens`） |
| HUD 出速率 | 最近一次调用 `outputTokens ÷ durationMs`（含首包等待，略低于页脚口径） |

过滤：`model_usage` 只取 `status='completed'` 且 `query_source='main_turn'`（排除标题生成等旁路调用与失败重试）。GLM 通道的 `inputTokens` 含缓存读、Anthropic 风格不含，按 `inputTokens >= cacheReadTokens` 自适应去重。

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
├── hooks/hooks.json            # SessionStart / PostToolUse / Stop
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
└── ui/server.log       # 服务日志 + 页脚脚本诊断上报
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `TOKEN_RATE_INTERVAL` | `8` | HUD 实时行最小注入间隔（秒）；`0` = 每次都注入 |
| `TOKEN_RATE_MAX_CTX` | `0` | 设为模型上限（如 128000/256000）后 HUD 显示 ctx 百分比 |
| `TOKEN_RATE_KEEP` | `128` | 状态保留的最近调用条数 |
| `TOKEN_RATE_APP` | `/Applications/ZCode.app` | 应用路径覆盖 |
| `TOKEN_RATE_IDLE_EXIT` | `21600` | 服务空闲退出秒数 |

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

- 实时估算值测的是界面可见文本：思考内容未展开显示时不计入，~ 值会偏低；真实值不受影响。
- 子代理的调用不并入主对话统计。
- 页脚数字在每轮结束后稳定；实时行绑定界面“运行中”节点，轮结束后 1～3 秒内由静态页脚接管。

## 许可

MIT
