# 给安装本项目的 Agent

本项目支持任何能接 MCP 的 Agent。你不需要 Claude/Codex 斜杠命令，也不需要作者私有仓库权限。

1. 先读 [宿主安装表和自检](docs/install-for-agents.md)。根据当前应用名、启动命令、环境或 MCP 设置界面判断宿主；不要按正在使用的模型品牌猜宿主。`coupon-split setup --detect` 是只读辅助探测，多个候选时按当前应用选择。
2. 已装 uv 时运行下面命令，把 `<host>` 换为表中的名字。先预览；用户的“帮我安装”授权包含对应 MCP 注册，可核对路径后添加 `--apply`。没有安装授权时只预览。不要改模型、全局审批或其他 MCP。

   ```sh
   uvx --python 3.14 --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.7.5/coupon_split-0.7.5-py3-none-any.whl coupon-split setup --host <host>
   ```

3. 默认使用文档支持的项目配置；只提供全局配置的宿主会显示实际目标。隔离环境用 `--project <workspace> --home <isolated-home>` 或 `--config <actual-config-path>`。保留已有条目；写入前安装器会保留原文件备份。不要把 Token 写到仓库或聊天。
4. 未识别宿主：`setup --host generic` 输出 command/args/env 标准 MCP 配置。查当前宿主官方 MCP 文档，把条目合并进它指定的配置中，保留其外层格式；不臆造路径。不支持 stdio 时按文档使用本机 HTTP/SSE。
5. 刷新 MCP 或新开会话，确认发现 `mcd_start`、`quote_best`、`place_orders`、`wait_payment`。调用 `mcd_start(utterance="我要吃麦当劳")` 自检：未授权应给本机授权页；已授权应询问“帮我配还是自己点”。只有工具发现不能算完整点餐验收。安装自检绝不创建真实订单。

实际点餐必须遵守 MCP initialize instructions 和每步返回的 workflow/next_tool。Agent 负责吃什么，引擎负责价格。完整展示方案卡后等用户下一条明确确认，再逐字传其最后一句给 `place_orders.user_confirmation`。超过 ¥200 要求用户回复完整确认句。原样展示付款卡后循环 `wait_payment`；模拟模式无有效支付链接，不等真实付款。没有审批弹窗的宿主上，`place_orders` 会先返回确认页链接：原样发给用户，再用同样参数重新调用并等待用户在浏览器点“确认下单”，绝不自己点；用户点完“确认下单”订单立即创建，不再等用户回来说话。只有“确认下单”（超过 ¥200 为“确认下单 ¥金额”，可带“好的”“谢谢”）才算确认，其余话一律不算，服务端只提示用户回复“确认下单”。用户在下单前后说“取消/不要了/别下单/等等”等撤回词（出现在句中任何位置，哪怕同句有“确认”），把这句原话逐字传给 place_orders（或调用 cancel_plan）：下单前会中止，订单已建则立即撤回，并如实转告结果。用户须知：回复“确认下单”才会下单；说“取消/不要了”随时撤回。之后才会建真实待付款单，首次使用必须向用户说明。工具结果末尾“给 Agent 的下一步”代码块是给你的参数，不要展示给用户。

开发本仓库时：不读取或提交个人凭据，不真实下单。运行 `pytest`；配置变更须验证预览不写、先备份、保留其他条目和对称卸载。不要把上面的安装自检当成真实付款验收。
