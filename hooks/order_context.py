"""Load the shipped ordering protocol for Claude's direct-MCP routing path.

No network, credentials or state writes. Unrelated prompts produce no context.
"""
import json
from pathlib import Path
import re
import sys
import os


def context_for(prompt):
    if not re.search(r'好了|选好了|点餐|麦当劳|想吃|要吃|外卖|外送|自取|堂食|外带|帮我配|自己点|取餐',prompt):return {}
    skill=(Path(__file__).resolve().parent.parent/'skills/mcd-order/SKILL.md').read_text(encoding='utf-8')
    return {'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':skill}}


def turn_text(value):
    visible=[];all_text=[]
    try:
        for line in Path(value.get('transcript_path','')).read_text(encoding='utf-8').splitlines():
            try:entry=json.loads(line)
            except ValueError:continue
            content=entry.get('message',{}).get('content',[])
            if entry.get('type')=='user' and (isinstance(content,str) or any(b.get('type')=='text' for b in content if isinstance(b,dict))):
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
            return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':reason}}
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


if __name__=='__main__':
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    value=handle(json.load(sys.stdin))
    print(json.dumps(value,ensure_ascii=False))
