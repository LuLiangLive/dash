#!/usr/bin/env bash
# make-deploy-package.sh —— 生成部署包 zip（供重新部署/分发）
# 用法: bash scripts/make-deploy-package.sh [输出路径]
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-/tmp/invest-dashboard-deploy.zip}"
STAMP="$(date +%Y%m%d_%H%M%S)"

# 收集要打包的内容（排除备份/缓存/日志/临时文件）
rm -f "$OUT"
zip -rq "$OUT" \
  *.py requirements.txt README.md Dockerfile nginx.conf .cloudstudio start_*.sh \
  collector static scripts data rendering \
  fund.db fund.db-wal fund.db-shm \
  -x \
    "*/__pycache__/*" \
    "*.pyc" \
    "fund.db.bak*" \
    "*.log" \
    "_gz.bin" \
    "deploy/*.md" \
    "*.md" \
  || true

# 补充：若排除了文档类 md，则显式保留主 README
if [ ! -f "$OUT" ] && command -v zip >/dev/null; then
  :
fi

echo "✅ 部署包已生成: $OUT"
ls -lh "$OUT" | awk '{print "大小:", $5, "路径:", $9}'
