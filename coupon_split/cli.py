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

SOURCE='https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.5.0/coupon_split-0.5.0-1-py3-none-any.whl'


def configure_codex(plugin=True):
    """Only this plugin's policy is edited; writes retain host approval prompts."""
    root=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))
    root.mkdir(parents=True,exist_ok=True)
    path=root/'config.toml'
    original=path.read_text(encoding='utf-8') if path.exists() else ''
    tomllib.loads(original)
    prefix='plugins."mcd-best-order@mcd-best-order-marketplace".mcp_servers.coupon-split' if plugin else 'mcp_servers.coupon-split'
    policies={prefix:('default_tools_approval_mode','auto'),prefix+'.tools.quote_best':('approval_mode','approve')}
    for tool in ('place_orders','cancel_plan','pay_card'):
        policies[prefix+'.tools.'+tool]=('approval_mode','prompt')
    config=tomllib.loads(original)
    existing=(config.get('plugins',{}).get('mcd-best-order@mcd-best-order-marketplace',{}) if plugin else config).get('mcp_servers',{}).get('coupon-split',{})
    if existing.get('default_tools_approval_mode')=='auto' and existing.get('tools',{}).get('quote_best',{}).get('approval_mode')=='approve' and all(existing.get('tools',{}).get(tool,{}).get('approval_mode')=='prompt' for tool in ('place_orders','cancel_plan','pay_card')):
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
    print('已配置：报价自动通过；下单、撤回和支付状态工具请求宿主审批。')


def preflight():
    """Prepare the MCP entry point before a cold Codex conversation starts."""
    import asyncio
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def check():
        params=StdioServerParameters(command=sys.executable,args=['-m','coupon_split.stdio'],env=dict(os.environ))
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools=await session.list_tools()
                if not any(tool.name=='mcd_start' for tool in tools.tools):raise RuntimeError('缺少点餐入口')
    print('正在准备点餐服务，首次需要下载依赖；不会查询账户或下单。',flush=True)
    try:asyncio.run(asyncio.wait_for(check(),timeout=180))
    except Exception:raise SystemExit('点餐服务启动检查失败；请检查 uv、Git 和网络后重新执行安装第二行。') from None
    print('点餐服务已准备好。请新开 Codex 会话，说“我要吃麦当劳”。')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['setup','doctor','forget-credential'])
    parser.add_argument('--host',choices=['codex-ide','codex-plugin','claude-code'])
    parser.add_argument('--apply',action='store_true',help='明确同意写入 MCP 配置和用户 skill')
    args=parser.parse_args()
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
    if not args.host:parser.error('setup 需要 --host')
    if args.host=='codex-plugin':
        if not args.apply:raise SystemExit('需 --apply 同意：仅配置本插件报价自动通过及写工具审批。')
        configure_codex()
        preflight()
        return
    if args.host=='claude-code':
        raise SystemExit('Claude 请用插件安装：/plugin marketplace add kimniniup-creator/mcd-best-order，然后 /plugin install mcd-best-order@mcd-best-order-marketplace')
    if args.host=='codex-ide':
        command=['codex','mcp','add','coupon-split','--','uvx','--python','3.14','--from',SOURCE,'coupon-split-mcp']
        target=Path.home()/'.agents/skills/mcd-order/SKILL.md'
    else:
        command=['claude','mcp','add','--scope','user','coupon-split','--','uvx','--from',SOURCE,'coupon-split-mcp']
        target=Path.home()/'.claude/skills/mcd-order/SKILL.md'
    print('将写入本人宿主 MCP 配置，并安装 skill：',target)
    print(subprocess.list2cmdline(command))
    if not args.apply:
        print('尚未修改配置。确认这些改动后，添加 --apply 重新运行。')
        return
    source=Path(__file__).parent/'assets/skills/mcd-order/SKILL.md'
    if not source.exists():source=Path(__file__).parent.parent/'skills/mcd-order/SKILL.md'
    if target.exists() and target.read_bytes()!=source.read_bytes():
        raise SystemExit('已有不同版本 skill，保留原文件；请先自行核对版本。')
    executable=shutil.which(command[0])
    if not executable:raise SystemExit('未找到宿主命令，请先安装宿主 CLI。')
    subprocess.run([executable,*command[1:]],check=True)
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target)
    if args.host=='codex-ide':
        # Server-scoped readiness and auth forwarding; never alter other MCPs.
        path=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'config.toml'
        text=path.read_text(encoding='utf-8')
        header='[mcp_servers.coupon-split]'
        block='required = true\nstartup_timeout_sec = 120\nenv_vars = ["MCD_MCP_TOKEN", "COUPON_SPLIT_HOME", "ALLOW_REAL_ORDERS", "COUPON_SPLIT_SIMULATE", "UV_CACHE_DIR", "PYTHON_KEYRING_BACKEND"]\n'
        # codex mcp add just created this section; remove only owned settings
        # so repeated setup remains valid TOML and keeps existing credentials.
        sections=[]
        for section in re.split(r'(?=^\[)',text,flags=re.M):
            if section.splitlines() and section.splitlines()[0].strip()==header:
                section=re.sub(r'^(?:required|startup_timeout_sec|env_vars)\s*=.*\n?', '', section,flags=re.M)
                section=section.replace(header,header+'\n'+block,1)
            sections.append(section)
        text=''.join(sections);tomllib.loads(text);path.write_text(text,encoding='utf-8')
        configure_codex(plugin=False)
        preflight()
    print('完成。请新开宿主会话，说出想吃的餐品。')
