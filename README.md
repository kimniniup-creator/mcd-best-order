# 麦当劳点餐助手（内部工具）

装好后对 Agent 说一句“我要吃麦当劳”，之后直接说想吃什么：你会拿到最低价和付款链接，付款即确认。

```text
Claude Code：claude plugin marketplace add kimniniup-creator/mcd-best-order; claude plugin install mcd-best-order@mcd-best-order-marketplace
Codex：codex plugin marketplace add kimniniup-creator/mcd-best-order; codex plugin add mcd-best-order@mcd-best-order-marketplace; uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.9.1/coupon_split-0.9.1-py3-none-any.whl coupon-split setup --host codex-plugin --apply
```

需要先装 uv 并有自己的麦当劳账户（首次使用按提示把 Token 粘到本机授权页，别发进聊天）；详细步骤见下文。

支持任何能接 MCP 的 Agent，Claude Code 和 Codex 有一键安装。查门店、选餐、比较同规格价格，用你自己的麦当劳账户点餐。

安装：先准备下面的条件，再复制对应的 Claude 两行或 Codex 三段命令。

同事安装的是公开仓库 [mcd-best-order](https://github.com/kimniniup-creator/mcd-best-order)（下面的命令已经指向它的发布包）；不要克隆私有开发仓库 coupon-split，没有权限，会报 exit 128。

装好后说：**我要吃麦当劳**

**默认直接下单：报价后立刻建单，付款即确认，不付款的单由麦当劳超时自动取消，想撤回随时说“不要了”。** 管理员可设环境变量 `COUPON_SPLIT_HUMAN_CONFIRM=true` 恢复人工确认页，`COUPON_SPLIT_MAX_ORDER_YUAN` 调整单次总额上限（默认 300 元，超过须回复“确认下单 ¥金额”）。

下文凡提到确认页、确认弹窗、“确认下单”金额句的地方，只在 `COUPON_SPLIT_HUMAN_CONFIRM=true` 时适用；默认的内部工具模式没有这一步。

## 其他 Agent（Cursor / Kiro / DeepSeek Harness……）

对你的 Agent 说：**“按 https://github.com/kimniniup-creator/mcd-best-order 的 AGENTS.md 帮我安装。”**

也可自己运行（先预览，核对目标后再加 `--apply`）：

```sh
uvx --python 3.14 --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.9.1/coupon_split-0.9.1-py3-none-any.whl coupon-split setup --host cursor
```

Codex 的 IDE/CLI 一键方式是 `--host codex-ide`（同时装 Skill，建议使用）；没有 Skill 的宿主上，笼统的一句话可能不会立刻触发点餐工具，可以说得具体些（例如“我要吃麦当劳，来份板烧”）。

把 `cursor` 换成 `dsh`、`kiro`、`vscode`、`windsurf`、`cline`、`roo`、`gemini` 或 `claude-desktop`。`setup --detect` 只读探测；`setup --host generic` 给其他宿主标准 MCP 配置；`uninstall --host <name> --apply` 对称移除本条目。详见 [Agent 安装与自检](docs/install-for-agents.md)。无需 Skill 或斜杠命令；本机 HTTP 用 `serve --http`。DeepSeek Harness（dsh）会在 `~/.dsh/profiles/mcd` 建一个独立 profile，默认模拟模式，用 `dsh --profile mcd` 启动；dsh 没有审批弹窗，下单前服务端会给你一个浏览器确认页。

## 安装前准备

- 有自己的麦当劳账户。首次使用需要到[麦当劳开放平台](https://open.mcd.cn/mcp)，用手机验证码登录并获取 Token，大约 2 分钟；随后粘贴到本机授权页，别发进聊天。
- 已安装并登录能接 MCP 的 Agent。使用下方插件安装时需 Claude Code 或支持 `codex plugin` 的 Codex。
- 已安装 uv。无需另装 Python，uv 会准备所需版本。各系统任选对应的一行：

  Windows PowerShell：

  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

  macOS：

  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  Linux：

  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  **安装后重开终端**，运行 `uv --version` 确认。
- 能访问 GitHub；第一次安装需要下载文件。
- 已安装 [Git](https://git-scm.com/downloads)。Windows 安装时选择把 Git 和 Unix 工具加入 PATH；`git --version`、`sh --version` 都应能运行。

## 先试用：模拟模式

**先完成下面的安装，再退出 Claude Code/Codex。** 打开终端，执行对应的设置命令，随后在这个终端输入 `claude` 或 `codex` 启动，新开对话。

Windows PowerShell：

```powershell
$env:COUPON_SPLIT_SIMULATE = 'true'
claude  # 用 Codex 的人改为 codex
```

Windows cmd：

```bat
set COUPON_SPLIT_SIMULATE=true
claude
```

macOS / Linux：

```sh
export COUPON_SPLIT_SIMULATE=true
claude  # 用 Codex 的人改为 codex
```

桌面版和 VS Code 里的 Codex 插件暂不支持按这份指南开启模拟模式，请用上述终端启动方式试用。

模拟模式仍用自己的账户读取门店、菜单和价格，但不会创建真实订单或有效支付链接。**报价末尾会写“当前为模拟模式”**；没有这句话就不要确认试单。GUI 或会清理环境的宿主，请在本插件 MCP 配置的 `env` 明确设置 `COUPON_SPLIT_SIMULATE: "true"` 和 `ALLOW_REAL_ORDERS: "false"`。准备真实使用时撤销这两个模拟开关并重启服务。

## Claude Code 安装

在 Claude Code 对话里分两次输入以下命令，每次只粘贴一行并回车，等第一行执行完成后再输入第二行。不要两行一起粘贴。

第 1 步，添加插件市场：

```text
/plugin marketplace add kimniniup-creator/mcd-best-order
```

第 2 步，安装插件：

```text
/plugin install mcd-best-order@mcd-best-order-marketplace
```

两步都完成后，新开对话。

第一次使用若弹出插件 hooks（自动检查）信任提示，核对插件名为 mcd-best-order 后点“信任/允许”。它会核对下单前最后一条用户消息。wait_payment 和 pay_card 只读查询无需逐次允许。下单和撤回时仍需在允许弹窗里点允许，不要开启全部允许。首次启动正在准备时，稍候再说一次原话；持续失败请检查 uv 和网络。

## Codex 安装

在终端依次复制以下三段，PowerShell 5 也可以逐段执行：

```text
codex plugin marketplace add kimniniup-creator/mcd-best-order
```

```text
codex plugin add mcd-best-order@mcd-best-order-marketplace
```

```text
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.9.1/coupon_split-0.9.1-py3-none-any.whl coupon-split setup --host codex-plugin --apply
```

最后一步准备点餐服务，并仅为本插件配置工具权限。完成后新开 Codex 对话。**不要对本插件开启全部允许**；本插件在 Codex 中不能独立核对聊天原文，下单和撤回仍保留逐次允许；wait_payment 和 pay_card 只读查询无需逐次允许。缺 uv 或点餐启动失败时，其他 Codex 对话仍可继续。

如果 VS Code 里的 Codex 插件没有插件安装入口，可在终端执行这一段，再新开对话：

```text
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.9.1/coupon_split-0.9.1-py3-none-any.whl coupon-split setup --host codex-ide --apply
```

## 连账户，开始点餐

1. 说：`我要吃麦当劳`，按提示选自己点或帮你配，补充位置。完整示例：`我自己点，在北京望京中街附近，到店自取，外带。` 把地名换成你的位置。
2. 第一次会给本机授权页。打开后，去 [麦当劳开放平台](https://open.mcd.cn/mcp) 用手机号和验证码登录，激活并复制自己的 Token。
3. 回本机授权页粘贴，点“保存到这台电脑”。**不要把 Token 发进聊天**。
4. 回对话说“好了”。在点餐页选好，点“选好了，去算最省”，助手自动接着报价。

也可以直接说“来份麦辣鸡腿堡，到店自取，外带，在北京望京中街附近”，或说“帮我配，3个人，一个不吃辣”“两个人，预算60”。宿主会从当前菜单配主推和备选，展示理由及照原样价；回复“就这样”或“换备选”后再算最省价，也可以继续改口味。网页开关只提供简易推荐。推荐使用你自己的 Agent，不需要额外的模型 API key。时间默认现在取；预约时请说清日期和钟点。外送只能选账户里已有的地址。

凭据保存在本机系统凭据管理器；macOS 是钥匙串。其他系统无法使用钥匙串时，会提示仅本人可读的文件位置。凭据不交给作者、不进入聊天或产品日志。失效后按新授权页重新保存。

新对话从空购物车开始；想继续未下单的一份，明确说“继续刚才”。说“照上次”只沿用已创建过订单的那一份；只报价没下单的不算。点餐链接是本机临时链接，不能转发，过期后重新说“我自己点”。

## 确认、付款、撤回

- 先核对报价里的门店、规格、数量、餐具和时间，再回复“确认下单”。总额超过 ¥200 时，回复报价给出的完整句子，例如 `确认下单 ¥201.00`，金额保留两位小数。**<!--confirm-rule-->只有这四种标准确认句才算确认：“确认下单”“确认”“同意下单”“就这样下单”（后面可带金额，如“确认下单 ¥10.00”，超过 ¥200 时必须带金额；金额必须是规范写法：可选一个货币符号 ¥ ￥ RMB CNY，整数加最多两位小数，可选一个单位元或块，不能有千分位逗号、多个符号或多个小数点，格式不对不算确认，改在确认页点确认下单；前面可带“好的”“好”“嗯嗯”“嗯”“对”“是的”“OK”“okay”，后面可带“谢谢你”“谢谢”“多谢”“谢了”“麻烦了”“辛苦了”“吧”），其余话一律不算确认，服务端只提示用户回复“确认下单”。<!--/confirm-rule-->**“好了”“可以”“OK”、带条件或带问号的话都不算，不会下单。真实下单随后还要你本人在确认弹窗或确认页里再确认一次，Claude Code、Codex 等所有宿主都一样：宿主支持弹窗时，勾选“确认下单”并填入实付金额再接受；拒绝、取消、超时或宿主不支持，就改给你一个本机浏览器确认页，点“确认下单”。多这一次，是因为宿主自报的名字和模型填写的“确认”都可以伪造，服务端只能验证你本人的这次点击或填写。模拟模式不需要，回复“确认下单”即可演示。**你点“确认下单”的那一刻订单就创建，不用再回对话说话，中间没有等待窗口。** 没有这一次点击就不会下单，Agent 自己写“确认”不算数。
- 下单前后改主意：由 Agent 判断。你在任何时候表达不想要、取消、改天、不吃了、等一下、先别、我再想想，Agent 会立刻调用撤回工具并如实告诉你结果（拿不准会先问“要撤回这单吗”）；“不要香菜”“换成大杯可乐”只是改餐品，不会撤回。确认页和付款页都有“撤回这单”按钮，点一下即撤回，不经任何语言判断。服务端另有兜底：只认含义明确的强词（取消、撤回、撤单、撤销、撤订、退单、退订、不要了、不要这单、别下单、不下了、不点了、不买了、算了，英文 cancel、stop（“no onions please”不会被误撤），含繁体和同音错别字），命中时：订单还没建，确认页作废，之后你重新说“确认下单”才会重新开始；订单已建成，立刻撤回并告诉你结果（已撤回 / 正在核对中，一出现就撤回 / 已付款无法撤回，需要在小程序申请退款）。“不要”“等等”“稍等”“改一下”“换一下”这类弱词不会触发兜底，只靠 Agent 判断。下单只认确认白名单或确认页点击，这一条不变。
- 必须安装麦当劳 App。下单后打开本机付款页，在 15 分钟内付款；电脑用手机里的麦当劳 App 扫一扫（推荐）或微信扫码，手机可点“打开麦当劳 App 付款”。拆单时按页面提示逐笔支付，钱直接付给麦当劳。
- 页面与对话助手会自动等待并更新状态；临近到期仍未付会自动撤回。备选入口是微信麦当劳小程序 → 我的订单（已见到本插件建的订单，能否直接付款待实测；麦当劳 App 的订单页目前可能看不到），这个入口及本人真实付款、付款后取餐码待手机实测。
- 本机付款页需要点餐服务保持运行。手机中的 localhost 指手机自己，不能直接打开电脑的本机链接；电脑用户请直接用手机扫码，不要复制本机链接到手机。
- 助手负责配餐内容，引擎按相同规格和数量比较原样、套餐、券、会员卡和拆单。省钱理由以报价的比较摘要为准；有界搜索不承诺穷尽所有买法。
- 下单后如果麦当劳明确拒绝（比如“太火爆了”“商品已售罄”），会直接告诉你原话，并说明没生成订单、没扣钱，可以稍后重下。网络没拿到结果时会进入“核对中”，约 2 分钟内对照你的麦当劳订单给结论：其实已经下成了就请你付款，没下成就告诉你可以重新下单。
- 想撤回就说“撤回订单”，等助手确认已取消及券是否退回。已付款订单请按麦当劳官方规则处理。结果不明时先查状态，不要重复下单。

## 出问题怎么办

下单或撤回出了问题，在终端执行下面的命令，导出一份诊断文件，发给开发。文件按白名单字段生成（状态、商品码和数量、时间、耗时、错误码、原因标签如 `rate-limited`），不含 Token、订单号、券码、地址、门店名和地址、手机号、姓名、付款链接，也不复制麦当劳的原话；发之前自己打开看一眼：

```text
uvx --python 3.14 --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.9.1/coupon_split-0.9.1-py3-none-any.whl coupon-split diagnose
```

文件保存在用户主目录，名为 `mcd-order-diagnosis-<时间>.json`。本机的 `orders.log`（在点餐状态目录里，不会被导出原文）每次下单、撤回只记录耗时、HTTP 状态、麦当劳的错误码和说明、traceId。

## 宿主已知限制

- **Codex 无头模式（`codex exec`）会“挂起”，原因在宿主和模型服务，不在本插件**（0.7.3 隔离实测，Codex 0.162.1）：
  1. 从脚本、CI 或别的 Agent 里启动 `codex exec "<提示>"` 时，如果标准输入是一根没有关闭的管道，Codex 会在日志里写 `Reading additional input from stdin...` 并一直等输入结束，整轮不开始。启动时务必关闭标准输入：Windows PowerShell 不支持 `<` 重定向（会报 RedirectionNotSupported），要交给 cmd：`cmd /c "codex exec ... < NUL"`（本机 PowerShell 5.1 实测可用；`$null | codex exec ...` 实测会送进一个 BOM 字符，不算真正关闭）；macOS/Linux 用 `codex exec ... < /dev/null`；Python 用 `stdin=subprocess.DEVNULL`。
  2. 模型服务返回 `429 Too Many Requests`（`exceeded retry limit, last status: 429`）时，`--json` 输出里只有 `thread.started`、`turn.started`，之后什么都没有，也不报错；表现就是“卡住”。这是账号或模型的限流或容量问题。换一个可用的模型、稍后重试，脚本里给每一轮设超时并重试。
  本插件已经把单次工具等待控制在宿主超时以内（`get_cart` 约 25 秒，`wait_payment` 最多 60 秒，确认页等待约 50 秒），隔离实测 Codex 在 20、58、65、125 秒的工具调用里都正常返回；无头模式下 Codex 对 MCP 确认弹窗（elicitation）立即回复“拒绝”，本插件随即给出本机确认页，不会卡住。
- **Claude Code 无头模式（`claude -p`）** 无法弹出 `place_orders` 要求的宿主批准，所以走不到下单这一步；请在交互界面使用。
- 真实下单在所有宿主（Claude Code、Codex、Cursor、dsh 等）下都会多一次确认：支持确认弹窗（elicitation）的宿主先弹窗，勾选“确认下单”并填入实付金额再接受；拒绝、取消、超时或不支持，就改走本机浏览器确认页，点击后立即下单。没有任何宿主可以跳过。

## 装坏了怎么恢复

如果 Codex 点餐启动异常，在普通终端执行：

```text
codex plugin remove mcd-best-order@mcd-best-order-marketplace
```

然后重开 Codex，可以继续其他对话。补好 uv、重开终端后，再回到“Codex 安装”，依次执行全部三段命令。**第三段 `coupon-split setup --host codex-plugin --apply` 不能漏；漏掉它就没有每次下单时的允许弹窗设置。** 通过 VS Code 里的 Codex 插件专用命令安装的版本，运行 `codex mcp remove coupon-split` 移除。

Claude Code 可以执行 `/plugin uninstall mcd-best-order@mcd-best-order-marketplace`，补好 uv 和网络后重新安装。不会自动替你安装软件。

## 国内访问 GitHub 慢怎么办

首次下载可能需要几分钟。先在浏览器检查能否打开本仓库和 [0.9.1 下载页](https://github.com/kimniniup-creator/mcd-best-order/releases/tag/v0.9.1)，确认网络可用后重试。不要从陌生镜像下载，也不需要申请作者私有仓库权限。成功安装的固定版本会优先从已下载的文件启动，之后 GitHub 暂时无法连接也不影响启动；查询菜单和价格仍需要访问麦当劳。

## 卸载与清除凭据

结束点餐对话后，先在终端清除本机凭据：

```text
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.9.1/coupon_split-0.9.1-py3-none-any.whl coupon-split forget-credential --apply
```

Claude Code 执行 `/plugin uninstall mcd-best-order@mcd-best-order-marketplace`；Codex 执行上面的 `codex plugin remove ...`。移除插件后，再移除插件市场：Claude Code 执行 `/plugin marketplace remove mcd-best-order-marketplace`；Codex 在终端执行 `codex plugin marketplace remove mcd-best-order-marketplace`。

上面的 `forget-credential --apply` 会清掉助手保存在这台电脑的凭据。清除凭据不会删除购物车；无需保留历史时，可自行删除自己的 `~/.coupon-split/` 目录（或自己设定的 `COUPON_SPLIT_HOME`）。在开放平台重新生成 Token 可以让旧凭据作废。

仅支持中国大陆麦当劳。价格随门店、时段和本人账户变化，搜索次数有限，不保证全局最低。验证码和付款由你本人完成。

版权：保留所有权利，见 LICENSE。本项目与麦当劳无隶属关系。

支持“穷鬼套餐”“四件套”等点餐俗称，按所选门店当前菜单确认餐品和价格；歧义词会先问清。比较口径：官方渠道最优价，比照原样下单省 ¥X。模拟模式不等待付款；真实下单后先展示付款页，再只读查询付款状态。
