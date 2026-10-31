"""Reviewable host adapters. Official source links live beside each profile.

Only the coupon-split entry is replaced. No credentials or approval bypasses are
installed. JSONC is parsed strictly after lexical comment/trailing comma removal;
the original bytes are always backed up before a changed configuration is saved.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
import uuid

NAME = 'coupon-split'
SOURCE = 'https://github.com/kimniniup-creator/mcd-best-order/releases/download/v0.9.2/coupon_split-0.9.2-py3-none-any.whl'
SOURCES = {
    # Paths and formats checked against each vendor's documentation.
    'dsh':'https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md',
    'kiro':'https://kiro.dev/docs/mcp/configuration/',
    'cursor':'https://cursor.com/docs/context/mcp',
    'vscode':'https://code.visualstudio.com/docs/agents/reference/mcp-configuration',
    'windsurf':'https://docs.windsurf.com/windsurf/cascade/mcp',
    'cline':'https://docs.cline.bot/mcp/mcp-overview',
    'roo':'https://docs.roocode.com/features/mcp/using-mcp-in-roo',
    'gemini':'https://geminicli.com/docs/tools/mcp-server/',
    'claude-desktop':'https://modelcontextprotocol.io/docs/develop/connect-local-servers',
    'claude-code':'https://code.claude.com/docs/en/plugins',
    'codex-plugin':'https://developers.openai.com/codex/plugins',
    'codex-ide':'https://developers.openai.com/codex/mcp',
    'generic':'https://modelcontextprotocol.io/specification/latest/basic/transports',
}
ALIASES={'deepseek-harness':'dsh','gemini-cli':'gemini','vscode-copilot':'vscode','roo-code':'roo'}


def stdio_entry():
    # uv discovers/downloads Python 3.14, even on a colleague's cold computer.
    return {'command':shutil.which('uvx') or 'uvx',
            'args':['--no-config','--managed-python','--python','3.14','--from',SOURCE,'coupon-split-mcp'], 'env':{}}


def config_path(host, project=None, home=None):
    project=Path(project or Path.cwd()); home=Path(home or Path.home())
    # Prefer project scope where documented; global-only hosts can use --config.
    projects={'kiro':'.kiro/settings/mcp.json','cursor':'.cursor/mcp.json',
              'vscode':'.vscode/mcp.json','roo':'.roo/mcp.json',
              'gemini':'.gemini/settings.json','claude-code':'.mcp.json'}
    if host=='dsh':return dsh_home(home)/'profiles'/DSH_PROFILE/'cordis.patch.yml'
    if host in projects:return project/projects[host]
    if host.startswith('codex-'):
        return (Path(os.environ['CODEX_HOME']) if not home_explicit(home) and os.environ.get('CODEX_HOME') else home/'.codex')/'config.toml'
    if host=='cline':
        return Path(os.environ.get('CLINE_MCP_SETTINGS_PATH',home/'.cline/data/settings/cline_mcp_settings.json'))
    if host=='windsurf':
        # Legacy Windsurf uses .codeium; current docs redirect to Devin Cascade.
        legacy=home/'.codeium/windsurf/mcp_config.json'
        if legacy.exists() or (home/'.codeium/windsurf').exists():return legacy
        return app_data(home)/'devin/mcp_config.json' if sys.platform=='win32' else Path(os.environ.get('XDG_CONFIG_HOME',home/'.config'))/'devin/mcp_config.json'
    if host=='claude-desktop':
        if sys.platform=='win32':return app_data(home)/'Claude/claude_desktop_config.json'
        if sys.platform=='darwin':return home/'Library/Application Support/Claude/claude_desktop_config.json'
        raise ValueError('Claude Desktop 官方支持 macOS/Windows；Linux 请用 --host generic。')
    raise ValueError('此宿主没有文件目标')


DSH_PROFILE='mcd'
BLOCK_START='# >>> coupon-split (由 coupon-split setup 管理；用 coupon-split uninstall --host dsh 移除) >>>'
BLOCK_END='# <<< coupon-split <<<'


def dsh_home(home):
    """DSH_HOME as the host defines it; an explicit --home (isolated tests) always wins."""
    if not home_explicit(home) and os.environ.get('DSH_HOME'):return Path(os.environ['DSH_HOME'])
    return home/'.dsh'


def dsh_block(entry=None):
    """Managed Cordis patch rows. MCD_MCP_TOKEN is forwarded explicitly: dsh strips KEY/TOKEN/SECRET/PASSWORD vars."""
    env=("Object.fromEntries(Object.entries({ PYTHONUTF8: '1', COUPON_SPLIT_SIMULATE: process.env.COUPON_SPLIT_SIMULATE ?? 'true', "
         "MCD_MCP_TOKEN: process.env.MCD_MCP_TOKEN, COUPON_SPLIT_HOME: process.env.COUPON_SPLIT_HOME, ALLOW_REAL_ORDERS: process.env.ALLOW_REAL_ORDERS, "
         "UV_CACHE_DIR: process.env.UV_CACHE_DIR, PYTHON_KEYRING_BACKEND: process.env.PYTHON_KEYRING_BACKEND }).filter(([, v]) => typeof v === 'string'))")
    skills=("(process.env.DSH_HOME || ((process.env.USERPROFILE || process.env.HOME) + '/.dsh')) + '/profiles/"+DSH_PROFILE+"/skills'")
    return f"""{BLOCK_START}
