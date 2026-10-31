"""Explicit, reviewable IDE setup. --apply is required before editing host config."""
import argparse
import shutil
import subprocess
import os
import json
import re
import tomllib
import sys
from pathlib import Path

from coupon_split.hosts import SOURCE


def configure_codex(plugin=True):
    """Only this plugin's policy is edited; writes retain host approval prompts."""
    root=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))
    root.mkdir(parents=True,exist_ok=True)
    path=root/'config.toml'
    original=path.read_text(encoding='utf-8') if path.exists() else ''
    tomllib.loads(original)
    before=original
    if not plugin:
        blocks=[]
        for block in re.split(r'(?=^\[)',original,flags=re.M):
            if block.splitlines() and block.splitlines()[0].strip()=='[mcp_servers.coupon-split]':
                block=re.sub(r'^required\s*=.*$', 'required = false',block,flags=re.M)
            blocks.append(block)
        original=''.join(blocks)
    prefix='plugins."mcd-best-order@mcd-best-order-marketplace".mcp_servers.coupon-split' if plugin else 'mcp_servers.coupon-split'
    policies={prefix:('default_tools_approval_mode','auto'),prefix+'.tools.quote_best':('approval_mode','approve')}
    policies[prefix+'.tools.propose_cart']=('approval_mode','approve')
    for tool in ('wait_payment','pay_card','withdraw_order'):policies[prefix+'.tools.'+tool]=('approval_mode','approve')
    for tool in ('place_orders','cancel_plan'):
        policies[prefix+'.tools.'+tool]=('approval_mode','prompt')
    config=tomllib.loads(original)
    existing=(config.get('plugins',{}).get('mcd-best-order@mcd-best-order-marketplace',{}) if plugin else config).get('mcp_servers',{}).get('coupon-split',{})
    if existing.get('default_tools_approval_mode')=='auto' and all(existing.get('tools',{}).get(tool,{}).get('approval_mode')=='approve' for tool in ('quote_best','propose_cart','wait_payment','pay_card','withdraw_order')) and all(existing.get('tools',{}).get(tool,{}).get('approval_mode')=='prompt' for tool in ('place_orders','cancel_plan')):
        if original!=before:path.write_text(original,encoding='utf-8')
        print('本插件报价与写操作审批策略已配置。')
        return
    sections=[]
    for block in re.split(r'(?=^\[)',original,flags=re.M):
        header=block.splitlines()[0].strip() if block.strip() else ''
        if header in {'['+section+']' for section in policies}:
            block=re.sub(r'^(?:default_tools_approval_mode|approval_mode)\s*=.*\n?', '', block,flags=re.M)
        sections.append(block)
    text=''.join(sections)
    for section,(key,value) in policies.items():
        header='['+section+']';setting=key+' = '+json.dumps(value)+'\n'
        if header in text:text=text.replace(header,header+'\n'+setting,1)
        else:text+='\n'+header+'\n'+setting
    tomllib.loads(text)
    path.write_text(text,encoding='utf-8')
    print('已配置：报价自动通过；下单和撤回请求Claude Code/Codex 允许弹窗。')


def preflight():
    """Prepare the MCP entry point before a cold Codex conversation starts."""
    import asyncio
    import logging
    import tempfile
    logging.getLogger('mcp').setLevel(logging.ERROR)
    async def check():
        params=StdioServerParameters(command=sys.executable,args=['-m','coupon_split.stdio'],env=dict(os.environ))
        async with stdio_client(params,errlog=quiet) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools=await session.list_tools()
                if not any(tool.name=='mcd_start' for tool in tools.tools):raise RuntimeError('缺少点餐入口')
    print('正在准备点餐服务，首次需要下载依赖；不会查询账户或下单。',flush=True)
    try:
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryFile(mode='w+',encoding='utf-8',errors='replace') as quiet:
            try:asyncio.run(asyncio.wait_for(check(),timeout=180))
            finally:
                quiet.seek(0)
                worker_error=quiet.read()
    except Exception as exc:
        from coupon_split.windows_mcp import blocked_component
        detail=blocked_component(exc)
        if not detail:detail=blocked_component(ImportError(locals().get('worker_error','')))
        raise SystemExit(detail or '点餐服务启动检查失败；请检查 uv、Git 和网络后重新执行安装第二行。') from None
    from coupon_split.windows_mcp import FAILURES,blocked_component
    for detail in dict.fromkeys(blocked_component(exc) for exc in FAILURES):
        if detail:print(detail+' 本次已跳过可选的 Windows Job Object，点餐服务可继续使用。')
    print('点餐服务已准备好。请新开 Codex 会话，说“我要吃麦当劳”。')


def doctor():
    """Probe the shipped launcher with the inherited host PATH, offline only."""
    ok=True
    for name,fix in (('node','安装 Node.js LTS：https://nodejs.org/'),
                     ('uv','Windows PowerShell：powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"；macOS/Linux：curl -LsSf https://astral.sh/uv/install.sh | sh')):
        resolved=shutil.which(name)
        if resolved:print(name+'：'+resolved)
        else:
            print('缺少 '+name+'，宿主无法启动插件。修复：'+fix+'；随后完全退出并重开 Agent。')
            ok=False
    print('插件启动和 hooks 不需要 sh；其他宿主的 setup 配置直接运行 uvx，不依赖 Node 或项目 .venv。')
    if not ok:return False
    root=Path(__file__).resolve().parent
    launcher=next((p for p in (root/'assets/hooks/launch.cjs',root.parent/'hooks/launch.cjs') if p.exists()),None)
    if launcher is None:
        print('缺少随包启动器。修复：重新安装最新版插件和 coupon-split。')
        return False
    try:
        reply=subprocess.run([shutil.which('node'),str(launcher),'probe'],capture_output=True,text=True,encoding='utf-8',timeout=20)
        if reply.returncode:raise ValueError(reply.stderr.strip())
        value=json.loads(reply.stdout)
        if value['prefix']!=value['base_prefix']:raise ValueError('启动器错误地选择了虚拟环境')
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
        print('启动依赖检查失败：'+str(exc))
        print('修复：运行 uv python install 3.14，再运行 coupon-split doctor；若报系统应用控制拦截，请修复 uv 托管 Python 安装。不要使用当前项目的 .venv 代替。')
        return False
    print('启动解释器：'+value['executable']+'（基础解释器，未使用当前项目 .venv）。')
    print('启动依赖检查通过；未联网、未安装软件、未连接账户。若宿主仍报找不到命令，请完全退出并重开 Agent，让它继承相同 PATH；旧插件仍调用 sh 时请更新插件。')
    return True


