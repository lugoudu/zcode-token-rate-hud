# token-rate-hud

> 在 ZCode 中查看每轮回答的 token 用量、首 token 延迟和生成速率。支持回答页脚、实时仪表盘、文本报告，以及工作流与普通子代理统计。

token-rate-hud 是一个面向 **macOS 桌面版 ZCode** 的本地插件。统计数据来自本机用量日志和 SQLite 数据库；统计与展示过程不上传数据，也不会向模型对话上下文注入内容。

## 功能一览

- **回答页脚**：在每轮回答下方显示用时、首 token 延迟、Decode 速度、端到端速率和模型；历史回答也会自动补齐。
- **实时统计**：回答生成时每秒刷新；带 `~` 的数值是流式估算，完成后切换为最终统计。
- **子代理统计**：展示动态工作流和 Agent 工具子代理的瞬时速率、调用均值与累计消耗。
- **文本报告**：通过 `/token-rate` 查看最近调用、按轮汇总和会话累计用量。
- **实时仪表盘**：通过 `/token-rate live` 启动本地服务，在浏览器中查看当前速率、本轮均值与峰值、会话累计和最近 64 次调用柱状图。

页脚示例：

```text
15:09 · 用时 2分32秒 · 首 token 6.9秒 · Decode速度 90 tok/s · 端到端 45 tok/s · GLM-5.3
```

生成中的实时行示例：

```text
21:25 · 进行中 42秒 · 首 token 6.2秒 · ~58 tok/s · GLM-5.3
```

工作流运行时，还会显示汇总与逐代理明细：

```text
21:25 · 进行中 4分12秒 · ~22 tok/s · GLM-5.3 · ⟪ 工作流 2/5 ⟫ ~120 tok/s
文献检索-1 ~52 · 文献检索-2 ~48 · 选题策划 ✓1.20M
```

其中，`~` 表示瞬时速率，`均` 表示调用均值，`✓` 后是累计总消耗（输入＋输出，缓存读计入输入侧）。子代理速率不会并入主代理速率，以免并行执行时产生误导。

## 安装

### 环境要求

- macOS 桌面版 ZCode。
- Python 3。
- 当前 ZCode 版本需要写入插件所需的本地用量数据；如果命令提示找不到数据源，插件无法凭空补齐统计。
- 仅安装报告和仪表盘不需要修改应用包；可选页脚的安装与卸载还需要 Node.js 自带的 `npx`，并可能通过它获取 `@electron/asar`。

### 第一步：安装插件本体

1. 将仓库克隆到本地：

   ```bash
   git clone https://github.com/lugoudu/zcode-token-rate-hud.git
   ```

2. 打开 ZCode，进入“设置 → 插件管理 → 发现”，点击 `+`，选择**仓库根目录**，也就是包含 `marketplace.json` 的文件夹。
3. 在列表中找到 `token-rate-hud`，安装后重启会话。

此时已经可以使用 `/token-rate` 报告和 `/token-rate live` 仪表盘。回答页脚需要单独启用。

### 第二步：启用回答页脚（可选）

> [!WARNING]
> ZCode 渲染层没有插件 UI 接口，因此页脚功能需要修改 `/Applications/ZCode.app/Contents/Resources/app.asar`，并安装一个由 launchd 管理的本地常驻服务。只需要报告和仪表盘时，不必执行这一步。

在 ZCode 中运行：

```text
/token-rate ui
```

安装器会自动备份原始包、注入页脚脚本、重打包并校验内容，然后安装本地数据服务。完成后，**完全退出 ZCode（Cmd+Q）再重新打开**，页脚才会出现。

> [!NOTE]
> 如果 ZCode 安装在非默认位置，可以通过 `TOKEN_RATE_APP` 环境变量指定应用包路径。

## 常用命令

