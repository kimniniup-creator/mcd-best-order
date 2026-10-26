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
    for tool in ('wait_payment','pay_card'):policies[prefix+'.tools.'+tool]=('approval_mode','approve')
    for tool in ('place_orders','cancel_plan'):
        policies[prefix+'.tools.'+tool]=('approval_mode','prompt')
    config=tomllib.loads(original)
    existing=(config.get('plugins',{}).get('mcd-best-order@mcd-best-order-marketplace',{}) if plugin else config).get('mcp_servers',{}).get('coupon-split',{})
    if existing.get('default_tools_approval_mode')=='auto' and all(existing.get('tools',{}).get(tool,{}).get('approval_mode')=='approve' for tool in ('quote_best','propose_cart','wait_payment','pay_card')) and all(existing.get('tools',{}).get(tool,{}).get('approval_mode')=='prompt' for tool in ('place_orders','cancel_plan')):
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
    logging.getLogger('mcp').setLevel(logging.ERROR)
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def check():
        params=StdioServerParameters(command=sys.executable,args=['-m','coupon_split.stdio'],env=dict(os.environ))
        async with stdio_client(params,errlog=quiet) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools=await session.list_tools()
                if not any(tool.name=='mcd_start' for tool in tools.tools):raise RuntimeError('缺少点餐入口')
    print('正在准备点餐服务，首次需要下载依赖；不会查询账户或下单。',flush=True)
    try:
        with open(os.devnull,"w") as quiet:
            asyncio.run(asyncio.wait_for(check(),timeout=180))
    except Exception:raise SystemExit('点餐服务启动检查失败；请检查 uv、Git 和网络后重新执行安装第二行。') from None
    print('点餐服务已准备好。请新开 Codex 会话，说“我要吃麦当劳”。')


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
        preflight()
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
        print('本机麦当劳凭据已清除。')
        return
    if not args.host:parser.error('setup/uninstall 需要 --host')
    try:
        setup(ALIASES.get(args.host,args.host),args.apply,args.action=='uninstall',args.project,args.home,args.config)
    except (ValueError,OSError) as exc:
        raise SystemExit('未修改配置：'+str(exc)) from None
    return


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
