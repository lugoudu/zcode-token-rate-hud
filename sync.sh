#!/bin/bash
# token-rate-hud 改动同步：提交全部变更并推送到 GitHub
# 用法：./sync.sh "简短改动说明"（省略参数时使用默认提交信息）
set -e
cd "$(dirname "$0")"

if [ -z "$(git status --porcelain)" ]; then
  echo "（无变更，跳过）"
  exit 0
fi

MSG="${1:-chore: 同步插件改动 $(date +%Y-%m-%d\ %H:%M)}"
git add -A
git commit -m "$MSG"
git push
echo "✓ 已提交并推送：$MSG"
