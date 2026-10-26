---
name: mcd-order
description: 麦当劳点餐入口。直接调用 mcd_start(utterance=用户本轮原话)，无需 shell 读取本文件；完整协议在工具描述和返回里。用于点餐、修改、帮我配、推荐一下、随便来点、人数/口味/预算、我自己点、照上次那份、“继续刚才的”“接着点刚才那个”及“好了”恢复。报价后的确认直接调用 place_orders，不能调用 mcd_start。服务尚未连上时说“服务启动中，请稍候再说一次”。
---

1. 报价后用户的“确认下单”或“确认下单 ¥金额”（超过 ¥200 必须带金额，可带“好的”“谢谢”）直接调用 `place_orders`；其他任何话都不算确认，不要自己判断“这算同意”，也逐字传入，服务端只提示或撤回。用户回复“确认下单”才会下单；说“取消/不要了/别下单/等等”随时撤回（同句带“确认”也按撤回），把原话逐字传给 `place_orders`，禁止调用 `mcd_start`。其余每条点餐原话（包括后续的方式、外带和餐具）立即调用 `mcd_start(utterance)`；同一对话后续轮传前一轮返回的 session_id，新对话留空。utterance 必须逐字等于本轮最后一条用户消息，严禁拼入上一轮餐品/要素或改写成汇总；例如第二轮只说“在望京中街附近”，就只传这句，历史由服务端自动保留。只使用本机授权账户；缺授权时原样展示 setup_url，不向对话索要 Token。
2. **原样展示 `display_markdown`**。第一问严格按返回的 T1 编号；`reply_options` 决定数字含义。具体餐品已说就保留在 session，不再问推荐或让用户重复点餐。地点和方式只问缺项，账户不问；时间默认立即，不沿用旧预约。
3. 收到“自己点 / 帮我配 / 照上次那份”时，把原 `session_id` 传给 `find_store`，`plan` 用这三个中文值。门店没定以前不发点餐页链接。外送先 `delivery_addresses` 选已有地址，再 `find_store(fulfillment="delivery", address_id=...)`，不用猜 beCode。不得创建测试地址。
4. `mcd_start` 或 `find_store` 返回 `items` / `selected_items` 时，这些就是原话餐品的购物车。接着 `quote_best`，不要再问推荐。返回 `next_tool`、`next_arguments` 是后续执行依据；已给地点时立即继续工具链，不在“已收到你的选择”处结束。新门店已有原话时 `get_menu(session_id=...)` 会按菜单继续解析。
5. 用户要自己点且门店已定，**先把 `display_markdown`（含 [打开点餐页](链接)）整段原样输出为用户可见的 assistant 正文，再在同一回合调用 `get_cart(session_id, wait=25, link_displayed=true)`**。用户看不到工具结果；内部思考、工具参数和工具结果中的链接都不算展示。必须在轮询之前发出正文，否则用户没有链接可点，永远无法提交。未提交就继续长轮询。提交后自动 `quote_best(items,store_code,elements)`，不要求用户回来说“选好了”。不输出内部说明或英文元话。链接只发一次，禁止催开页、声称发了两次或要求打开后说一声。超时继续 get_cart(wait=25)，不重复链接。选好会自动接着算；如果没接上，回来说一句“好了”。用户说“好了”时调用 mcd_start(utterance="好了") 取回原会话并报价，没有 Stop 钩子的宿主也照此恢复。购物车持久化 24 小时，MCP 退出后页面服务退出。

   同一条 assistant 消息按这个顺序组织：先 text 块原样写“最近的是… [打开点餐页](实际URL) — 选好点底部‘选好了，去算最省’，选好会自动接着算；如果没接上，回来说一句“好了”。”，再 tool_use 块调用 get_cart。禁止只有 tool_use 块。
6. 用户直接说餐品时 `get_menu(search=原话, store_code, elements)` 按菜单名称/别名匹配，使用返回的 `selected_items` 官方默认规格。需要改杯型或套餐选配时再取 `get_item_detail`。用户说“帮我配”“推荐一下”“随便来点”或给人数、口味、预算时，走 `get_menu(recommend=true,session_id,store_code)` 获取配餐上下文。你就是宿主配餐模型，不安装或调用其他大模型。阅读菜单分页、官方套餐组成/选配及 user_requests，避忌口、按人数和预算搭配；未知辣度/过敏原/份量不要猜。每人约一主食一饮料、每两人一小食，多人考虑分享餐；不要给一人两个汉堡加水。给不同的主推+备选和各一句理由，调用 `propose_cart(session_id,options=[{title,reason,items:[{productCode,quantity,roundList?,modification?}]}])`；错误按原因重配。原样展示两个方案和照原样价，结束回合等用户认可。用户“就这样”“换备选”交 `mcd_start` 后按返回 `quote_best`；“换成鸡肉的”“不要辣的”“再加一份薯条”交 mcd_start 保留人数/原话条件，再 get_menu → propose_cart。认可配餐只代表同意核价，不代表确认下单。工具 next_tool=null 就停止，不能提前报价。网页 simple_recommend 只做简易兜底。
7. `quote_best` 传固定 elements：`fulfillment`、`addressId`（外送）、`takeWayCode`（可暂缺）、`reservationDate`（仅本次明确预约）、`needTableware`，账户由官方填。预约必须取门店时段核实；工具参数按 schema 修正，只向用户说需要补充的点餐信息，绝不能删掉预约后改成立即单。
8. **原样输出 display_markdown，不得改写或删行。** 完整展示 `display_markdown` 的省钱、金额、单数/付款次数、不买卡备选、订单要素和规格；到报价结束回合，等明确确认。`elements_pending` 的堂食/外带并到确认句，不先多问一轮。
9. **严禁自主下单**。报价后结束回合，等用户明确确认，将确认原话逐字传入 `place_orders(user_confirmation=...)`，凭证绑定账户，10分钟过期。服务端还会让用户本人再确认一次：place_orders 可能返回 stage=confirm 和确认页链接（或宿主弹出确认框）。此时把链接整段原样写进可见正文，再用同样参数再次调用 place_orders 等用户点“确认下单”，绝不自己打开、提交或代替用户点击；有宿主审批时也必须通过。默认真实下单；`COUPON_SPLIT_SIMULATE=true` 为模拟模式。超过 ¥200 时，先展示具体金额，要求用户追加回复“确认下单 ¥实际金额（两位小数）”，逐字传入 `high_amount_confirmation`。审批或确认被拒绝时不得绕过。下单后原样输出支付卡，15分钟内在麦当劳官方付款，手机可跳 App，钱直接给麦当劳；用户说撤回就 `cancel_plan`，再 `pay_card` 确认状态和退券。结果不明时只对账/撤回，禁止重新下单。仍禁止 `confirm_address`、地址创建、领券、积分和抽奖。错误用人话呈现，不编造报价。