def main():
    parser=argparse.ArgumentParser()
    from coupon_split.hosts import SOURCES, ALIASES, setup, detect
    parser.add_argument('action',choices=['setup','uninstall','serve','doctor','diagnose','forget-credential'])
    parser.add_argument('--host',choices=[*SOURCES,*ALIASES])
    parser.add_argument('--apply',action='store_true',help='明确同意写入 MCP 配置和用户 skill')
    parser.add_argument('--detect',action='store_true',help='只读探测宿主，不写入配置')
    parser.add_argument('--project',type=Path,help='项目配置的目标工作区，默认当前目录')
    parser.add_argument('--home',type=Path,help='隔离测试用的宿主 home')
    parser.add_argument('--config',type=Path,help='显式配置文件路径；扩展版 Cline 请用其界面打开的路径')
    parser.add_argument('--http',action='store_true',help='启动仅监听本机的 Streamable HTTP 服务')
    parser.add_argument('--out',type=Path,help='diagnose 导出目录，默认用户主目录')
    parser.add_argument('--port',type=int,default=0,help='HTTP 端口，默认随机可用端口')
    args=parser.parse_args()
    if args.action=='serve':
        if not args.http:parser.error('serve 需要 --http（stdio 请用 coupon-split-mcp）')
        if not 0<=args.port<=65535:parser.error('端口必须在 0–65535')
        serve_http(args.port)
        return
    if args.detect:
        if args.action!='setup':parser.error('--detect 只用于 setup')
        print(json.dumps(detect(args.project,args.home),ensure_ascii=False,indent=2))
        return
    if args.action=='diagnose':
        from coupon_split.diagnostics import export
        from coupon_split.local import state_dir
        target=export(state_dir(),args.out)
        print('已导出（按白名单字段生成，不含 Token、订单号、券码、地址、门店、手机号、姓名、付款链接）：'+str(target))
        print('发给开发前请先打开看一眼：里面只有状态、商品码、数量、时间、耗时、错误码和原因标签（如 rate-limited），没有麦当劳的原话、地址、门店、订单号或券码。')
        return
    if args.action=='doctor':
        if not doctor():raise SystemExit(1)
        return
    if args.action=='forget-credential':
        if not args.apply:raise SystemExit('需 --apply 同意清除本机凭据；不会删除点餐状态。')
        import keyring
        from keyring.errors import PasswordDeleteError
        try:keyring.delete_password('coupon-split','upstream')
        except PasswordDeleteError:pass
        from coupon_split.local import state_dir
        file=state_dir()/'credential'
        if file.exists():file.unlink()
        from coupon_split.local import forget_amap_key
        forget_amap_key()
        print('本机麦当劳凭据和高德 Key 已清除。')
        return
    if not args.host:parser.error('setup/uninstall 需要 --host')
    try:
        setup(ALIASES.get(args.host,args.host),args.apply,args.action=='uninstall',args.project,args.home,args.config)
    except (ValueError,OSError) as exc:
        raise SystemExit('未修改配置：'+str(exc)) from None
    if args.apply and args.action=='setup' and args.host!='generic' and not os.environ.get('COUPON_SPLIT_NO_WARM'):
        warm_start()
    return


def warm_start():
    """Build the MCP environment once, now. Codex starts its MCP servers while the first turn is already running, so a cold
    first start (download, environment build) can outlast the turn's first step; with the environment cached it takes seconds."""
    import subprocess
    from coupon_split.hosts import stdio_entry
    entry=stdio_entry()
    print('预热点餐服务（只拉取并构建运行环境，不连接麦当劳，不读写账户）……',flush=True)
    try:
        subprocess.run([entry['command'],*entry['args']],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
        print('预热完成。')
    except (OSError,subprocess.TimeoutExpired):
        print('预热没有完成（不影响使用，第一次启动会慢一些）。')


def serve_http(port=0):
    """Bind before advertising the endpoint; only a local bearer can use MCP."""
    import socket
    import uvicorn
    os.environ['COUPON_SPLIT_HTTP']='1'
    from coupon_split.server import app, LOCAL_KEY
    sock=socket.socket()
    sock.bind(('127.0.0.1',port))
    sock.listen(128)
    actual=sock.getsockname()[1]
    print(json.dumps({'transport':'streamable-http','url':f'http://127.0.0.1:{actual}/mcp',
                      'headers':{'Authorization':'Bearer '+LOCAL_KEY}},ensure_ascii=False),flush=True)
    print('仅本机可用；访问令牌不要发到聊天或提交到仓库。SSE-only 宿主需使用兼容桥接，不能把 /mcp 当作旧版 /sse。',file=sys.stderr)
    try:uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=actual,access_log=False,log_level='warning')).run(sockets=[sock])
    finally:sock.close()


if __name__=='__main__':main()
