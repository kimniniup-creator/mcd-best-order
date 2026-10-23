#!/bin/sh
# Git supplies sh on Windows; no Python is required before uv installation.
if ! command -v uv >/dev/null 2>&1; then
  hint='缺少 uv。Windows PowerShell: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" ; macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh 。请先征得用户同意安装，完成后重开终端和宿主。'
  case "$1" in
    server) printf '%s\n' "$hint" >&2; exit 127 ;;
    *) printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"缺少 uv。请向用户原样展示 README 中的 uv 安装命令，未经同意不要自动安装；安装后重开终端和宿主。"}}'; exit 0 ;;
  esac
fi
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$1" in
  server) exec uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.0/coupon_split-0.5.0-py3-none-any.whl coupon-split-mcp ;;
  hook) exec uv run --no-project --python 3.14 "$root/hooks/order_context.py" ;;
  check) exec uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.0/coupon_split-0.5.0-py3-none-any.whl coupon-split startup ;;
  *) exit 2 ;;
esac