| 命令 | 作用 |
|---|---|
| `/token-rate` | 查看当前活跃会话的文本报告，包括最近调用、按轮汇总和会话累计 |
| `/token-rate live` | 启动本地仪表盘服务；随后访问 [http://127.0.0.1:7864](http://127.0.0.1:7864) |
| `/token-rate ui` | 安装页脚；ZCode 升级后也可用此命令重新安装 |
| `/token-rate ui-status` | 检查应用包、注入标记、备份、用量库、数据服务和 launchd 状态 |
| `/token-rate ui-off` | 移除页脚注入及 launchd 常驻服务；完全退出并重开 ZCode 后生效 |

单独启动的仪表盘服务可用以下命令停止：

```bash
pkill -f "tokrate.py serve"
```

如果页脚已经安装了 launchd 常驻服务，进程会被自动重新拉起；需要彻底停止并移除该服务时，请使用 `/token-rate ui-off`。

## 指标怎么读

| 展示项 | 含义 |
|---|---|
| 用时 | 整轮墙钟时间，包含首包等待、模型调用和工具执行 |
| 首 token | 从本轮开始到首次输出内容的等待时间，即 TTFT |
| `Decode速度` | 首输出之后各次主调用输出速率的汇总口径：总输出 token ÷ 扣除 TTFT 后的有效时长；属于宿主侧测量，不等同于服务商的纯解码基准 |
| `端到端` | 本轮输出 token ÷ 整轮墙钟时间；首包等待和工具执行都计入分母 |
| `剔等待` | 可选参考值：剔除工具开始执行前的空档后计算；仅在时间线完整、无需推断时显示，不会替换端到端主值 |
| `~` 实时速率 | 根据回答文本与思考内容的字符增量估算，并随已完成调用自动校准；最终统计不依赖该估算 |
| `已完成均值` | 无法得到瞬时值时，显示本轮已完成调用的 Decode 均值 |
| 子代理 `✓` 数值 | 累计总消耗，而不是仅统计输出 token；工作流结束后也会在页脚显示合计 |

> [!IMPORTANT]
> `/token-rate` 报告中的调用速率**包含首包等待**，与页脚的 `Decode速度` 口径不同，不能直接混读。Decode 样本不完整时，无效调用会同时从分子和分母中剔除；样本覆盖与剔除原因保留在 `/turns` 数据中。

普通 Agent 子代理的名称后可能出现归属标注：“推断”表示按执行时间窗匹配父轮，“未定”表示无法可靠确定父轮。后者仍会统计用量，但不会伪造归属关系。

## 隐私与工作原理

插件使用两类本地数据源：

- `~/.zcode/cli/db/db.sqlite`：以只读方式读取 `turn_usage`、`model_usage` 等用量表，为页脚和实时子代理统计提供数据。
- `~/.zcode/cli/rollout/model-io-sess_*.jsonl`：读取逐次模型调用记录，为文本报告和仪表盘提供数据；调试目录中的同类日志也能自动识别。

本地 HTTP 服务只监听 `127.0.0.1`，默认端口为 `7864`。统计与展示过程不联网、不上传数据；只有安装或卸载页脚时，`npx` 可能需要联网获取打包工具。

SessionStart hook 只负责清理过期状态和按需拉起服务，不向对话上下文写入任何内容。插件自身的状态、校准数据和日志保存在 `~/.zcode/token-rate-hud/`。

## 升级、卸载与恢复

### ZCode 升级后

ZCode 升级通常会覆盖 `app.asar`，页脚因此消失。重新运行 `/token-rate ui`，然后完全退出并重开 ZCode 即可。报告功能不受应用包覆盖影响。

### 卸载

1. 运行 `/token-rate ui-off`，移除当前应用包中的页脚注入和 launchd 常驻服务。
2. 完全退出并重开 ZCode。
3. 如不再需要插件，在插件管理器中卸载 `token-rate-hud`。

`ui-off` 会从**当前版本**的 `app.asar` 中剔除注入标签并重新打包，不会直接用旧备份覆盖当前应用。

> [!WARNING]
> `app.asar.token-rate-bak` 仅是应急备份。ZCode 升级后，该备份可能属于旧版本；不要在版本不匹配时手动覆盖新包。

### 启动异常时

修改应用包可能使签名校验失败；如果应用带有 quarantine 隔离标记，Gatekeeper 还可能阻止启动。安装器会检测并提示这一情况。遇到启动异常时，先移除页脚注入：

```bash
T="$(ls -d ~/.zcode/cli/plugins/cache/token-rate-local/token-rate-hud/*/lib/tokrate.py 2>/dev/null | sort -V | tail -1)"
python3 "$T" ui-uninstall
```

只有确认应急备份与当前 ZCode 版本完全匹配时，才考虑手动恢复备份。

## 常见问题

| 现象 | 排查方法 |
|---|---|
| 找不到用量数据源 | 当前 ZCode 版本可能未写入所需日志或数据库；先运行 `/token-rate ui-status` 检查用量库状态 |
| 页脚不出现 | 确认已执行 `/token-rate ui` 并用 Cmd+Q 完全重启；再运行 `/token-rate ui-status` 检查注入与服务状态 |
| 升级后页脚消失 | 重新运行 `/token-rate ui`，然后完全重启 ZCode |
| 页脚出现但没有数字 | 查看 `~/.zcode/token-rate-hud/ui/server.log` 中的服务与 `[inject]` 诊断信息 |
| 仪表盘无法访问 | 重新运行 `/token-rate live`，确认地址为 `http://127.0.0.1:7864`；独立服务日志位于 `/tmp/token-rate-hud.log` |
| 多窗口数据串台 | 升级到最新版本；实时数据会按窗口所属会话隔离，无法识别会话时会选择不显示，而不是回退到全局数据 |

## 项目结构

```text
zcode-token-rate-hud/
├── README.md                 # 用户说明
├── LICENSE                   # MIT 许可证
├── marketplace.json          # 本地插件市场清单
├── sync.sh                   # 仓库同步脚本
└── token-rate-hud/           # 插件本体
    ├── .zcode-plugin/        # 插件清单
    ├── commands/             # /token-rate 命令定义
    ├── hooks/                # SessionStart hook
    ├── lib/                  # 核心统计、CLI 与本地服务
    └── ui/                   # 页脚注入脚本
```

数据来源、完整统计口径、注入保障细节和环境变量说明见[技术文档](./token-rate-hud/README.md)。

## 许可证

[MIT](./LICENSE)
