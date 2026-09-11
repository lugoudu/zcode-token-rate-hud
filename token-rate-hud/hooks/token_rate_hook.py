#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
token-rate-hud 的 hook 入口（PostToolUse / Stop / SessionStart）。

设计原则：
  - 任何异常一律静默退出（exit 0、无输出），绝不干扰会话；
  - 只读本地 ~/.zcode/cli/rollout/ 日志，不联网、不上传；
  - 输出严格符合引擎 hook 输出 schema（仅 additionalContext）。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))

import tokrate  # noqa: E402


def main():
    sub = sys.argv[1] if len(sys.argv) > 1 else "post"
    try:
        tokrate.mode_hook(sub)
    except Exception:
        pass  # 遥测失败不能影响正常工作
    return 0


if __name__ == "__main__":
    sys.exit(main())
