---
description: 查看 token 速率报告（当前/本轮出速率、Decode 均值、端到端、会话累计）；ui 参数管理界面页脚，live 打开实时仪表盘
argument-hint: [ui | ui-off | ui-status | live]
---

# token 速率报告

用户想查看当前会话的 token 速率统计。先取插件库路径，再按参数执行：

```bash
T="$(ls -d ~/.zcode/cli/plugins/cache/token-rate-local/token-rate-hud/*/lib/tokrate.py 2>/dev/null | sort -V | tail -1)"
echo "脚本：$T"
```

## 无参数（默认）：文本报告

```bash
python3 "$T" report
```

将输出原样放入代码块展示，可在其后附最多三行解读（当前出速率、本轮均值、会话累计）。

## 参数为 ui：安装界面页脚

先向用户说明这一步做什么，再执行：

> 界面页脚会把统计行画在每条回答下方（DeepSeek 风格）。ZCode 渲染层没有插件 UI 接口，因此需要给 ZCode 的 app.asar 注入一行脚本：会自动备份原始包到 `app.asar.token-rate-bak`，可随时用 `ui-uninstall` 完整还原；ZCode 每次升级会覆盖 app.asar，重跑一次本命令即可恢复。数据服务本地只读、仅监听 127.0.0.1。

```bash
python3 "$T" ui-install
```

然后把脚本输出原样展示，并提示用户：**完全退出 ZCode（Cmd+Q）再打开**方能生效；还原命令是 `python3 "$T" ui-uninstall`。

## 参数为 ui-off：卸载界面页脚

```bash
python3 "$T" ui-uninstall
```

提示用户完全退出并重开 ZCode 后页脚消失。

## 参数为 ui-status：查看页脚状态

```bash
python3 "$T" ui-status
```

## 参数为 live：实时仪表盘

```bash
cd /tmp && nohup python3 "$T" serve --port 7864 >/tmp/token-rate-hud.log 2>&1 &
```

告诉用户仪表盘地址为 http://127.0.0.1:7864 （每秒刷新当前速率、进行中轮次、最近 64 次调用速率柱状图）；用 `pkill -f "tokrate.py serve"` 停止。

若任一命令提示找不到数据源，说明当前 ZCode 版本不写用量数据，向用户说明该限制即可。
