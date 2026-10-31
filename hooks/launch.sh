#!/bin/sh
# Resolve a fixed cached release before attempting any network access.
if ! command -v uv >/dev/null 2>&1; then
  hint='缺少 uv。Windows PowerShell: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" ; macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh 。安装后重开终端和你的 Agent。'
  case "$1" in
    server) printf '%s\n' "$hint" >&2; exec sh "$(dirname -- "$0")/missing_uv.sh" ;;
    *) printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"缺少 uv。请向用户展示安装命令：Windows PowerShell：powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"；macOS/Linux：curl -LsSf https://astral.sh/uv/install.sh | sh。安装后重开终端。"}}'; exit 0 ;;
  esac
fi
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v node >/dev/null 2>&1; then
  printf '%s\n' '缺少 Node.js。请安装 Node.js LTS，重开终端和 Agent，再运行 coupon-split doctor。' >&2
  exit 1
fi
exec node "$root/hooks/launch.cjs" "$@"
