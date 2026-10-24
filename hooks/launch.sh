#!/bin/sh
# Resolve a fixed cached release before attempting any network access.
if ! command -v uv >/dev/null 2>&1; then
  hint='缺少 uv。Windows PowerShell: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" ; macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh 。安装后重开终端和 Claude Code 或 Codex。'
  case "$1" in
    server) printf '%s\n' "$hint" >&2; exec sh "$(dirname -- "$0")/missing_uv.sh" ;;
    *) printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"缺少 uv。请向用户展示安装命令：Windows PowerShell：powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"；macOS/Linux：curl -LsSf https://astral.sh/uv/install.sh | sh。安装后重开终端。"}}'; exit 0 ;;
  esac
fi
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source='https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.6.2/coupon_split-0.6.2-py3-none-any.whl'
case "$1" in
  server) exec uv run --offline --no-project --python 3.14 "$root/hooks/stdio_gateway.py" ;;
  hook) exec uv run --offline --no-project --python 3.14 "$root/hooks/order_context.py" ;;
  check)
    if ! uv python find --offline 3.14 >/dev/null 2>&1; then
      printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"点餐工具首次准备中。若暂时不可用，请稍候再试；持续失败请检查 uv 和网络后重开对话。"}}'
      exit 0
    fi
    exec uv run --offline --no-project --python 3.14 "$root/hooks/order_context.py" startup ;;
  *) exit 2 ;;
esac
