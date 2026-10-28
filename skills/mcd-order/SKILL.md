---
name: mcd-order
description: 麦当劳点餐入口。用户想吃、点、外送麦当劳，或说餐品、帮我配、人数、预算、我自己点、照上次那份、好了，就调用 mcd_start(utterance=用户本轮原话)，无需 shell 读取本文件。报价后的确认直接交给 place_orders，不要再调用 mcd_start。服务尚未连上时说“服务启动中，请稍候再说一次”。
---

你理解用户、决定下一步；服务端把你的理解落到麦当劳的真实数据上，并做确定性校验。完整的本体概要和安全不变量在 MCP instructions 里，这里只写 Claude Code 里的补充。

## 怎么用

- 用户提到点餐，先 `mcd_start(utterance=本轮原话, foods=你理解出的餐品)`。utterance 逐字等于最后一条用户消息，不拼历史；同一对话后续轮传前一轮返回的 `session_id`。
- 想把取餐方式、地标、外送地址的说法、时间、餐品与规格、人数、忌口一次说清，用 `resolve_order(utterance, interpretation)`。用户没说的槽位不要填。
- 工具返回 `resolved`（值和来源）、`ambiguous`（候选和要问的事实）、`missing`（必须问用户的槽位）、`violations`（违反的约束、事实、可行替代）、`state` 和 `allowed_actions`。`missing` 和 `ambiguous` 里的事实问用户；`violations` 里的事实告诉用户并给出可行选择；不替用户选，也不绕过约束。
- 外送地址只能是账户已保存的地址；你转述用户的说法，服务端去对。门店营业和预约时段以门店返回为准，不可行时服务端会给出可选时段。
- 返回里的 `reply_must_show_user`（点餐页、确认页、付款页链接，报价金额，授权页）必须出现在你给用户的回复里：用户看不到工具返回。用户自己点时，`picker_url` 就是点餐页链接。`get_cart` 返回 `page_opened` 说明用户是否打开过页面，`polls_without_page_open` 连续增加说明用户没有拿到链接。
- 用户直接说餐品：`resolve_order` 或 `mcd_start(foods=...)` 落到菜单；规格（大杯、去冰）放在 `foods[].spec`，官方没有的规格服务端会列出官方选项，不近似替换。
- 用户要你配餐：`get_menu(recommend=true, session_id, store_code)` 取配餐上下文，你自己配主推和备选，用 `propose_cart` 校验，用户认可后再 `quote_best(session_id=...)`。认可配餐只代表同意核价，不代表确认下单。
- 价格只来自 `quote_best`。

## 安全不变量

1. 报价后用户说的标准确认句（“确认下单”等，超过 ¥200 要带金额）逐字交给 `place_orders(user_confirmation=...)`；不得代造、拼接或补金额。真实订单另需用户本人在确认页点击，或在确认弹窗里填回与报价一致的实付金额后接受，任何宿主（含 Claude Code、Codex）都一样。看到确认页链接就发给用户，不自己打开或点击。
2. 用户任何时候表达不想要、取消、改天、不吃了、等一下、先别、我再想想，就调用 `withdraw_order(plan_id)`（或 `cancel_plan`）并如实转告；拿不准先问“要撤回这单吗”。“不要香菜”“换成大杯可乐”只是改餐品。确认页和付款页也有“撤回这单”按钮。
3. 下单后展示付款卡和付款页链接，用 `wait_payment(plan_id, timeout=60)` 等结果；模拟模式没有真实付款，不等待。结果不明时只对账或撤回，不重新下单。
4. 不创建地址，不向对话索要 Token；缺授权只给本机授权页。

## 其他

- 工具结果末尾有一块结构化 JSON（宿主只显示文字时从里面取 `session_id`、`plan_id`、`confirm_token` 等）；这一块不展示给用户。`display_markdown` 是现成的展示素材，可以直接用，也可以自己组织语言。
- MCP 启动失败且缺 uv 时，转达安装提示。Windows PowerShell：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`；macOS/Linux：`curl -LsSf https://astral.sh/uv/install.sh | sh`。未经用户同意不要代执行安装，安装后重开终端和你的 Agent。
- 授权失效时调用 `mcd_start` 获取本机重新授权链接，授权后继续原来的点餐，不让用户重说。
- 下单后状态是 `verifying`（核对中）时用 `wait_payment` 等结论，不要重新下单；`place_failed` 如实转述麦当劳原话和“没扣钱”。
