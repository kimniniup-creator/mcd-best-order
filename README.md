# mcd-best-order · 麦当劳点餐助手

在 Claude Code 或 Codex 里说想吃什么，用自己的麦当劳账户查门店、选餐、比较同规格价格，确认后下单。

```text
你：来份麦辣鸡腿堡，到店自取，外带
助手：照原样 ¥25.00 → 实付 ¥20.00 · 1 单 · 付 1 次
      用券：本人可用券；规格、门店、餐具和时间逐项展示
      回复“确认”后，宿主弹出审批；通过后返回官方支付链接
```

以上为虚构文字示例，实际价格以当前门店和本人账户核价为准。只支持中国大陆麦当劳。

## 安装

先准备已登录的 Claude Code 或 Codex CLI、Git 和 uv。无需自己安装 Python，uv 会准备所需版本。Windows 的 Git 安装选项需将 Git 和 Unix 工具加入 PATH（终端里 `git --version`、`sh --version` 都能运行）。

缺 uv 时启动脚本会给出安装提示，不会自行安装。你同意安装后，在自己的终端运行一行：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS / Linux：

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装后重开终端，确认 `uv --version`。uv 安装来源：[官方安装指南](https://docs.astral.sh/uv/getting-started/installation/)。**首次约 1 分钟**，会下载 Python 和依赖；后续使用固定 0.5.0 wheel 的 uvx 缓存，不再逐会话解析 GitHub 源码并构建。

Claude Code CLI / VS Code 扩展，在 Claude 对话中执行这两行，然后新开对话：

```text
/plugin marketplace add kimniniup-creator/mcd-best-order
/plugin install mcd-best-order@mcd-best-order-marketplace
```

**Claude 必须使用完整插件方式安装。** 单独 MCP + skill 缺少生命周期钩子，网页交接不可靠。首次使用时审阅并信任本插件 hooks；不要关闭下单审批。

Codex CLI，在终端执行这两行，然后新开对话：

```sh
codex plugin marketplace add kimniniup-creator/mcd-best-order
codex plugin add mcd-best-order@mcd-best-order-marketplace && uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.0/coupon_split-0.5.0-py3-none-any.whl coupon-split setup --host codex-plugin --apply
```

第二行自动配置本插件：只让本机报价工具自动通过；下单、撤回、支付状态保留宿主审批。保留其他插件配置，不需手改 TOML。PowerShell 5 不支持 `&&` 时，将第二行中的两条命令分两行运行。需使用支持 `codex plugin` 的新版 CLI；插件安装后重新开启会话。交互式 Codex 弹出读取或工具审批时，**弹出时点允许**。点餐工具自带完整协议，无需通过 shell 读取 SKILL.md。宿主策略参见 [OpenAI 插件文档](https://developers.openai.com/plugins/build/plugins#configure-installed-server-policy)。

Codex IDE 没有插件入口时，可在终端运行以下一条命令，自动注册 MCP 并安装 skill；此路径的交接验证是 CLI 非交互模拟，实际 IDE 面板仍需现场验收：

```sh
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.0/coupon_split-0.5.0-py3-none-any.whl coupon-split setup --host codex-ide --apply
```

公开仓库和 wheel 不需要申请仓库权限。Git/uv 未装时先补齐；遇到 Git 的“找不到命令”先检查 PATH，遇到下载失败先检查网络，不要索要作者私有仓库权限。Claude 第一个会话工具尚未连上时会提示“服务启动中，请稍候再说一次”；稍候重说原话即可。

## 首次授权与点餐

准备好已登录宿主、Git 和 uv 后，从安装到首报价共 6 步（上面两行安装算前两步；PowerShell 5 拆行执行仍为同一安装步骤）：

1. 添加公开 marketplace（上面的第一行）。
2. 安装插件（第二行；Codex 自动 setup 包含在此步）。新开对话。
3. 说“我要吃麦当劳”，打开助手给的 **本机授权页**。
4. 去 `open.mcd.cn` 的 MCP 页面，手机验证码登录，激活并复制自己的 Token。
5. 回本机授权页粘贴，点“保存到这台电脑”，确认连接成功。空值、无效或过期会显示原因。
6. 回对话说“好了，我自己点，在某城市某地标附近，到店自取，外带”，打开点餐页选好并提交；助手自动给首报价，不再要求重复说明。可以把地点、餐具、预约一起说。

不要把 Token 发进聊天。Token 过期时会给出重新授权链接，按以上步骤重新保存。每位用户使用自己的账户和券。Windows 存入凭据管理器；macOS 存入钥匙串。其他系统无可用钥匙串时才使用仅本人可读的本机凭据文件，页面会说明保存位置。Token 仅用于访问麦当劳官方服务，不上传到插件仓库或作者服务器，不记入聊天和产品日志。

“我自己点”会打开本机网页；选好点“选好了，去算最省”，助手会继续报价。如果没接上，说“好了”恢复。网页随机端口只在会话运行时可用，购物车可恢复24小时，不要转发本机点餐链接。外送只能选已有地址；没有可配送门店时换已有地址或改到店自取。

## 确认、付款与撤回

默认允许真实下单，但必须同时满足：对话中明确确认、有效的10分钟确认凭证、宿主审批通过、下单前餐品数量/规格/金额复核通过。报价后不会自动下单。**总额超过 ¥200，还要追加回复“确认下单 ¥实际金额”，保留两位小数。** 宿主没有审批能力或拒绝审批时停止，不能绕过。

下单后支付卡包含麦当劳官方支付链接和手机 App 跳转链接，15分钟内付款。钱直接付给麦当劳，我们不经手。多单会明确说明需付几次。想撤回就在对话说“撤回订单”，等待已取消及退券核验；已支付订单不能作为待付单自动撤回。结果不明时先核对状态，切勿再次下单。

演示时在启动宿主前设置 `COUPON_SPLIT_SIMULATE=true`（PowerShell：`$env:COUPON_SPLIT_SIMULATE='true'`），重启会话即可模拟，不创建订单或支付链接。旧环境变量 `ALLOW_REAL_ORDERS=false` 同样关闭真实下单。

## 常见问题

- **装不上 / 找不到 uv 或 sh**：先检查 `git --version`、`sh --version`、`uv --version`；重开终端。Git 未装时先从 [git-scm.com](https://git-scm.com/downloads) 安装。市场报错时检查网络和插件名，重新执行 marketplace add，再 install / add；不要把命令里的市场名改成私有仓库。Claude 可用 `/plugin` 查看 Errors，Codex 可用 `codex plugin list` 检查。
- **授权失败**：确认复制完整 Token；已过期就在开放平台重新生成，再回本机页保存。连接服务失败稍后再试；Windows 凭据管理器不可用时先修复 keyring，不把凭据发给助手。
- **没有可配送门店**：换账户内已有地址，或改到店自取；插件不创建地址。
- **想撤回订单**：说“撤回订单”，等助手确认状态；券可能稍后退回，要以核验结果为准。
- **隐私与地区**：凭据只保存在本机；仅支持中国大陆麦当劳。不要转发点餐/支付链接或本机状态目录。
- **限制**：价格随门店、时段、账户券变化；候选搜索有次数上限，不保证全局最低。验证码、官方支付由你自己完成。

## 卸载与清除本机数据

先结束正在点餐的会话。在卸载插件之前，清除本机凭据：

```sh
uvx --from https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.0/coupon_split-0.5.0-py3-none-any.whl coupon-split forget-credential --apply
```

Claude 对话执行 `/plugin uninstall mcd-best-order@mcd-best-order-marketplace`；Codex 终端执行 `codex plugin remove mcd-best-order@mcd-best-order-marketplace`。清除凭据不会删除购物车；确认不需要历史后，手动删除自己的 `~/.coupon-split/`（或自己设置的 `COUPON_SPLIT_HOME`）。Codex 的本插件策略表可保留，不会在卸载后运行。去开放平台重新生成 Token 可使旧 Token 作废。

版权：保留所有权利，见 LICENSE。项目与麦当劳无隶属关系。

当前为 0.5.0 验收候选版。引擎层真实自测已通过：到店单品一单、取得官方支付卡、不付款、立即撤回，订单列表只读确认已取消。账户当时无可用券，退券闭环未验证。宿主审批框留给同事在交互界面验收；引擎层检查不代替交互审批验收。
