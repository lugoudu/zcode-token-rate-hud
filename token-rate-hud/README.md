# token-rate-hud —— ZCode token 速率 HUD + 界面页脚

在 ZCode 里实时看到 token 速率，两种呈现方式，共用一个本地数据层：

| 模块 | 位置 | 形态 |
|---|---|---|
| **任务窗口 HUD** | 每次工具调用后的工具结果旁、每轮结束的总结 | 官方 hook + `additionalContext` 注入 |
| **界面页脚**（可选） | 每条回答下方一行统计（DeepSeek 风格） | 给 app.asar 注入一行渲染层脚本 |

纯本地运行，不联网、不上传任何数据。

## 效果

**界面页脚**（需安装 UI 模块，随滚动历史回答自动补齐）：

```
15:09 · 用时 2分32秒 · 首 token 6.9秒 · 68 tok/s · ctx 155.7k · GLM-5.3
```

**实时行**（页脚模块自带，任务进行中显示在当前回答下方，每秒刷新，轮结束自动转为静态页脚）：

```
21:03 · 进行中 1m59s · 首 token 7.0s · 82.8 tok/s · ctx 234.2k · 6 次调用 · GLM-5.3
```

**任务窗口 HUD**（默认启用，无需安装 UI 模块）：

```
⚡token-rate｜出 56.1 tok/s · 入 26.9 tok/s · 缓存读 137.0k｜本轮 69 次 · 出 48.7k（均 47.5）｜ctx 139.3k（token 遥测，无需回应）
🏁token-rate 本轮：16m43s · 67 次调用 · 出 46.9k tok（均 47.3 · 峰 81.3 tok/s）· 入 6.39M｜会话累计 53 次 · 纯生成 16m31s｜ctx 136.6k
```

**命令**：

- `/token-rate`：文本报告（最近调用明细、按轮汇总、会话累计、上下文占用）
- `/token-rate ui`：安装界面页脚；`ui-off` 卸载；`ui-status` 查看状态
- `/token-rate live`：本地实时仪表盘 http://127.0.0.1:7864 ，每秒刷新
- `/token-rate hud-off` / `hud-on`：开关任务窗口注入行（装好页脚后，HUD 行可关掉以省上下文）

## 数据源与口径

两个数据源都来自 ZCode 本地，服务只读打开，不写入 ZCode 任何文件：

- `~/.zcode/cli/db/db.sqlite`（官方用量库，界面页脚用它）：
  - `turn_usage`：按轮聚合，含 `user_message_id`（界面 `section[data-turn-id]` 的桥）、TTFT、状态
  - `model_usage`：按次调用明细，含 `time_to_first_token_ms`
- `~/.zcode/cli/rollout/model-io-sess_*.jsonl`（模型 I/O 日志，任务窗口 HUD 用它）

口径（界面页脚口径更专业，已吸收）：

| 指标 | 定义 |
|---|---|
| 用时 | 整轮墙钟 `MAX(completed_at) − MIN(started_at)`，含工具执行时段 |
| 首 token | 本轮最早发起那一步的 TTFT |
| tok/s | `Σoutput_tokens ÷ Σ(duration_ms − ttft_ms)`——剔除首包等待的**纯解码速率** |
| 模型 | 本轮用过的模型，多个用 `/` 拼接 |
| ctx | 本轮最后一次调用的上下文占用近似值（`computed_total_tokens`） |
| HUD 出速率 | 最近一次调用 `outputTokens ÷ durationMs`（含首包等待，故略低于页脚口径） |

过滤规则：`model_usage` 只取 `status='completed'` 且 `query_source='main_turn'`，排除标题生成等旁路调用与失败重试。

## 界面页脚：为什么需要改 app.asar

ZCode 的插件扩展点只有 skills / commands / hooks / MCP servers / agents，**渲染层没有插件 UI 接口**（`channels`、`outputStyles` 等字段在 3.11.2 里只被记录、不执行）。因此要在“每条回答下方”画东西，只能往渲染层注入脚本——本插件把这件事做成了一条命令，并且尽量做安全：

