# 麦当劳点餐助手

在 Claude Code 或 Codex 里查门店、选餐、比较同规格价格，用你自己的麦当劳账户点餐。

安装：先准备下面的条件，再复制对应的 Claude 两行或 Codex 三段命令。

装好后说：**我要吃麦当劳**

**默认会真实下单，需要你在对话里说“确认”，Claude Code 或 Codex 弹窗时点允许。** 报价后不会自动下单。想先试用，装好后、启动程序前开启[模拟模式](#先试用模拟模式)。

## 安装前准备

- 有自己的麦当劳账户。首次使用需要到[麦当劳开放平台](https://open.mcd.cn/mcp)，用手机验证码登录并获取 Token，大约 2 分钟；随后粘贴到本机授权页，别发进聊天。
- 已安装并登录 Claude Code 或支持 `codex plugin` 命令的 Codex。
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

模拟模式仍用自己的账户读取门店、菜单和价格，但不会创建订单或支付链接。**报价末尾会写“当前为模拟模式”**；没有这句话就不要确认试单。准备真实使用时，把值改成 `false`，退出程序再重新启动。

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

第一次使用若弹出插件 hooks（自动检查）信任提示，核对插件名为 mcd-best-order 后点“信任/允许”。它会核对下单前最后一条用户消息。下单时仍需在允许弹窗里点允许，不要开启全部允许。首次启动正在准备时，稍候再说一次原话；持续失败请检查 uv 和网络。

## Codex 安装

在终端依次复制以下三段，PowerShell 5 也可以逐段执行：

```text
codex plugin marketplace add kimniniup-creator/mcd-best-order
```

```text
codex plugin add mcd-best-order@mcd-best-order-marketplace
```

```text
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.3-r15-final/coupon_split-0.5.3-1-py3-none-any.whl coupon-split setup --host codex-plugin --apply
```

最后一步准备点餐服务，并仅为本插件设置报价和下单的允许弹窗。完成后新开 Codex 对话。**不要对本插件开启全部允许**；本插件在 Codex 中不能独立核对聊天原文，下单、撤回和支付状态查询都保留逐次允许。缺 uv 或点餐启动失败时，其他 Codex 对话仍可继续。

如果 VS Code 里的 Codex 插件没有插件安装入口，可在终端执行这一段，再新开对话：

```text
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.3-r15-final/coupon_split-0.5.3-1-py3-none-any.whl coupon-split setup --host codex-ide --apply
```

## 连账户，开始点餐

1. 说：`我要吃麦当劳`，按提示选自己点或帮你配，补充位置。完整示例：`我自己点，在北京望京中街附近，到店自取，外带。` 把地名换成你的位置。
2. 第一次会给本机授权页。打开后，去 [麦当劳开放平台](https://open.mcd.cn/mcp) 用手机号和验证码登录，激活并复制自己的 Token。
3. 回本机授权页粘贴，点“保存到这台电脑”。**不要把 Token 发进聊天**。
4. 回对话说“好了”。在点餐页选好，点“选好了，去算最省”，助手自动接着报价。

也可以直接说“来份麦辣鸡腿堡，到店自取，外带，在北京望京中街附近”，或说“帮我配，3个人，一个不吃辣”“两个人，预算60”。宿主会从当前菜单配主推和备选，展示理由及照原样价；回复“就这样”或“换备选”后再算最省价，也可以继续改口味。网页开关只提供简易推荐。推荐使用你自己的 Claude Code/Codex，不需要额外的模型 API key。时间默认现在取；预约时请说清日期和钟点。外送只能选账户里已有的地址。

凭据保存在本机系统凭据管理器；macOS 是钥匙串。其他系统无法使用钥匙串时，会提示仅本人可读的文件位置。凭据不交给作者、不进入聊天或产品日志。失效后按新授权页重新保存。

新对话从空购物车开始；想继续未下单的一份，明确说“继续刚才”。说“照上次”只沿用已创建过订单的那一份；只报价没下单的不算。点餐链接是本机临时链接，不能转发，过期后重新说“我自己点”。

## 确认、付款、撤回

- 先核对报价里的门店、规格、数量、餐具和时间，再回复“确认”。总额超过 ¥200 时，回复报价给出的完整句子，例如 `确认下单 ¥201.00`，金额保留两位小数。随后在允许弹窗里核对并点允许。
- 下单后打开官方支付链接，15 分钟内付款；手机可跳到麦当劳 App。钱直接付给麦当劳。拆成多单时会告诉你要付几次。
- 想撤回就说“撤回订单”，等助手确认已取消及券是否退回。已付款订单请按麦当劳官方规则处理。结果不明时先查状态，不要重复下单。

## 装坏了怎么恢复

如果 Codex 点餐启动异常，在普通终端执行：

```text
codex plugin remove mcd-best-order@mcd-best-order-marketplace
```

然后重开 Codex，可以继续其他对话。补好 uv、重开终端后，再回到“Codex 安装”，依次执行全部三段命令。**第三段 `coupon-split setup --host codex-plugin --apply` 不能漏；漏掉它就没有每次下单时的允许弹窗设置。** 通过 VS Code 里的 Codex 插件专用命令安装的版本，运行 `codex mcp remove coupon-split` 移除。

Claude Code 可以执行 `/plugin uninstall mcd-best-order@mcd-best-order-marketplace`，补好 uv 和网络后重新安装。不会自动替你安装软件。

## 国内访问 GitHub 慢怎么办

首次下载可能需要几分钟。先在浏览器检查能否打开本仓库和 [0.5.3 下载页](https://github.com/kimniniup-creator/mcd-best-order/releases/tag/v0.5.3-r15-final)，确认网络可用后重试。不要从陌生镜像下载，也不需要申请作者私有仓库权限。成功安装的固定版本会优先从已下载的文件启动，之后 GitHub 暂时无法连接也不影响启动；查询菜单和价格仍需要访问麦当劳。

## 卸载与清除凭据

结束点餐对话后，先在终端清除本机凭据：

```text
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.3-r15-final/coupon_split-0.5.3-1-py3-none-any.whl coupon-split forget-credential --apply
```

Claude Code 执行 `/plugin uninstall mcd-best-order@mcd-best-order-marketplace`；Codex 执行上面的 `codex plugin remove ...`。移除插件后，再移除插件市场：Claude Code 执行 `/plugin marketplace remove mcd-best-order-marketplace`；Codex 在终端执行 `codex plugin marketplace remove mcd-best-order-marketplace`。

上面的 `forget-credential --apply` 会清掉助手保存在这台电脑的凭据。清除凭据不会删除购物车；无需保留历史时，可自行删除自己的 `~/.coupon-split/` 目录（或自己设定的 `COUPON_SPLIT_HOME`）。在开放平台重新生成 Token 可以让旧凭据作废。

仅支持中国大陆麦当劳。价格随门店、时段和本人账户变化，搜索次数有限，不保证全局最低。验证码和付款由你本人完成。

版权：保留所有权利，见 LICENSE。本项目与麦当劳无隶属关系。