- insert:
    # MCP（stdio）。MCD_MCP_TOKEN 只读环境变量，绝不写进本文件。
    # COUPON_SPLIT_SIMULATE 未设置时默认 true（模拟，不产生真实订单）；真实下单请在启动 dsh 前设为 false。
    - id: mcp-coupon-split
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: coupon-split
        transport: stdio
        command: uv
        args:
          - tool
          - run
          - --python
          - '3.14'
          - --from
          - {SOURCE}
          - coupon-split-mcp
        env: !!js "{env}"
        toolCallTimeoutMs: 120000
        failOnStartupError: false
    # 本 profile 的技能目录（不扫默认根，不影响其他 profile）。
    - id: mcd-skill-filesystem
      name: '@deepseek-ai/dsh-skill-filesystem'
      config:
        providerName: mcd-order
        includeDefaultRoots: false
        customSkillDirs:
          - !!js "{skills}"
        watch: true
{BLOCK_END}
"""


PROFILE_FILES={
    'package.json':json.dumps({'name':'dsh-profile-'+DSH_PROFILE,'private':True,'dependencies':{},
        'dsh':{'profile':{'bundles':['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app']}}},indent=2)+'\n',
    'cordis.yml':'# dsh profile root — an empty entry list. Edit cordis.patch.yml, not this file.\n[]\n',
    'pnpm-workspace.yaml':'packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n',
}


def skill_source():
    here=Path(__file__).parent
    for candidate in (here/'assets/skills/mcd-order/SKILL.md',here.parent/'skills/mcd-order/SKILL.md'):
        if candidate.exists():return candidate
    raise ValueError('找不到随包提供的 mcd-order SKILL.md')


def patch_without_block(text):
    pattern=re.compile(r'\n?'+re.escape(BLOCK_START)+r'.*?'+re.escape(BLOCK_END)+r'\n?',re.S)
    return pattern.sub('\n',text).strip('\n')


def render_dsh_patch(original,uninstall=False):
    rest=patch_without_block(original)
    body=re.sub(r'(?m)^\s*#.*$','',rest).strip()
    if uninstall:return (rest+'\n') if rest else ''
    if body in ('','[]'):
        # Fresh or empty profile patch: ours is the only content (comments kept).
        comments='\n'.join(l for l in rest.splitlines() if l.strip().startswith('#'))
        return (comments+'\n' if comments else '')+dsh_block()
    if not body.startswith('-'):
        raise ValueError('cordis.patch.yml 不是 YAML 列表，保留原文件；请用 setup --host generic 并手动合并')
    return rest+'\n\n'+dsh_block()


def setup_dsh(apply=False,uninstall=False,home=None,config=None):
    home=Path(home or Path.home())
    patch=Path(config) if config else config_path('dsh',None,home)
    profile=patch.parent
    skill=profile/'skills'/'mcd-order'/'SKILL.md'
    plan=[]  # (path, new text, description)
    old=patch.read_text(encoding='utf-8-sig') if patch.exists() else ''
    new=render_dsh_patch(old,uninstall)
    if new!=old:plan.append((patch,new,'移除本插件配置块' if uninstall else '写入本插件配置块'))
    if uninstall:
        if skill.exists():plan.append((skill,None,'移除本插件 Skill'))
    else:
        source=skill_source().read_text(encoding='utf-8')
        if not skill.exists() or skill.read_text(encoding='utf-8')!=source:plan.append((skill,source,'写入 Skill'))
        for name,text in PROFILE_FILES.items():
            if not (profile/name).exists():plan.append((profile/name,text,'新建独立 profile 文件'))
    print(('移除' if uninstall else '配置')+' dsh（DeepSeek Harness）独立 profile “'+DSH_PROFILE+'” → '+str(profile))
    print('来源：'+SOURCES['dsh']+' ；profile/patch 格式见 https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md')
    print('只处理本 profile 目录；不修改 DSH_HOME 下其他 profile、settings 或凭据。')
    if not plan:print('无需改动。');return
    for path,text,why in plan:print(' - '+why+'：'+str(path))
    if not apply:print('仅预览，尚未写入。添加 --apply 才会写入，已有文件先备份。');return
    for path,text,why in plan:
        if (path.is_symlink()):raise ValueError('拒绝写入符号链接：'+str(path))
        if path.exists():
            backup=path.with_name(path.name+'.coupon-split-'+uuid.uuid4().hex+'.bak')
            with backup.open('xb') as f:f.write(path.read_bytes())
            print('已备份：'+str(backup))
        if text is None:
            path.unlink()
            for folder in (path.parent,path.parent.parent):
                try:folder.rmdir()
                except OSError:pass
            continue
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('w',encoding='utf-8',newline='\n') as f:f.write(text)
    if uninstall:
        print('已移除。profile 目录和其中其他文件保留；不需要时可手动删除 '+str(profile)+'。')
    else:
        print('已写入。启动：dsh --profile '+DSH_PROFILE+'（需要先 export/set MCD_MCP_TOKEN；Token 不写进文件）。')
        print('默认是模拟模式；真实下单请在启动 dsh 前设置 COUPON_SPLIT_SIMULATE=false。dsh 不弹审批，下单前服务端会给你一个浏览器确认页。')


def home_explicit(home):return home.resolve()!=Path.home().resolve()


def app_data(home):
    return Path(os.environ.get('APPDATA',home/'AppData/Roaming')) if not home_explicit(home) else home/'AppData/Roaming'


def jsonc(text):
    # Never remove comment markers inside quoted strings (URLs, paths, etc.).
    tokens=re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    clean=tokens.sub(lambda m:m[0] if m[0].startswith('"') else ' ',text)
    clean=re.sub(r'("(?:\\.|[^"\\])*"|,\s*(?=[}\]]))',lambda m:m[0] if m[0].startswith('"') else '',clean)
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError('配置有重复 JSON 键；保留原文件，请先核对')
            out[key]=value
        return out
    return json.loads(clean or '{}',object_pairs_hook=pairs)


def codex_block(entry):
    # required = true: Codex starts its MCP servers while the first turn is already running; a required server is awaited, so the first
    # turn has the tools (cold uvx environments: 3/3 with it, 2/3 without). Cost: if the server cannot start Codex says so instead of
    # silently going on without it; `setup --uninstall` removes the entry.
    text='[mcp_servers.coupon-split]\nstartup_timeout_sec = 120\nrequired = true\n'
    for key in ('command','args'):text+=key+' = '+json.dumps(entry[key],ensure_ascii=False)+'\n'
    text+='[mcp_servers.coupon-split.env]\n'
    for key,value in entry['env'].items():text+=json.dumps(key)+' = '+json.dumps(value)+'\n'
    return text


def remove_toml(text, prefix):
    # Refuse unusual inline/quoted definitions rather than accidentally alter them.
    blocks=re.split(r'(?=^\[)',text,flags=re.M)
    return ''.join(b for b in blocks if not (b.startswith('['+prefix+']') or b.startswith('['+prefix+'.')))


def render(host, original, entry, uninstall=False):
    if host.startswith('codex-'):
        data=tomllib.loads(original)
        prefix='plugins."mcd-best-order@mcd-best-order-marketplace".mcp_servers.coupon-split' if host=='codex-plugin' else 'mcp_servers.coupon-split'
        text=remove_toml(original,prefix)
        if not uninstall:
            if host=='codex-plugin':
                text+='\n['+prefix+']\ndefault_tools_approval_mode = "auto"\n'
            else:text+='\n'+codex_block(entry)
            for tool in ('resolve_order','quote_best','propose_cart','wait_payment','pay_card','withdraw_order','place_orders','cancel_plan'):
                from coupon_split.humanconfirm import human_confirm_enabled
                mode='prompt' if tool=='cancel_plan' or (tool=='place_orders' and human_confirm_enabled()) else 'approve'  # internal-tool mode: nothing to ask the person at place_orders
                text+='\n['+prefix+'.tools.'+tool+']\napproval_mode = '+json.dumps(mode)+'\n'
        tomllib.loads(text)
        return text
    data=jsonc(original)
    if not isinstance(data,dict):raise ValueError('配置根必须是对象')
    key='servers' if host=='vscode' else 'mcpServers'
    servers=data.setdefault(key,{})
    if not isinstance(servers,dict):raise ValueError('宿主 MCP 配置必须是对象')
    if uninstall:servers.pop(NAME,None)
    else:servers[NAME]=dict(entry,**({'type':'stdio'} if host=='vscode' else {}))
    return json.dumps(data,ensure_ascii=False,indent=2)+'\n'


def detect(project=None,home=None):
    home=Path(home or Path.home());project=Path(project or Path.cwd())
    commands={'dsh':['dsh'],'kiro':['kiro','kiro-cli'],'cursor':['cursor','agent'],
              'vscode':['code'],'windsurf':['windsurf'],'cline':['cline'],
              'gemini':['gemini'],'claude-code':['claude'],'codex-ide':['codex']}
    dirs={'dsh':['.dsh'],'kiro':['.kiro'],'cursor':['.cursor'],'windsurf':['.codeium/windsurf'],
          'claude-desktop':['AppData/Roaming/Claude','Library/Application Support/Claude']}
    out=[]
    extensions=list((home/'.vscode/extensions').glob('*'))
    for host in SOURCES:
        if host=='generic':continue
        evidence=[]
        for cmd in commands.get(host,[]):
            path=shutil.which(cmd)
            if path:evidence.append('command:'+cmd)
        try:
            if config_path(host,project,home).exists():evidence.append('config')
        except ValueError:pass
        if any((home/d).is_dir() for d in dirs.get(host,[])):evidence.append('directory')
        ext={'roo':'rooveterinaryinc.roo-cline-','cline':'saoudrizwan.claude-dev-','vscode':'github.copilot-'}
        if host in ext and any(p.name.lower().startswith(ext[host]) for p in extensions):evidence.append('extension')
        out.append({'host':host,'detected':bool(evidence),'evidence':evidence})
    return out


def codex_skill_path(home=None,config=None):
    """User skill directory of Codex ($CODEX_HOME/skills). Verified: a bare 我要吃麦当劳 reliably finds the tools only with it."""
    if config:return Path(config).parent/'skills'/'mcd-order'/'SKILL.md'
    home=Path(home or Path.home())
    root=Path(os.environ['CODEX_HOME']) if not home_explicit(home) and os.environ.get('CODEX_HOME') else home/'.codex'
    return root/'skills'/'mcd-order'/'SKILL.md'


def sync_skill(path,apply,uninstall):
    """Install/remove our own Skill file only; never overwrite a different file without a backup."""
    source=skill_source().read_text(encoding='utf-8')
    exists=path.exists()
    current=path.read_text(encoding='utf-8') if exists else None
    if uninstall:
        if not exists or current!=source:
            print('Skill：'+('已不存在。' if not exists else '内容已被你改过，保留不动：'+str(path)));return
        print('移除 Skill：'+str(path))
        if apply:
            path.unlink()
            for folder in (path.parent,path.parent.parent):
                try:folder.rmdir()
                except OSError:pass
        return
    if current==source:print('Skill 已是最新：'+str(path));return
    print(('更新' if exists else '写入')+' Skill：'+str(path))
    if not apply:return
    path.parent.mkdir(parents=True,exist_ok=True)
    if exists:
        backup=path.with_name(path.name+'.coupon-split-'+uuid.uuid4().hex+'.bak')
        backup.write_bytes(path.read_bytes());print('已备份：'+str(backup))
    path.write_text(source,encoding='utf-8',newline='\n')


def setup(host,apply=False,uninstall=False,project=None,home=None,config=None,entry=None,skill=True):
    result=_setup_config(host,apply,uninstall,project,home,config,entry)
    if host=='codex-ide' and skill:
        # Codex needs the Skill to pick up a vague first sentence (the tools can be discovered late).
        sync_skill(codex_skill_path(home,config),apply,uninstall)
    return result


def _setup_config(host,apply=False,uninstall=False,project=None,home=None,config=None,entry=None):
    entry=copy.deepcopy(entry or stdio_entry())
    if host=='generic':
        print(json.dumps({'mcpServers':{NAME:entry}},ensure_ascii=False,indent=2))
        print('把它粘到你的 Agent 的 MCP 配置里。')
        return
    if host=='dsh':
        return setup_dsh(apply,uninstall,home,config)
    path=Path(config) if config else config_path(host,project,home)
    if path.is_symlink():raise ValueError('拒绝写入符号链接配置；请给出实际文件路径')
    old=path.read_bytes() if path.exists() else b''
    text=old.decode('utf-8-sig')
    updated=render(host,text,entry,uninstall)
    # Compare parsed semantics so repeated setup does not churn JSONC/comments.
    same=updated==text
    if host.startswith('codex-'):same=tomllib.loads(text)==tomllib.loads(updated)
    if host not in ('codex-ide','codex-plugin'):
        same=jsonc(text)==json.loads(updated)
    print(('移除' if uninstall else '配置')+' '+host+' → '+str(path))
    print('来源：'+SOURCES[host])
    # Show only owned entry, never dump existing configuration secrets.
    print(json.dumps({'remove':NAME} if uninstall else entry,ensure_ascii=False,indent=2))
    if same or (uninstall and not path.exists()):print('无需改动。');return
    if not apply:print('仅预览，尚未写入。添加 --apply 才会写入，已有文件先备份。');return
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        backup=path.with_name(path.name+'.coupon-split-'+uuid.uuid4().hex+'.bak')
        with backup.open('xb') as f:f.write(old)
        if os.name!='nt':backup.chmod(0o600)
        print('已备份：'+str(backup))
    # Refuse a concurrent edit after the preview was computed.
    if (path.read_bytes() if path.exists() else b'')!=old:raise ValueError('配置已被其他程序修改，请重新预览')
    with path.open('w',encoding='utf-8',newline='\n') as f:f.write(updated)
    if os.name!='nt':path.chmod(0o600)
    print('已写入。重启宿主或刷新 MCP，然后调用 mcd_start。')
    if host=='codex-plugin':print('此项只配置已安装插件的审批；插件仍按 README 的 marketplace 命令安装。')
    if host=='claude-code':print('已配置项目 MCP；若需要插件 Skill 和钩子，仍可按 README 安装插件，避免同时启用两份 MCP。')