10. `get_cart` 返回 `handoff_correction` 时，若链接尚未展示，立即把 display_markdown 作为可见正文输出，再继续等待；同一回合已展示就不重复，包括“好了”恢复回合。预约不清楚必须按 `time_clarification` 追问，禁止模型自行猜日期/时段。sources 由服务端核对整个当前会话的原话，跨轮保留“你说的”；只来自已结束上一单的历史才标“沿用上次”，系统最近门店标“最近”，其他标“默认”。
11. “好了”恢复后的 quote_best 方案卡与正常路径完全相同：最终回复必须逐字复制完整 display_markdown，包含表格、覆盖、备选和确认句；不得概述、改写或加引言。

12. MCP 启动失败且缺 uv 时，原样转达安装提示。Windows PowerShell：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`；macOS/Linux：`curl -LsSf https://astral.sh/uv/install.sh | sh`。未经用户同意不要代执行安装。安装后重开终端和你的 Agent。Claude Code 推荐安装完整插件（含 hooks 和本 Skill）；其他 Agent 只接 MCP 也能完整使用，流程写在 MCP instructions 和每个工具返回里。
13. 授权失效时调用 `mcd_start` 获取本机重新授权链接，不向对话索要 Token。授权成功后说“可以说‘帮我配’‘我自己点’或‘外送到公司’”，然后继续原点餐意图，不让用户重复。


## 内容需求、官方比价与付款（0.6.0）
- 你负责“吃什么”：按人数、口味和预算提案；propose_cart 中的套餐 SKU 也是内容需求。引擎会展开精确规格和数量，比较单点、套餐、券、会员卡与拆单。
- quote_best 的 comparison_summary 是解释省钱理由的唯一价格依据；只解释其中实际核验的策略和差价，不编造券、卡优惠或未核价的比较。原样显示报价卡（含比较摘要）。
- 用户明确确认且宿主允许后才 place_orders。真实下单后先原样输出付款卡和“打开付款页”本地链接，再循环调用只读 wait_payment(plan_id,timeout=60)；模拟模式下展示模拟结果后结束，不调用 wait_payment。返回“还没付款”时继续等待；取消或付款成功后结束。临近到期会自动撤回。
- 电脑用手机麦当劳 App 扫一扫（推荐）或微信扫码；手机链接可打开麦当劳 App 付款。必须安装麦当劳 App。备选入口是微信麦当劳小程序 → 我的订单（已见到本插件建的订单，能否直接付款待实测）；麦当劳 App 的订单页目前可能看不到。
- 已付款后告诉用户真实返回的取餐码和门店；没有取餐码就说明还未返回，不能猜。

## 黑话与模拟付款（0.6.1）
- 黑话必须交给 get_menu(search=用户原话) 或 mcd_start 原话处理，按门店当前菜单返回的 selected_items、澄清和缺货提示继续，禁止自己猜商品码或沿用历史价格。穷鬼套餐、四件套、辣堡、双吉、板烧等由包内词典解析；8+4 必须追问；疯狂星期四/V我50 属于肯德基；麦门/金拱门不是餐品。
- **模拟模式（dry_run=true）下展示模拟结果后结束，不调用 wait_payment，不等付款。** 只有真实待付款才在先展示付款卡之后调用只读 wait_payment；不触发写操作审批。临期撤回由后台巡检负责，查询本身不撤单。部分完成时分别告知已付取餐码、已撤回缺少的餐品，重新下单前重新报价并等确认。

## 任何宿主都能用（0.7.4）
- 工具结果末尾有“给 Agent 的下一步”代码块，写着 next_tool 和 next_arguments。宿主只给你看文字时，从这个代码块取参数（session_id、plan_id、confirm_token、store_code、items 等）；这一块不要展示给用户。
- 下单后如果状态是 verifying（核对中），原样输出提示并用 wait_payment 等结论，期间不要重新下单；核对出“其实已经下成了”就按付款卡让用户付款；结论是 place_failed（没下成）就如实转述麦当劳的原话和“没扣钱，可以重新下单”，不要编原因。
