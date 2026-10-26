"""Load the shipped ordering protocol for Claude's direct-MCP routing path.

No network, credentials or state writes. Unrelated prompts produce no context.
"""
import json
from pathlib import Path
import re
import sys
import os
import time
import sqlite3


def wait_for_host_mcp(timeout=8):
    """Stdlib-only hook: wait for the host's own tools/list handshake."""
    started=time.time()
    path=Path(os.environ.get('COUPON_SPLIT_HOME',Path.home()/'.coupon-split'))/'mcp-ready.json'
    until=time.monotonic()+timeout
    while True:
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            if value.get('phase')=='tools_listed' and value.get('started',0)>=started-3:
                time.sleep(.02)
                return True
        except (OSError,ValueError):pass
        if time.monotonic()>=until:return False
        time.sleep(.01)


def startup_context():
    ready=wait_for_host_mcp()
    message=('点餐工具已连接。' if ready else '点餐工具尚未连接，可能仍在启动或启动失败；请检查 uv 和网络，再重开对话。')+'工具可用后直接调用 mcd_start，按返回协议继续；全程中文，无需 shell 读取 SKILL.md。工具尚未出现时说“服务启动中，请稍候再说一次”；不要编造已连接或报价。'
    return {'hookSpecificOutput':{'hookEventName':'SessionStart','additionalContext':message}}


def context_for(prompt):
    if not re.search(r'好了|选好了|继续刚才|接着点刚才那个|点餐|麦当劳|想吃|要吃|外卖|外送|自取|堂食|外带|帮我配|推荐一下|随便来点|预算|忌口|不吃|[一二两三四五六七八九十\d]+个?人|自己点|取餐',prompt):return {}
    skill=(Path(__file__).resolve().parent.parent/'skills/mcd-order/SKILL.md').read_text(encoding='utf-8')
    return {'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':skill}}


def transcript_file(name):
    # Windows may receive a legitimate host transcript longer than MAX_PATH.
    # Do not turn an unreadable transcript into repeated false link denials.
    name=os.path.abspath(name)
    if os.name=='nt' and not name.startswith('\\\\?\\'):
        name='\\\\?\\UNC\\'+name[2:] if name.startswith('\\\\') else '\\\\?\\'+name
    return Path(name)


def turn_text(value):
    visible=[];all_text=[]
    try:
        for line in transcript_file(value.get('transcript_path','')).read_text(encoding='utf-8').splitlines():
            try:entry=json.loads(line)
            except ValueError:continue
            content=entry.get('message',{}).get('content',[])
            if entry.get('type')=='user' and not entry.get('isMeta') and (isinstance(content,str) or any(b.get('type')=='text' for b in content if isinstance(b,dict))):
                visible=[];all_text=[]
            all_text.append(json.dumps(content,ensure_ascii=False))
            if entry.get('type')=='assistant' and isinstance(content,list):
                visible.extend(b.get('text','') for b in content if isinstance(b,dict) and b.get('type')=='text')
    except OSError:pass
    return visible,'\n'.join(all_text).replace('\\/','/')


def link_instruction(url):
    return '用户看不到任何工具结果。下一条 assistant 必须先有 text 块逐字输出下面一行，不能说“链接在上面”，不能只写 thinking。然后 tool_use get_cart(wait=25)。不需要用户授权，你可立即修正顺序后重试；禁止向用户解释流程检查。\n[打开点餐页]('+url+') — 选好会自动接着算；如果没接上，回来说一句“好了”。'


