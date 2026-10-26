"""Host-independent guidance shared by MCP and the cold discovery gateway."""
from coupon_split.confirmation import confirm_rule_text, withdraw_rule_text

RISK_NOTICE = '默认会真实下单。报价后你明确确认，真实下单还需要在浏览器确认页（或宿主弹出的确认弹窗，填入实付金额后接受）里再确认一次，之后才会创建待付款订单；Claude Code、Codex 等宿主也一样，多这一次是因为服务端无法证明宿主或模型有没有伪造你的话，也无法相信宿主自报的名字，所以真实下单必须有一次它自己能验证的人工点击。付款由你本人完成。想先试用请开启模拟模式：模拟模式只回复“确认下单”即可，不弹窗、不产生真实订单。回复“确认下单”才会下单；不想要了随时撤回：确认页和付款页都有“撤回这单”按钮，也可以直接告诉我，我会立刻帮你撤回。'

INSTRUCTIONS = '''麦当劳点餐流程（所有 MCP 宿主通用，无需斜杠命令、Skill 或钩子）：
1. 用户一提到想吃或点麦当劳（例如“我要吃麦当劳”“来份板烧”“点个餐”），第一步就调用 mcd_start(utterance=本轮最后一条用户消息的逐字原文)，哪怕地点、方式都还没说；不要先自己追问或回答，缺什么由 mcd_start 的返回告诉你怎么问。同一对话后续每轮复制返回的 session_id；不拼接历史。
2. 工具结果里“给 Agent 的下一步”代码块是给你的参数，不要展示给用户；宿主若只显示文本，就从该块取 next_tool、next_arguments、session_id、plan_id、confirm_token、store_code、items 等。首次向用户说明真实下单风险。未确定点法先问“帮我配还是自己点？”，已有明确餐品不重复问。只补 missing，不擅选外送地址。缺授权只展示本机授权页，绝不索要聊天 Token，不创建地址。
3. Agent 只负责吃什么：依当前官方菜单构思，propose_cart 校验配餐；展示主备选后停等认可。价格、优惠、规格与拆单由引擎 quote_best 计算，不能自行编价或承诺最低。
4. 每步依 next_tool / next_arguments 继续；next_tool=null 表示停止等用户。原样输出 display_markdown，不改写、不删行。点餐页整段链接先在可见 assistant 正文发出，再 get_cart(wait=25,link_displayed=true)；工具结果不算用户可见正文。
5. 方案卡完整展示后结束回合，等用户明确确认。确认后直接 place_orders，不重开 mcd_start。user_confirmation 必须逐字复制用户最后一句原文，不能由 Agent 代造、补金额或从历史拼接；有金额时必须匹配所选报价。超过 ¥200 必须用户完整回复“确认下单 ¥金额”（两位小数），且该原文同时作为 high_amount_confirmation。服务端另有一道由人完成的确认：place_orders 可能先返回 stage=confirm 和确认页链接（或让宿主弹出确认框）。此时先把链接整段原样写进可见正文，再用同样参数再次调用 place_orders 等用户点击；绝不自己打开、提交或代替用户点击确认页。用户点“确认下单”的那一刻订单就会创建，不留等待窗口。@@CONFIRM@@**撤回由你判断，不靠关键词**：用户在任何时候（报价后、确认页、付款前、付款中）表达不想要、取消、改天再说、不吃了、等一下、先别、换个别的、我再想想等不想继续的意思，你必须立刻调用 withdraw_order(plan_id)（或 cancel_plan），并把返回结果如实告诉用户：确认页还没点就作废确认页，订单已建就撤回订单，已付款则转告无法在这里撤回；不用等用户说出特定词，也不要先追问再动手。拿不准用户是不是想撤，就先问“要撤回这单吗”，等回答后再决定。“不要香菜”“换成大杯可乐”“等一下我看看菜单”这类只是修改餐品或看菜单，不是撤回。服务端还留一道兜底：place_orders 收到@@WITHDRAW@@时也会作废确认或撤回，但这只是保险，不能代替你的判断。撤回之后用户再明确说“确认下单”才会重新开始确认。宿主有审批时仍须通过；Claude 钩子是额外一层。
6. 每个与订单有关的返回都带 next_actions：撤回（withdraw_order）、付款（pay_card，付款由用户本人完成）、等待（wait_payment 或等用户说话），按需选用；撤回永远可用。place_orders 返回付款卡后先在可见正文原样输出 display_markdown（包括付款页链接），再循环 wait_payment(plan_id,timeout=60)。未付继续等；已付告知实际取餐码；取消或部分完成如实解释。模拟模式不调用真实写接口，没有有效支付链接，不等待真实付款。
''' + RISK_NOTICE + '\n服务端只能核验收到的原文与报价，无法证明宿主是否伪造用户消息；宿主自报的名称（clientInfo、User-Agent 等）、宿主的自动审批、模型自己写的确认词都不算用户确认，没有任何宿主享受豁免；真实下单只认用户在确认页上的点击，或确认弹窗里勾选并填回与报价一致的实付金额后的接受（拒绝、取消、超时都不算，会改走确认页）。'
INSTRUCTIONS = INSTRUCTIONS.replace('@@CONFIRM@@', confirm_rule_text()).replace('@@WITHDRAW@@', withdraw_rule_text())

FLOW = [
    {'step':'开始','next_tool':'mcd_start','say':'帮我配还是自己点？已有明确餐品时只补缺项。'},
    {'step':'配餐','next_tool':'propose_cart','say':'按当前菜单配好主推和备选，请你选定；不自行编价。'},
    {'step':'报价','next_tool':'quote_best','say':'原样展示方案卡，等你明确确认。'},
    {'step':'确认','next_tool':'place_orders','say':'带上你最后一句确认原文和报价凭证；真实下单会弹出确认弹窗或返回确认页链接：弹窗由用户填入实付金额后接受；链接原样发给用户，等用户点“确认下单”后再次调用。模拟模式直接下单。'},
    {'step':'付款','next_tool':'wait_payment','say':'先原样展示付款卡，然后持续查询；模拟模式不等待付款。'},
    {'step':'撤回','next_tool':'withdraw_order','say':'任何时候用户表达不想要、取消、改天、不吃了、等一下、先别、我再想想，立刻调用并如实告知结果；拿不准先问“要撤回这单吗”。不要香菜、换大杯这类只是改餐品，不用撤回。'},
]
