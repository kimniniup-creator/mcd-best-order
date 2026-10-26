# 任意 MCP Agent 安装与自检（0.7.5）

模型名不等于宿主名：DeepSeek 可以运行在 DSH、Cursor 等不同应用中。先看当前应用、命令或配置界面，再选 host。无需斜杠命令、Skill 或钩子；MCP instructions、工具描述和文本结果自带流程，MCP prompts 的 `order_mcdonalds` 是可选入口。

## 命令

先准备 [uv](https://docs.astral.sh/uv/getting-started/installation/)，无需自己安装 Python。下文 `coupon-split` 均可替换为：

```sh
uvx --python 3.14 --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.7.5/coupon_split-0.7.5-py3-none-any.whl coupon-split
```

```sh
coupon-split setup --detect
coupon-split setup --host cursor
coupon-split setup --host cursor --apply
coupon-split uninstall --host cursor
coupon-split uninstall --host cursor --apply
coupon-split setup --host generic
```

默认预览仅显示目标路径及本插件条目，不输出其他服务器的秘密。`--apply` 才写入；已有文件按原字节备份到同目录唯一 `.coupon-split-<id>.bak`，再替换本插件条目。JSONC 注释和排版在首次写入时规范化，其他配置的含义保持；备份保留原文。重复安装不重复注册或反复备份。卸载只移除本条目，保留配置文件、备份、其他 MCP、账户和点餐数据。备份可能含个人配置，别提交或分享。

`--project` 指定项目目录，`--home` 指定隔离 home，`--config` 指定真实配置文件。扩展版 Cline、非默认 VS Code profile/远程环境请从 MCP 界面打开实际配置文件，再用 `--config`，不要猜用户数据目录。安装器不调整全局工具审批，也不自动启用真实写工具。

## 宿主与配置

来源为各宿主官方文档（2026-10-26 核对）；下表是默认目标。不同版本、远程窗口、WSL 或配置 profile 应以该宿主实际打开的文件为准。

| host | 默认配置 / 格式 | 可选说明或 Skill | 官方来源 |
|---|---|---|---|
| dsh | 独立 profile：`$DSH_HOME/profiles/mcd/`（默认 `~/.dsh`）下的 `cordis.patch.yml`（用标记块管理）、`skills/mcd-order/SKILL.md` 和 profile 骨架；启动用 `dsh --profile mcd`；不改其他 profile、settings 或凭据 | 同一个 profile 的 `skills/` 目录，已由 setup 写好；MCP 流程不依赖 Skill | [MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)、[profile 与 patch](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md) |
| kiro | 项目 `.kiro/settings/mcp.json`，`mcpServers` | `.kiro/steering/mcd-order.md`；可选 `.kiro/skills/mcd-order/SKILL.md` | [MCP](https://kiro.dev/docs/mcp/configuration/)、[作用域](https://kiro.dev/docs/cli/chat/configuration/) |
| cursor | 项目 `.cursor/mcp.json`，`mcpServers` | `.cursor/rules/mcd-order.mdc`（按当前 Rules 格式） | [MCP](https://cursor.com/docs/context/mcp)、[Rules](https://cursor.com/docs/context/rules) |
| vscode | 项目 `.vscode/mcp.json`，`servers`，`type:stdio` | `.github/copilot-instructions.md`，保留原文后追加 | [MCP 格式](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) |
| windsurf | 已存在旧版 `.codeium/windsurf/mcp_config.json` 时沿用；当前 Cascade Windows `%APPDATA%/devin/mcp_config.json`、macOS/Linux `$XDG_CONFIG_HOME/devin/mcp_config.json`（默认 `~/.config`），`mcpServers` | 工作区 `.windsurf/rules/`；新 Devin Local agent 使用不同配置，请选 generic 并读当前文档 | [官方 Cascade（现重定向 Devin）](https://docs.windsurf.com/windsurf/cascade/mcp) |
| cline | CLI `~/.cline/data/settings/cline_mcp_settings.json`，支持 `CLINE_MCP_SETTINGS_PATH`；扩展必须用界面打开路径 + `--config` | `.clinerules/` | [MCP](https://docs.cline.bot/mcp/mcp-overview) |
| roo | 项目 `.roo/mcp.json`，`mcpServers` | `.roo/rules/` | [MCP](https://docs.roocode.com/features/mcp/using-mcp-in-roo) |
| gemini（gemini-cli） | 项目 `.gemini/settings.json`，`mcpServers` | 项目 `GEMINI.md` 或当前 Agent Skills 配置；不用打开全局 trust | [MCP](https://geminicli.com/docs/tools/mcp-server/) |
| claude-desktop | Windows `%APPDATA%/Claude/claude_desktop_config.json`；macOS `~/Library/Application Support/Claude/claude_desktop_config.json`，`mcpServers` | MCP instructions/prompt 已足够；不强装 Claude Code Skill | [本地 MCP](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| claude-code | 保留 README 插件市场两步安装（含 Skill 和钩子）；setup 还提供项目 `.mcp.json` 直连，不要同时启用两份同名服务 | 插件 `skills/mcd-order/SKILL.md` | [插件](https://code.claude.com/docs/en/plugins)、[项目 MCP](https://code.claude.com/docs/en/mcp) |
| codex-plugin | 先按 README 安装插件；setup 只配置 `CODEX_HOME/config.toml` 的本插件审批，写工具仍 prompt | 插件自带 Skill | [插件](https://developers.openai.com/codex/plugins) |
| codex-ide | `CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`），`mcp_servers`；CLI/IDE 共用 | setup 同时把 Skill 写到 `CODEX_HOME/skills/mcd-order/SKILL.md`（已验证：没有它时，Codex 可能对一句笼统的“我要吃麦当劳”先自己提问、不调用点餐工具）；已有不同的同名文件会先备份，卸载只移除未被改动的那份 | [MCP](https://developers.openai.com/codex/mcp) |
| generic | 只输出标准 stdio `command/args/env`；根据宿主官方文档合并 | 可粘贴本页“点餐约定”到宿主规则区 | [MCP 传输](https://modelcontextprotocol.io/specification/latest/basic/transports) |

支持别名 `deepseek-harness`、`vscode-copilot`、`roo-code`。可选说明文字不由安装器自动覆盖；既有用户规则应先读再合并。项目 MCP 配置可能需要用户信任工作区并重启服务。

VS Code 新 Copilot harness 也支持工作区根 `.mcp.json` 或 `$COPILOT_HOME/mcp-config.json` 的 `mcpServers` 格式；本安装器默认仍使用有 IntelliSense 的 `.vscode/mcp.json`。若你的当前 harness 只读 portable 格式，使用 `setup --host generic` 的内容，合并到官方指定 portable 文件，不能把 `servers` 和 `mcpServers` 混用。

DeepSeek Harness（dsh）的特殊点，都已由 `setup --host dsh` 处理：

- 一个 MCP 服务对应一行 Cordis 配置（`@deepseek-ai/dsh-mcp-client`，不接受 `mcpServers` JSON）。安装器在独立 profile `mcd` 的 `cordis.patch.yml` 里写一个带标记的块，卸载时只移除这个块。若 profile 目录不存在，会按 dsh 的 profile 清单格式新建（`package.json`、`cordis.yml`、`pnpm-workspace.yaml`），已有文件不覆盖。要手动安装，把这个块复制进你自己的 `cordis.patch.yml`，把 `skills/mcd-order/SKILL.md` 放进该 profile 的 `skills/`。
- dsh 会从 stdio 子进程环境里删掉名字含 KEY/TOKEN/SECRET/PASSWORD 的变量，所以块里用 `process.env.MCD_MCP_TOKEN` 显式转发；Token 永远只读环境变量，不写进文件。启动 dsh 的同一个终端里设置它。
- dsh 只把工具结果的文本交给模型，看不到 `structuredContent`。每个工具结果末尾都有“给 Agent 的下一步”代码块（next_tool、next_arguments、session_id、plan_id、confirm_token、store_code、items……），structuredContent 里有同样内容，所以只读文本的宿主也能走完整个流程。
- dsh 对 MCP 工具不弹审批。服务端因此在 `place_orders` 里自己要一次由人完成的确认（见下一节），模型自己写“确认”不能下单。
- 默认模拟模式（`COUPON_SPLIT_SIMULATE` 未设置时为 true）。真实下单：启动 dsh 前设置 `COUPON_SPLIT_SIMULATE=false`。
- 卸载：`coupon-split uninstall --host dsh --apply`（保留 profile 目录），或手动删除整个 `$DSH_HOME/profiles/mcd`。

dsh 的 npm bundle 形式的发布是可选项，本版没有做；手动步骤同上。

## 下单前的人工确认（所有宿主）

模型自己填写的 `user_confirmation` 不能证明用户确认过。`place_orders` 因此在服务端要一次由人完成的确认，按顺序：

1. 宿主本身一定会让用户点允许、且无法被绕过（Claude Code，`clientInfo.name` 为 `claude-code`，已用无头模式验证连 `bypassPermissions` 也跳不过）：直接放行，Claude 钩子仍核对原话。
2. 宿主支持 MCP elicitation：服务端弹出确认框，显示门店、餐品和金额，用户接受后才下单。
3. 否则（dsh、多数 IDE 的 Agent、HTTP 模式）：`place_orders` 先返回 `stage=confirm` 和本机确认页链接。Agent 把链接原样发给用户，再用同样的参数再次调用 `place_orders`，服务端最多等约 50 秒让用户在浏览器点“确认下单”。点了才下单；点“先不下单”或超时不会下单。确认页只接受同源的页面点击，一次确认只对应这个方案、金额和取餐方式。

这挡的是“模型代替用户确认”，不是本机上的恶意程序：能读 `~/.coupon-split` 并伪造浏览器请求的本机程序不在防护范围。钩子拿不到对话记录的宿主（如 dsh 的 Claude 钩子桥）里，本插件的核对钩子会返回 `ask` 而不是直接拒绝。

## 本机 HTTP / SSE

stdio 是本地 CLI/IDE 最简单的方式。HTTP-only 宿主或需要独立常驻服务时运行：

```sh
coupon-split serve --http
coupon-split serve --http --port <端口号>
```

默认随机空闲端口，只绑定 `127.0.0.1`。启动行给出实际 `/mcp` URL 和本机 Bearer header；复制到宿主的 MCP URL/header 配置中。该访问令牌和麦当劳账户 Token 是两件事，均不要发聊天。无令牌/错误令牌返回 401；仅支持同源或无 Origin 的本机客户端。重启后端口可能变；保留终端直到付款结束。

Cursor/Kiro 可用 `url`，VS Code 可用 `type:http` + `url`，Gemini CLI 的 Streamable HTTP 字段为 `httpUrl`（`url` 是旧 SSE），DSH 用 `transport:streamable-http` + `url`。Cline/Roo 用其官方界面选择 Streamable HTTP；Claude Desktop 的本地安装默认 stdio。不能把新 `/mcp` 端点当成旧 `/sse`。纯远端云宿主无法直接访问你电脑的 loopback；需由宿主提供本地连接能力，本版不自动开放公网或建立隧道。

## 自检与点餐约定

1. MCP initialize 应有完整 instructions，tools/list 应含点餐和付款工具；支持 prompts 的宿主可读 `order_mcdonalds`，不支持 prompts 仍能工作。
2. 实际调用 `mcd_start(utterance="我要吃麦当劳")`。未授权只在本机页输入自己的 Token，切勿贴进聊天；已授权会询问点法及缺失要素。
3. “帮我配”由 Agent 读当前菜单并用 `propose_cart` 校验，主备选经用户认可后才 `quote_best`。“自己点”先把点餐页链接发到可见正文，再 `get_cart` 等待。原样展示方案卡，价格由引擎算。
4. 报价后结束回合等明确确认。`place_orders.user_confirmation` 逐字复制用户本轮最后一句；只有“确认下单”“确认下单 ¥金额”这类标准说法（可带“好的”“谢谢”）算确认，其他话只提示用户回复“确认下单”；含取消/不要了/别下单/等等等撤回词的话（哪怕同句有“确认”）一律按撤回处理。回复“确认下单”才会下单；说“取消/不要了”随时撤回。金额写错也拒绝。超过 ¥200 要完整原文“确认下单 ¥金额”（两位小数），辅助字段不能替代原文。`place_orders` 可能先返回确认页链接（见上一节“下单前的人工确认”）：原样发给用户，再用同样参数重新调用并等待，绝不自己点。确认后才会建真实待付款单；首次提醒用户，可先模拟。若下单没成功，原样转述麦当劳的原话；状态是核对中就用 `wait_payment` 等结论，不要重新下单。
5. 原样展示付款卡后循环 `wait_payment`；只读查询不自行下单。模拟模式没有可用支付链接或真实订单；不要拿模拟付款卡冒称真实支付成功。

安装自检不要下单。完整模拟测试须显式设 `COUPON_SPLIT_SIMULATE=true`、`ALLOW_REAL_ORDERS=false`；若宿主会清理环境，将这两个非秘密开关放进本插件配置的 env。仅看到 tools/list 或配置语法正确，只能记为“配置验证”，不能记为“宿主完整实测”。