- **自动备份**：原包备份为 `app.asar.token-rate-bak`（连同 `app.asar.unpacked`）。注意：**应用升级后备份即过期**，仅作应急手动还原，卸载不走备份
- **原子替换**：用 rename 换包，运行中的 ZCode 不受影响，下次启动才生效
- **替换前自检**：新包必须通过①注入标记校验②全量解包 + 逐文件尺寸比对（27282 个文件），任一失败就放弃替换、原包不动
- **一键还原**：`/token-rate ui-off` 从**当前**包剔除注入标签后干净重打包——任何 ZCode 版本下都正确（不存在旧版备份回滚导致的版本错配风险）
- **窗口期风险**：改包会让代码签名失效（`codesign --verify` 报失败）。已逐项核实本机不受影响：
  1. ZCode.app 没有 quarantine 隔离标记（Gatekeeper 不会在启动时强校验签名）；
  2. Electron 的 `EnableEmbeddedAsarIntegrityValidation` fuse 为 **Disabled**（`Info.plist` 里虽有 `ElectronAsarIntegrity` 段，但不生效）；
  3. 全部文件替换用 rename，运行中的 ZCode 不受影响。
  可用 `npx @electron/fuses read --app /Applications/ZCode.app` 复核第 2 点。

## 安装

### 1. 插件本体

已通过本地 marketplace（`token-rate-local`）注册，插件位于 `~/.zcode/cli/plugins/cache/token-rate-local/token-rate-hud/`。
重装或换机器时：设置 → 插件管理 → 发现 → `+` → 添加本地目录 `/Users/wangnan/ZCodeProject/zcode-token-rate-market` → 安装 → 重启会话。

### 2. 界面页脚（可选）

```
/token-rate ui          # 安装（自动备份 + 注入 + 校验）
/token-rate ui-status   # 查看状态
/token-rate ui-off      # 卸载还原
```

安装后**完全退出 ZCode（Cmd+Q）再打开**生效。数据服务（127.0.0.1:7864，仅本机）由 SessionStart hook 在会话启动时自动拉起，空闲 6 小时自动退出。

**ZCode 每次升级都会覆盖 app.asar**，页脚会消失：重跑一次 `/token-rate ui` 即可恢复（脚本检测到未注入会自动重打包）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TOKEN_RATE_INTERVAL` | `8` | HUD 实时行最小注入间隔（秒）；`0` = 每次工具调用都注入 |
| `TOKEN_RATE_MAX_CTX` | `0` | HUD 里 ctx 百分比；设 128000（GLM-5.x）/200000 后显示 |
| `TOKEN_RATE_KEEP` | `128` | 状态保留的最近调用条数 |
| `TOKEN_RATE_APP` | `/Applications/ZCode.app` | 应用路径覆盖（预演测试用） |
| `TOKEN_RATE_IDLE_EXIT` | `21600` | 服务空闲退出秒数 |

页脚显示 ctx 与否在安装时决定：`ui-install --no-ctx` 可关（再跑一次 `ui` 即恢复显示）。

## 目录结构

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
├── state-sess_*.json   # 各会话增量解析状态
├── ui/inject.js        # 页脚脚本副本（端口已烘焙）
├── ui/enabled          # 页脚启用标记
├── ui/port             # 服务端口
└── ui/server.log       # 服务日志 + 页脚脚本诊断上报
```

## 排障

| 现象 | 处理 |
|---|---|
| 页脚不出现 | `ui-status` 看注入标记；重跑 `/token-rate ui`；确认已 Cmd+Q 重启 |
| 页脚出现但无数字 | 看 `~/.zcode/token-rate-hud/ui/server.log` 的 `[inject]` 诊断行（会打印界面里的 turn-id 原值） |
| ZCode 启动异常 | `python3 <插件>/lib/tokrate.py ui-uninstall` 剔除注入标签重打包；仍异常时手动换回 `app.asar.token-rate-bak`（注意与当前版本是否匹配） |
| HUD 行不出现 | `python3 <插件>/lib/tokrate.py line` 手测；确认 `~/.zcode/cli/rollout/` 有文件 |
| 数据服务没起 | `python3 <插件>/lib/tokrate.py serve --port 7864` 手动前台启动看报错 |

## 已知边界

- 页脚数字在**每轮结束后**稳定（TTFT 与解码时间按轮聚合）；**任务进行中**由实时行每秒刷新（`/live` 接口，从 `model_usage` 聚合当前轮）。已知粒度：轮内第一次模型调用完成前无实时行（数据在调用完成时才落库）。
- 实时行绑定界面“运行中”节点（`data-v4-running-live-tail`）；轮结束由 `turn_usage` 落库接管为静态页脚，衔接间隔约 1～3 秒。
- 子代理的调用不并入主对话统计（`query_source='main_turn'` 过滤）。
- 多人共用一台机器时，服务只监听 127.0.0.1，任何本机进程可读这些统计（无敏感信息）。
