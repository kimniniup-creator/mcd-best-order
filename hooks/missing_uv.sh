#!/bin/sh
# A dependency-free, read-only fallback. Git's sh/sed are already prerequisites.
# No arguments, credentials, network calls or order operations are executed.
while IFS= read -r request; do
  id=$(printf '%s\n' "$request" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*\("[^"\\]*"\|[0-9][0-9]*\).*/\1/p')
  [ -n "$id" ] || continue
  case "$request" in
    *'"initialize"'*)
      printf '%s\n' '{"jsonrpc":"2.0","id":'"$id"',"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"点餐安装提示","version":"0.5.3"},"instructions":"缺少 uv，只在用户点餐时调用 mcd_start 展示中文安装命令；其他对话正常进行。"}}' ;;
    *'"tools/list"'*)
      printf '%s\n' '{"jsonrpc":"2.0","id":'"$id"',"result":{"tools":[{"name":"mcd_start","description":"点餐服务缺少 uv。调用后原样展示中文安装命令，安装后重开终端；其他对话可正常进行。","inputSchema":{"type":"object","properties":{"utterance":{"type":"string"}},"required":["utterance"]},"annotations":{"readOnlyHint":true,"destructiveHint":false,"openWorldHint":false}}]}}' ;;
    *'"tools/call"'*)
      printf '%s\n' '{"jsonrpc":"2.0","id":'"$id"',"result":{"content":[{"type":"text","text":"缺少 uv，点餐功能暂时不可用，其他 Codex 对话可以继续。Windows PowerShell：powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"；macOS/Linux：curl -LsSf https://astral.sh/uv/install.sh | sh。安装后重开终端和 Claude Code 或 Codex。"}],"structuredContent":{"stage":"install","next_tool":null,"display_markdown":"缺少 uv，点餐功能暂时不可用，其他 Codex 对话可以继续。Windows PowerShell：`powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"`；macOS/Linux：`curl -LsSf https://astral.sh/uv/install.sh | sh`。安装后重开终端和 Claude Code 或 Codex。"},"isError":false}}' ;;
    *) printf '%s\n' '{"jsonrpc":"2.0","id":'"$id"',"result":{}}' ;;
  esac
done
