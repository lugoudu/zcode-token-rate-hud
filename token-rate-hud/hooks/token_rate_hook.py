#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
token-rate-hud 的 hook 入口（仅 SessionStart）。

设计原则：
  - 产品承诺零上下文注入：本插件任何 hook 路径绝不输出 additionalContext，
    stdout 保持为空；
  - 任何异常一律静默退出（exit 0、无输出），绝不干扰会话；
  - 只读本地数据（rollout 日志 + SQLite 只读），不联网、不上传。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))

import tokrate  # noqa: E402


def main():
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    if sub != "session_start":
        # 未知/已下线参数（旧版 post/stop/reset 注入路径已移除）直接退出：
        # 不读 stdin、不执行任何逻辑，诊断写 stderr（不进模型上下文）
        sys.stderr.write(f"token-rate-hook: 不支持的参数 {sub!r}，已忽略\n")
        return 0
    try:
        tokrate.mode_session_start()
    except Exception:
        pass  # 遥测失败不能影响正常工作
    return 0


if __name__ == "__main__":
    sys.exit(main())
