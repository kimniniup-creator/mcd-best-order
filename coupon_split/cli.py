"""Explicit, reviewable IDE setup. --apply is required before editing host config."""
import argparse
import shutil
import subprocess
import os
import json
import re
import tomllib
from pathlib import Path

SOURCE='git+https://github.com/kimniniup-creator/mcd-best-order.git'


def configure_codex():
    """Only this plugin's policy is edited; writes retain host approval prompts."""
    root=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))
    root.mkdir(parents=True,exist_ok=True)
    path=root/'config.toml'
    original=path.read_text(encoding='utf-8') if path.exists() else ''
    tomllib.loads(original)
    prefix='plugins."mcd-best-order@mcd-best-order-marketplace".mcp_servers.coupon-split'
    policies={prefix:('default_tools_approval_mode','auto'),prefix+'.tools.quote_best':('approval_mode','approve')}
    for tool in ('place_orders','cancel_plan','pay_card'):
        policies[prefix+'.tools.'+tool]=('approval_mode','prompt')
    existing=tomllib.loads(original).get('plugins',{}).get('mcd-best-order@mcd-best-order-marketplace',{}).get('mcp_servers',{}).get('coupon-split',{})
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


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['setup','forget-credential'])
    parser.add_argument('--host',choices=['codex-ide','codex-plugin','claude-code'])
    parser.add_argument('--apply',action='store_true',help='明确同意写入 MCP 配置和用户 skill')
    args=parser.parse_args()
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
        return
    if args.host=='claude-code':
        raise SystemExit('Claude 必须用完整插件：/plugin marketplace add kimniniup-creator/mcd-best-order，然后 /plugin install mcd-best-order@mcd-best-order-marketplace')
    if args.host=='codex-ide':
        command=['codex','mcp','add','coupon-split','--','uvx','--from',SOURCE,'coupon-split-mcp']
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
    print('完成。请新开宿主会话，说出想吃的餐品。')