def local_picker_url(sid):
    """A pending tool result may not yet be flushed to the host transcript."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',sid):return None
    root=Path(os.environ.get('COUPON_SPLIT_HOME',Path.home()/'.coupon-split'))
    try:
        value=json.loads((root/'bridge.json').read_text(encoding='utf-8'))
        url=value.get('url','')
        if re.fullmatch(r'http://127\.0\.0\.1:\d+',url):return url+'/order/'+sid
    except (OSError,ValueError):pass
    return None


def handle(value):
    if value.get('hook_event_name')=='PreToolUse':
        # Only this plugin's two side-effect-free payment reads bypass a prompt.
        # Never allow a different server's same-named tool or any write tool.
        if re.fullmatch(r'mcp__(?:plugin_mcd-best-order_coupon-split|coupon-split)__(?:wait_payment|pay_card)',value.get('tool_name','')):
            return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'allow','permissionDecisionReason':'只读查询付款状态，不下单、不撤回。'}}
        if value.get('tool_name','').endswith('__place_orders'):
            return order_guard(value)
        if not value.get('tool_name','').endswith('__get_cart'):return {}
        args=value.get('tool_input',{})
        if not args.get('wait',0):return {}
        path=value.get('transcript_path')
        if not path:return {}
        sid=args.get('session_id','');visible,raw=turn_text(value)
        if sid and not any('/order/'+sid in text and '[打开点餐页]' in text for text in visible):
            match=re.search(r'http://127\.0\.0\.1:\d+/order/'+re.escape(sid),raw)
            url=match[0] if match else local_picker_url(sid)
            reason=link_instruction(url) if url else '先原样输出最近工具的 display_markdown 为 assistant text 正文，再调用 get_cart(wait=25)，无需用户授权。'
            return {'hookSpecificOutput':{'hookEventName':'PreToolUse','additionalContext':reason}}
        return {}
    if value.get('hook_event_name')=='Stop':
        # A print-mode host may treat its link text as a final answer. Keep that
        # same turn alive once, after the visible message, so the promised wait
        # and webpage handback can run. Quotes and unrelated turns pass through.
        text=value.get('last_assistant_message','')
        visible,raw=turn_text(value)
        urls=re.findall(r'http://127\.0\.0\.1:\d+/order/[A-Za-z0-9_-]+',raw)
        if not value.get('stop_hook_active') and urls and not any(urls[-1] in t for t in visible) and '对得上你点的' not in text:
            return {'decision':'block','reason':link_instruction(urls[-1])}
        if not value.get('stop_hook_active') and re.search(r'http://127\.0\.0\.1:\d+/order/[A-Za-z0-9_-]+',text):
            return {'decision':'block','reason':'点餐页链接已在用户可见正文中展示。现在继续本回合，立即调用 get_cart(session_id=该链接最后一段, wait=25)，收到网页提交后自动 quote_best；不要等新的用户消息，不重发链接，也不要把这段内部续跑指令复述给用户。'}
        return {}
    return context_for(value.get('prompt',''))


def last_user_message(path):
    """Read human text only: assistant text, tool results and meta are untrusted."""
    latest=None
    for line in transcript_file(path).read_text(encoding='utf-8').splitlines():
        try:entry=json.loads(line)
        except ValueError:continue
        if entry.get('type')!='user' or entry.get('isMeta'):continue
        content=entry.get('message',{}).get('content')
        if isinstance(content,str):latest=content
        elif isinstance(content,list):
            text=[b.get('text','') for b in content if isinstance(b,dict) and b.get('type')=='text']
            if text:latest='\n'.join(text)
    return latest


# Same pattern as coupon_split.confirmation.NEGATIVE (a test keeps them identical): a retraction is never blocked here,
# the server turns it into a refusal or a withdrawal.
NEGATIVE = re.compile(
    r'先别|别(?:下|买|点|订|了|急|等|动)|不(?:要了|下|买|订|点了|用了|必了|想(?:要|下|买)?了|确认|同意|行|对)|不要[。！!\s]*$|先不|暂时不|暂不'
    r'|取消|算了|撤回|撤销|反悔|改主意|等等|等一下|等会|稍等|先等|停一下|停下|想想|考虑一下|再看看'
    r'|\b(?:no|nope|cancel|stop|wait|never\s*mind|hold\s*on|don\'?t|do\s*not)\b', re.I)


def order_guard(value):
    """Fail closed using the transcript and read-only local plan, never model prose."""
    def deny(reason):
        return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':reason}}
    def ask(reason):
        return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'ask','permissionDecisionReason':reason}}
    if not value.get('transcript_path'):
        # DeepSeek Harness and similar hosts give hooks no transcript: hand the decision to the person
        # (the server still asks for confirmation itself) instead of blocking every order.
        return ask('宿主没有提供对话记录，无法核对你的确认原话。请你本人核对方案和金额后选择是否允许下单。')
    try:
        try:text=last_user_message(value.get('transcript_path',''))
        except OSError:return ask('读不到对话记录，无法核对你的确认原话。请你本人核对方案和金额后选择是否允许下单。')
        # Whole-message matching rejects questions, quotes and negated consent.
        if text and NEGATIVE.search(text) and value.get('tool_input',{}).get('user_confirmation')==text:
            return {}
        normalized=re.sub(r'[。！!，,\s]','',text or '')
        if not re.fullmatch(r'(?:确认(?:下单)?|同意下单|就这样下单)(?:[¥￥]?\d+\.\d{2})?',normalized):
            return deny('最后一条用户消息没有明确确认下单，请先展示报价并等待用户确认。')
        args=value.get('tool_input',{})
        if args.get('user_confirmation')!=text:
            return deny('确认参数必须逐字等于最后一条用户消息，不能由助手代写。')
        db=Path(os.environ.get('COUPON_SPLIT_HOME',Path.home()/'.coupon-split'))/'plans.sqlite3'
        with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as conn:
            row=conn.execute('SELECT body FROM plans WHERE id=?',(args.get('plan_id'),)).fetchone()
        if not row:return deny('找不到本机报价，请重新报价后确认。')
        plan=json.loads(row[0])
        selected=next(p for p in plan['plans'] if p['option_id']==args.get('option_id','0'))
        if selected['price']>20000:
            expected=f"确认下单 ¥{selected['price']/100:.2f}"
            if text!=expected or args.get('high_amount_confirmation')!=text:
                return deny('金额超过 ¥200，请用户追加回复：'+expected)
    except (OSError,ValueError,KeyError,TypeError,StopIteration,sqlite3.Error):
        return deny('无法核对最后一条用户消息或报价，停止下单；请重新报价并明确确认。')
    return {}


if __name__=='__main__':
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    value=startup_context() if sys.argv[1:]==['startup'] else handle(json.load(sys.stdin))
    print(json.dumps(value,ensure_ascii=False))
