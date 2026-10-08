#!/usr/bin/env bash
# 一键导出本机长效令牌并注入 GitHub Secret。
# 解释器不写死版本号：优先 command -v，找不到再退回 WorkBuddy 托管 python（按目录序取最新）。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -z "${PYTHON_BIN}" ]; then
  for cand in "$HOME"/.workbuddy/binaries/python/versions/*/python.exe \
              "$HOME"/.workbuddy/binaries/python/versions/*/python; do
    [ -x "$cand" ] && PYTHON_BIN="$cand"
  done
fi
if [ -z "${PYTHON_BIN}" ]; then
  echo "[FAIL] 未找到 python 解释器（command -v python3 / python 均不可用）" >&2
  exit 1
fi
echo "[ok] 解释器：$PYTHON_BIN"

exec "$PYTHON_BIN" "$SELF_DIR/export_token.py" "$@"
