"""Run with: uv run python -m coupon_split.server"""
import asyncio
import copy
import contextlib
import contextvars
import io
import logging
import os
from pathlib import Path
import secrets
import time

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context
from pydantic import create_model, ConfigDict, Field
from typing import Literal, Annotated
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import FileResponse, Response, JSONResponse, HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from coupon_split.optimizer import DEFAULT_CART, atom, validate_quantities
from coupon_split.elements import OrderElements, normalize_elements, merge_elements, utterance_elements
from coupon_split.menu import menu_rows, matches, intent_text, resolve_intent, default_item, recommend_rows, AmbiguousIntent
from coupon_split.presentation import markdown, public, money, distance, quote_summary, option_difference
from coupon_split.service import Service, owner, deadline, Withdrawn
from coupon_split.upstream import Upstream
from coupon_split.local import load_token, save_token, state_dir, LocalState, CartStore, order_context
import json
import threading
import socket

ROOT = Path(__file__).resolve().parent.parent
if (Path(__file__).resolve().parent/"assets/web").exists():
    ROOT=Path(__file__).resolve().parent/"assets"
load_dotenv(ROOT.parent / ".secrets" / "mcd-mcp.env")
load_dotenv(ROOT / ".env")
runtime = state_dir() if os.environ.get("COUPON_SPLIT_STDIO") or os.environ.get('COUPON_SPLIT_HTTP') else ROOT / ".runtime"
runtime.mkdir(exist_ok=True)
access_file = runtime / "access.key"
if not access_file.exists():
    access_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
LOCAL_KEY = access_file.read_text(encoding="utf-8").strip()
UPSTREAM_TOKEN = load_token()
request_base = contextvars.ContextVar("request_base",default="")
credential = contextvars.ContextVar("credential", default="")
SIMULATION_GATE = False  # tests only: also demand the human confirmation in simulation
upstream = Upstream(allow_real=os.environ.get("ALLOW_REAL_ORDERS", "true").lower() == "true" and os.environ.get("COUPON_SPLIT_SIMULATE", "false").lower() != "true")
service = Service(upstream, str(runtime / "plans.sqlite3"))
from coupon_split.humanconfirm import ConfirmationStore
from coupon_split.confirmation import confirm_rule_text

# How long a withdrawal waits for an in-flight create-order to come back before it reports the pending withdrawal.
WITHDRAW_WAIT = 90
confirmations = ConfirmationStore(runtime / "confirmations.sqlite3")
active_credentials = {}  # memory only; local default is recovered on restart
conversation_id = secrets.token_urlsafe(24)
store_context = {}
menu_context = {}
from coupon_split.protocol import INSTRUCTIONS, FLOW, RISK_NOTICE
mcp = FastMCP("最优下单", instructions=INSTRUCTIONS, stateless_http=True, json_response=True)


@mcp.prompt(description='麦当劳点餐：按用户原话起步，报价后等待确认，再展示付款卡。无需 Skill。')
def order_mcdonalds(utterance: str = '我要吃麦当劳') -> str:
    return INSTRUCTIONS+'\n用户点餐原话：'+utterance+'\n现在调用 mcd_start，后续按返回的 next_tool 继续。'


def token():
    value = credential.get() or (UPSTREAM_TOKEN if os.environ.get("COUPON_SPLIT_STDIO") else "")
    if not value:
        raise ValueError("缺少鉴权")
    active_credentials[owner(value)] = value
    return value


AGENT_SKIP = {'markdown','display_markdown','say_hint','before_next_tool','ordering_protocol','comparison_summary','orders','evidence',
              'plans','elements','coverage','extras','headline','workflow','risk_notice','inferred_sources','no_card','stores','last_search',
              'links','confirmation_instruction','next_action','pending_cart_notice','owner'}


def complete_next_arguments(data):
    """Fill the parameters the next tool needs, so a host that reads either side can continue."""
    if data.get('next_arguments') or not data.get('next_tool'):
        return data
    tool=data['next_tool']
    store=data.get('store_code') or data.get('storeCode') or (data.get('default_store') or {}).get('storeCode')
    if store and not data.get('store_code'):data['store_code']=store
    if tool=='quote_best' and store:
        items=data.get('items') or data.get('selected_items')
        if items:data['next_arguments']={'store_code':store,'items':items}
    elif tool=='get_cart' and data.get('session_id'):
        data['next_arguments']={'session_id':data['session_id'],'link_displayed':True,'wait':data.get('wait',25)}
    elif tool=='wait_payment' and data.get('plan_id'):
        data['next_arguments']={'plan_id':data['plan_id'],'timeout':60}
    return data


def agent_block(data):
    """Machine-readable next step for hosts that show the model only content[].text (DeepSeek Harness)."""
    view={}
    for key,value in data.items():
        if key in AGENT_SKIP or value in (None,'',[],{}) and key!='next_tool':continue
        if len(json.dumps(value,ensure_ascii=False,separators=(',',':')))>3500 and key not in {'items','selected_items'}:continue
        view[key]=value
    if any(a['action']=='withdraw' for a in view.get('next_actions',[])):
        view['withdraw_rule']=WITHDRAW_RULE
    return ('---\n【给 Agent 的下一步（用户看不到这一块，不要展示、复述或改写给用户）】\n'
            '宿主若只显示文本，就从下面的代码块取参数；next_tool 为 null 表示本回合停止，等用户说话。\n'
            '下一步调用：\n```json\n'+json.dumps(view,ensure_ascii=False,separators=(',',':'))+'\n```')


WITHDRAW_RULE=('用户在任何时候表达不想要、取消、改天再说、不吃了、等一下、先别、换个别的、我再想想等不想继续的意思，立刻调用 withdraw_order(plan_id)'
               '（订单已建则撤回订单，未建则作废确认页），并把结果如实告诉用户；拿不准就先问用户“要撤回这单吗”。不要等用户说出特定词，也不要先问清楚再动手。'
               '“不要香菜”“换成大杯可乐”这类只是修改餐品，不是撤回。')
TERMINAL_STATES={'cancelled','paid','place_failed','completed','awaiting_coupon_return','partially_completed','expired'}


def next_actions(data):
    """撤回 / 付款 / 等待 hints for the host agent, attached to every result that belongs to a plan."""
    plan_id=data.get('plan_id')
    if not plan_id:return None
    state=data.get('state')
    args={'plan_id':plan_id}
    actions=[]
    if state=='awaiting_payment':
        actions.append({'action':'pay','tool':'pay_card','arguments':args,'say':'把付款卡和付款页链接原样给用户，付款由用户本人完成；需要重新展示时调用 pay_card。'})
        actions.append({'action':'wait','tool':'wait_payment','arguments':{'plan_id':plan_id,'timeout':60},'say':'循环等待付款结果。'} if not data.get('dry_run') else
                       {'action':'wait','tool':None,'arguments':{},'say':'模拟订单不会真实付款，不用等待；结束回合，等用户说话。'})
    elif state in {'verifying','placing'}:
        actions.append({'action':'wait','tool':'wait_payment','arguments':{'plan_id':plan_id,'timeout':60},'say':'等待麦当劳核对下单结果，期间不要重新下单。'})
    elif state in {'quoted','confirm_required'}:
        actions.append({'action':'wait','tool':None,'arguments':{},'say':'等用户确认（说“确认下单”或在确认页点击），不要替用户确认。'})
    if state not in TERMINAL_STATES:
        actions.append({'action':'withdraw','tool':'withdraw_order','arguments':args,'say':WITHDRAW_RULE})
    return actions


def result(data, text):
    data=dict(data)
    data.setdefault('next_tool',None)
    actions=next_actions(data)
    if actions is not None:data['next_actions']=actions
    complete_next_arguments(data)
    # Some MCP clients (including DSH) project only content[].text to the model, others only
    # structuredContent. Every routing parameter is written to both (see agent_block).
    if data.get('picker_url'):
        data=dict(data,next_action='先发送可见 assistant 正文，再调用 get_cart(wait=25)',
                  before_next_tool='display_markdown 必须已经在 assistant 正文输出；工具结果不算输出。')
    stage=data.get('stage',{'quoted':'quote','awaiting_payment':'placed'}.get(data.get('state'),'pick'))
    transport=agent_block(dict(data,stage=stage))
    data = dict(data, markdown=text, display_markdown=text,
                stage=data.get('stage',{'quoted':'quote','awaiting_payment':'placed'}.get(data.get('state'),'pick')),
                say_hint=('先把 display_markdown 中含 [打开点餐页] 的整段话原样输出给用户，再调用 get_cart(wait=25)。链接只发一次；超时继续等待，不催开页。' if data.get('picker_url') else '原样输出 display_markdown，不得改写或删行；包括账户、餐具、时间、用券、规格和覆盖行。'),
                reply_options=data.get('reply_options', []), links=data.get('links', {}))
    return CallToolResult(content=[TextContent(type="text", text=text),TextContent(type='text',text=transport)], structuredContent=data)


def start_guidance(fn):
    from functools import wraps
    @wraps(fn)
    async def wrapped(*args,**kwargs):
        response=await fn(*args,**kwargs)
        data=response.structuredContent
        data['workflow']=FLOW
        notice_key='risk-notice-'+owner(credential.get() or UPSTREAM_TOKEN or 'unauthorized')
        if not memory.read(notice_key).get('shown'):
            old_text=data['display_markdown']
            text=RISK_NOTICE+'\n\n'+old_text
            data.update(markdown=text,display_markdown=text,risk_notice=RISK_NOTICE)
            for n,block in enumerate(response.content):
                if isinstance(block,TextContent) and block.text==old_text:
                    response.content[n]=TextContent(type='text',text=text)
                    break
            memory.write(notice_key,{'shown':True})
        response.content.append(TextContent(type='text',text='点餐流程（宿主执行，不要展示）：'+json.dumps(FLOW,ensure_ascii=False,separators=(',',':'))))
        return response
    return wrapped


def start_form_schema(schema):
    titles={'plan':'这次怎么点','way':'怎么拿','where':'在哪儿附近','people':'几个人（选填）','when':'什么时候'}
    for name,field in schema.get('properties',{}).items():
        field['title']=titles.get(name,name)
        if name=='people':
            field.pop('anyOf',None);field.pop('default',None)
            field.update(type='integer',minimum=1,maximum=12)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def find_store(city: str, keyword: str = "", fulfillment: Literal['pickup','drive','delivery'] = "pickup", address_id: str = "", session_id: str = "", plan: Literal['帮我配','我自己点','照上次那份'] | None = None, utterance: str = "", location: str = "") -> CallToolResult:
    """mcd_start 后，按用户给出的城市、地标和方式查询门店；不能从地址簿擅自推断用户想外送。"""
    keyword = keyword or location
    if fulfillment!='delivery' and not city.strip():
        return result({'stage':'ask','missing':['city'],'next_tool':'find_store','location':keyword},'请从用户给出的地点中提取城市后继续查门店；只有无法确定城市时才询问用户。')
    if fulfillment!='delivery' and not keyword:
        return result({'stage':'ask','missing':['where'],'next_tool':'mcd_start'},'你在哪个城市、哪个地标附近？')
    if not session_id:
        candidate=memory.read(owner(token())).get('active_session','')
        if candidate and carts.get(candidate):session_id=candidate
    if fulfillment == "delivery":
        r = await upstream.call(token(), "delivery-query-stores", {"addressId":address_id,"beType":2})
    else:
        r = await upstream.call(token(), "query-nearby-stores", {"city":city,"keyword":keyword,"beType":5 if fulfillment=="drive" else 1,"searchType":2})
    stores = r["data"] if isinstance(r["data"],list) else r["data"].get("stores",[])
    stores = sorted(stores,key=lambda s:float(s.get("distance") or 1e12))[:5]
    stores = [{"storeCode":str(s["storeCode"]),"storeName":s.get("storeName",s.get("name","门店")),"distance":distance(s.get("distance")),"beCode":s.get("beCode"),"reservationTimeOptions":s.get("reservationTimeOptions",[]),"business_hours":s.get("businessHours") or s.get("businessTime") or s.get("openTime") or ""} for s in stores]
    for s in stores:
        s['fulfillment']=fulfillment
        store_context[(owner(token()),s["storeCode"])]=s
    previous=memory.read(owner(token()))
    previous["last_search"]={"city":city,"keyword":keyword,"fulfillment":fulfillment,"store_code":stores[0]["storeCode"] if stores else None}
    previous.setdefault("stores",{}).update({s["storeCode"]:s for s in stores})
    memory.write(owner(token()),previous)
    text = ('最近的是 **'+stores[0]['storeName']+'**'+(' · '+stores[0]['distance'] if stores[0]['distance'] else '')+'，可换一家。') if stores else '这个地点暂时没有可用门店，请换个附近地标。'
    data = {'stores':[{k:v for k,v in s.items() if k!='reservationTimeOptions'} for s in stores],
            'default_store':{k:v for k,v in stores[0].items() if k!='reservationTimeOptions'} if stores else None,
            'stage':'ask'}
    if session_id:
        cart = owned_cart(session_id)
        # Only mcd_start's current original message owns provenance. A search
        # argument is often a model-composed history summary, never evidence.
        cart.update(store_code=stores[0]['storeCode'] if stores else None, location=keyword,city=city)
        if cart['elements'].get('fulfillment')!=fulfillment:
            cart['elements'].setdefault('sources',{})['fulfillment']='default'
        cart['elements'].update(fulfillment=fulfillment)
        cart['elements'].setdefault('sources',{})['store_code']='nearest'
        if address_id: cart['elements']['addressId']=address_id
        if plan: cart['plan']=plan
        if stores:
            await resolve_cart_intent(cart, token())
            data.update(session_id=session_id, selected_items=cart['items'], elements=cart['elements'])
            if cart.get('intent_question'):
                data.update(next_tool=None,stage='ask',reply_options=cart['intent_options'])
                text+='\n'+cart['intent_question']
            elif cart['items']:
                data.update(next_tool='quote_best', stage='pick')
                text += '\n已放入你说的餐品，接着核价。'
            elif cart.get('plan')=='我自己点':
                page=(await local_url())+'/order/'+session_id
                data.update(picker_url=page, order_url=page, links={'picker_url':page}, next_tool='get_cart', wait=25, stage='pick')
                text += f'\n[打开点餐页]({page}) — 选好点底部“选好了，去算最省”，选好会自动接着算；如果没接上，回来说一句“好了”。'
            elif cart.get('plan')=='帮我配':
                data.update(next_tool='get_menu',recommend=True,people=cart.get('people') or 1,next_arguments={'store_code':cart['store_code'],'session_id':session_id,'recommend':True})
        carts[session_id]=cart
    return result(data,text)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def get_menu(store_code: str, search: str = "", offset: int = 0, elements: OrderElements | None = None, people: int | None = None, recommend: bool = False, session_id: str = "") -> CallToolResult:
    """浏览菜单。recommend=true 返回≤8KB宿主配餐上下文，不代配、不写购物车。
    由你阅读分页和条件、配主推+备选，再调用 propose_cart，展示后等用户认可再核价。
    """
    if recommend:
        from coupon_split.meal_planning import context
        import sys
        return await context(sys.modules[__name__],store_code,session_id,people,offset)
    t = token()
    cart = owned_cart(session_id) if session_id else None
    elements = normalize_elements(elements or (cart['elements'] if cart else {}))
    if elements and (not elements.get('account_name') or not elements.get('account_last4')):
        a=(await account_info()).structuredContent
        elements.update(account_name=a['account_name'],account_last4=a['account_last4'])
    context = order_context(get_store_context(t,store_code),elements,require_take=False) if elements else {}
    base = {"storeCode":store_code,"orderType":1,"beType":1,**context}
    menu = (await upstream.call(t,"query-meals",base))["data"]
    rows = menu_rows(menu)
    if search:
        from coupon_split.slang import resolve
        slang=resolve(search,rows)
        if slang and not slang['selected']:
            return result({'items':slang['rows'],'selected_items':[],'slang_kind':slang['entry']['kind'],'stage':'ask','next_tool':'mcd_start'},slang['message'])
        rows=slang['rows'] if slang else matches(search,rows)
    page=rows[max(0,offset):max(0,offset)+8]
    selected=[]
    if cart:
        cart['store_code']=store_code
        await resolve_cart_intent(cart,t,menu)
        if cart.get('intent_question'):
            return result({'stage':'ask','next_tool':None,'session_id':session_id,'selected_items':[],'reply_options':cart['intent_options']},cart['intent_question'])
        selected=cart['items']
    elif search and offset==0:
        try:resolved=resolve_intent(search,rows)
        except AmbiguousIntent as exc:
            return result({'items':exc.options,'reply_options':[{'n':n,'does':r['name'],'productCode':r['productCode']} for n,r in enumerate(exc.options,1)],'stage':'pick','next_tool':'get_item_detail'},str(exc))
        for item in resolved:
            detail=(await upstream.call(t,'query-meal-detail',dict(base,code=item['productCode'])))['data']
            selected.append(default_item(detail,item['quantity']))
    compact=[{k:r[k] for k in ('productCode','name','price_yuan','was_yuan','requires_card','category')} for r in page]
    def menu_line(n,r):
        price=f"¥{float(r['price_yuan']):.2f}"
        if r.get('requires_card'):price='含卡价 '+price+' / 不带卡价 '+(f"¥{float(r['was_yuan']):.2f}" if r.get('was_yuan') else '待核')+'（卡费另计，报价时比较）'
        return f"{n} {r['name']}　{price}"
    text='这家店现在的菜单（也可以直接说餐品名称和数量）：\n'+'\n'.join(menu_line(n,r) for n,r in enumerate(page,1))
    data={'items':compact,'total':len(rows),'next_offset':offset+8 if offset+8<len(rows) else None,'selected_items':selected,'stage':'pick','next_tool':'quote_best' if selected else 'get_item_detail'}
    # Variable-length menus/default rounds must never overflow the model's budget.
    while len(json.dumps(data,ensure_ascii=False).encode())+len(text.encode())*2>3700 and len(compact)>1:
        compact.pop();data['next_offset']=offset+len(compact)
        text='这家店现在的菜单（也可以直接说名称和数量）：\n'+'\n'.join(menu_line(n,r) for n,r in enumerate(compact,1))
    return result(data,text)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def propose_cart(session_id: str, options: list[dict]) -> CallToolResult:
    """提交一个主推+一个备选：[{title,reason,items:[{productCode,quantity,roundList?,modification?}]}]。
    校验菜单/时段/忌口/官方选配和照原样价；错误按原因重配。成功只更新本地购物车。
    原样展示后停止等用户“就这样”“换备选”或修改；认可后 mcd_start → quote_best，禁止自主下单。
    """
    import sys
    from coupon_split.meal_planning import propose
    return await propose(sys.modules[__name__],session_id,options)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def simple_recommend(store_code: str, people: int | None = None, elements: OrderElements | None = None) -> CallToolResult:
    """仅网页无宿主模型时的简易规则推荐；对话请 get_menu(recommend=true) 后由宿主 propose_cart。"""
    import sys
    from coupon_split.meal_planning import simple
    return await simple(sys.modules[__name__],store_code,people,elements)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def get_item_detail(store_code: str, product_code: str, elements: OrderElements | None = None) -> CallToolResult:
    """按需读取一个商品的官方规格/特制；未指定规格只用官方默认项。"""
    e=normalize_elements(elements)
    if e and (not e.get('account_name') or not e.get('account_last4')):
        account=(await account_info()).structuredContent
        e.update(account_name=account['account_name'],account_last4=account['account_last4'])
    context=order_context(get_store_context(token(),store_code),e,require_take=False) if e else {}
    detail=(await upstream.call(token(),'query-meal-detail',{'storeCode':store_code,'orderType':1,'beType':1,**context,'code':product_code}))['data']
    from coupon_split.presentation import food_name
    catalog=(await upstream.call(token(),'query-meals',{'storeCode':store_code,'orderType':1,'beType':1,**context}))['data']['meals']
    names={str(c):v.get('name',v.get('productName','所选餐品')) for c,v in catalog.items()}
    def name_fields(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {'name','description'} and isinstance(item,str):value[key]=food_name(item,names)
                else:name_fields(item)
        elif isinstance(value,list):
            for item in value:name_fields(item)
    name_fields(detail)
    return result({'detail':detail,'default_item':default_item(detail)},detail.get('name',product_code)+'：可以按官方默认规格，也可以更换下面的选项。')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
async def quote_best(items: list[str] | list[dict], store_code: str, allow_combos: bool = True, elements: OrderElements | None = None) -> CallToolResult:
    """输入 sku:数量，核价并在本地保存方案和确认凭证，不创建上游订单。同购物车并发复用，其余同账户排队。先展示金额，等下一回合确认。"""
    t=token()
    incoming=normalize_elements(elements)
    explicit_mode=incoming.get('fulfillment')
    state=memory.read(owner(t))
    active=carts.get(state.get('active_session',''))
    if active and active.get('recommendation_pending'):
        raise ValueError('配餐方案尚未获用户认可；先展示主推和备选，等用户说“就这样”或“换备选”后调用 mcd_start，再核价')
    previous=state.get('elements',{})
    if active and active['owner']==owner(t):
        previous=active['elements']
        for key,value in previous.items():
            if key!='sources' and key not in incoming:
                incoming[key]=value
                incoming.setdefault('sources',{})[key]=previous.get('sources',{}).get(key,'default')
    elements=merge_elements(previous,incoming)
    selected_store=get_store_context(t,store_code)
    if selected_store.get('fulfillment') and selected_store['fulfillment']!=elements.get('fulfillment'):
        labels={'pickup':'到店自取','drive':'得来速','delivery':'麦乐送'}
        raise ValueError(('本次传入的方式' if explicit_mode else '沿用的方式')+'“'+labels.get(elements.get('fulfillment'),'所选方式')+'”与这家店的查询方式“'+labels.get(selected_store['fulfillment'],'门店方式')+'”不符，请按本次方式重新查询门店。')
    # Model-provided sources are never evidence of what the user said.
    trusted=previous.get('sources',{})
    for key,value in elements.items():
        if key=='sources':continue
        elements['sources'][key]=(trusted.get(key,'default') if active and previous.get(key)==value else
            (trusted.get(key) if trusted.get(key) in {'default','nearest','account'} else 'last') if key in previous and previous[key]==value else 'default')
    official=(await account_info()).structuredContent
    if incoming.get("account_last4") and incoming["account_last4"]!=official["account_last4"]:
        raise ValueError("账户后四位与本机授权账户不一致，请刷新账户信息")
    elements['account_last4']=official['account_last4']
    elements['account_name']=official['account_name']
    elements['sources']['account_name']='account'
    elements['sources']['account_last4']='account'
    if not elements['sources'].get('store_code'):
        elements['sources']['store_code']='nearest' if (owner(t),store_code) in store_context else 'last' if memory.read(owner(t)).get('store_code')==store_code else 'default'
    if elements.get('fulfillment')=='delivery':
        addresses=(await upstream.call(t,'delivery-query-addresses',{}))['data']
        addresses=addresses if isinstance(addresses,list) else addresses.get('addresses',[])
        address=next((a for a in addresses if str(a.get('addressId'))==str(elements.get('addressId'))),None)
        if not address:raise ValueError('配送地址不属于本账户，请重新选择')
        elements=dict(elements,delivery_address=address.get('fullAddress',address.get('address','')))
    context=order_context(get_store_context(t,store_code),elements,require_take=False)
    if items and isinstance(items[0],dict) and 'item' in items[0]:
        items=[copy.deepcopy(item['item']) for item in items]
    if items and isinstance(items[0],str):
        from coupon_split.optimizer import parse_cart, atom
        items=[atom(c,n) for c,n in parse_cart(items).items()]
    selected_specs=await validate_selected(t, store_code, items, context,fill_defaults=True)
    plan = await service.quote(t,store_code,items,allow_combos,context)
    take=elements.get("takeWayCode")
    take_options=plan['plans'][0]['orders'][0]['takeWayList']
    if take:
        selected=next((r for r in take_options if take==r.get('code',r.get('takeWayCode'))),{})
        elements['takeWayName']=selected.get('name') or selected.get('takeWayName') or selected.get('title') or {'eat-in':'堂食','take-in-store':'外带'}.get(take,'所选取餐方式')
    if take and context["beType"] != 2 and any(take not in {r.get("code",r.get("takeWayCode")) for r in o["takeWayList"]} for p in plan["plans"] for o in p["orders"]):
        raise ValueError("请选择本次计价返回的堂食/外带方式")
    pending=['takeWayCode'] if context['beType']!=2 and not take else []
    plan.update(order_elements=elements,elements_pending=pending,elements_validated=not pending,selected_specifications=selected_specs)
    previous=memory.read(owner(t))
    previous.update(elements=elements,store_code=store_code,items=items,
        item_summary='、'.join(v['name']+' ×'+str(v['quantity'])+('（'+'、'.join(v['choices']+v['modifications'])+'）' if v['choices'] or v['modifications'] else '') for v in selected_specs))
    memory.write(owner(t),previous)
    plan.update(get_store_context(t,store_code))
    plan.update(menu_context.get((owner(token()),store_code),{}))
    for spec in plan.get('selected_specifications',[]):
        if plan.get('names',{}).get(spec['productCode']):
            spec['name']=plan['names'][spec['productCode']]
    from coupon_split.presentation import resolve_display_names
    resolve_display_names(plan)
    service.save(plan)
    if pending and elements.get('fulfillment')=='drive':plan['take_options']=take_options
    # Direct quotation and the browser path must leave the same resumable cart.
    sid=state.get('active_session','')
    if not active or active.get('owner')!=owner(t):
        sid=secrets.token_urlsafe(24)
        active={'owner':owner(t),'conversation_id':conversation_id,'plan':'我自己点'}
    active.update(items=copy.deepcopy(items),elements=elements,store_code=store_code,
        submitted=True,intent_resolved=True,item_summary=previous['item_summary'],pending_plan_id=plan['plan_id'])
    carts[sid]=active
    memory.write(owner(t),dict(previous,active_session=sid))
    data=quote_summary(plan)
    data.update(session_id=sid,confirmation_instruction='报价后结束本回合。用户下一条说“确认下单”（超过 ¥200 说“确认下单 ¥金额”）时，直接调用 place_orders；用户表达不想要、取消、改天、不吃了、等一下等不想继续的意思时，立刻调用 withdraw_order(plan_id) 并如实告知结果，拿不准先问“要撤回这单吗”；不要调用 mcd_start。',
        next_arguments={'plan_id':plan['plan_id'],'confirm_token':plan['confirm_token']})
    return result(data,markdown(plan))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
async def get_options(plan_id: str, offset: int = 0, current_option_id: str = "0") -> CallToolResult:
    """分页读取去重后的候选方案摘要，每页五条。"""
    plan=service.load(plan_id,token())
    current=next(p for p in plan['plans'] if p['option_id'] == current_option_id)
    rows=[{"option_id":p["option_id"],"label":p["label"],"price":p["price"],"order_count":len(p["orders"]),"difference":option_difference(p,current)} for p in plan["plans"][max(0,offset):max(0,offset)+5]]
    return result({"items":rows,"total":len(plan["plans"]),"next_offset":offset+5 if offset+5<len(plan["plans"]) else None,"price_calls":plan["price_calls"]},"候选均经官方核价；有限搜索，不保证全局最优。")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
async def get_evidence(plan_id: str, evidence_id: str) -> CallToolResult:
    """按编号读取当前账户的脱敏逐笔计价证据；券标识重放须用本机私有证据库。"""
    plan=service.load(plan_id,token())
    evidence=next((e for e in plan["evidence"] if e["evidence_id"]==evidence_id),None)
    if not evidence:
        raise ValueError("计价依据不存在")
    # Deliberately returned only on explicit evidence retrieval, never in quote_best.
    import copy
    value=copy.deepcopy(evidence)
    def scrub(v):
        if isinstance(v,dict):return {k:scrub(x) for k,x in v.items() if k not in {"couponId","couponCode","payId"}}
        if isinstance(v,list):return [scrub(x) for x in v]
        return v
    return result({"evidence":scrub(value),"notice":"券标识已脱敏，重放请用本机证据库"},"券标识已脱敏，重放请用本机证据库。")


async def prepare_place(t, plan, option_id, confirm_token, take_way=''):
    """Every check that must pass before an order may be created; raises ValueError. Returns the chosen option."""
    selected=next(p for p in plan['plans'] if p['option_id']==option_id)
    if take_way:
        import hmac
        if not hmac.compare_digest(confirm_token,plan['confirm_token']):raise ValueError('确认凭证不匹配')
        if any(take_way not in {v.get('code',v.get('takeWayCode')) for v in o['takeWayList']} for o in selected['orders']):raise ValueError('请选择本次官方堂食/外带选项')
        plan['order_elements']['takeWayCode']=take_way
        plan['order_elements']['sources']['takeWayCode']='utterance'
        plan.update(elements_pending=[],elements_validated=True);service.save(plan)
    service.validate_before_place(plan,selected)
    for order in selected['orders']:
        await validate_selected(t,plan['storeCode'],order['request']['items'],order['request'],fill_defaults=False)
    return selected


async def withdraw_created(t, plan):
    """Withdraw what exists (or is still being verified) for this plan; a no-op when nothing was created."""
    if plan['state'] in {'awaiting_payment','verifying','rollback_pending'} and (plan.get('orders') or plan.get('unknown_orders')):
        return await service.cancel(t,plan['plan_id'])
    return plan


async def finish_place(t, plan_id, confirm_token, option_id, key=None):
    """Create the order(s) and keep the local memory in step. Runs on the person's click, or right after an approved dialog.

    key is the confirmation record of this attempt (state placing). Its last gate sits right before
    create-order; a withdrawal after that gate cannot stop the request, so once the order exists
    it is withdrawn here, before the attempt is marked used.
    """
    try:
        plan = await service.place(t,plan_id,confirm_token,option_id,guard=(lambda:confirmations.commit(key)) if key else None)
    except BaseException:
        if key:confirmations.finish(key,'failed')
        raise
    if key and not confirmations.finish(key,'used'):
        # "别下单" arrived while create-order was in flight: the order now exists, so take it back at once.
        plan=await withdraw_created(t,plan)
        plan['withdrawn_after_create']=True
        service.save(plan)
        confirmations.finish(key,'used',force=True)
    await attach_payment_url(plan)
    if plan.get('orders'):
        state=memory.read(owner(t))
        state['last_order']={'elements':copy.deepcopy(plan.get('order_elements',{})),
            'store_code':plan['storeCode'],'items':copy.deepcopy(plan.get('requested_items',[])),
            'item_summary':state.get('item_summary','')}
        memory.write(owner(t),state)
    return plan


def withdrawal_text(plan):
    state=plan['state']
    if state in {'cancelled','awaiting_coupon_return'}:
        return '好的，已经按你的意思撤回这笔订单，没扣钱。' if not plan.get('dry_run') else '好的，已经撤回（模拟订单，本来就没有真实扣款）。'
    if state=='verifying':
        return '好的，不下单。刚才的下单结果还在向麦当劳核对中，已记下撤回要求：订单一出现就会自动撤回，请不要重新下单。'
    if state=='rollback_pending':
        return '你说不下单，但撤回还没有成功，系统会继续重试。保险起见请打开微信麦当劳小程序 → 我的订单，看到待付款的单就点取消，不要付款。'
    if state in {'paid','partially_completed'}:
        return '这笔订单已经付款，没法在这里撤回。需要退款请在微信麦当劳小程序 → 我的订单里申请。'
    if state=='place_failed':
        return '好的，不下单。这单本来就没有下成，没扣钱。'
    if state=='placing':
        return '好的，不下单。下单请求已经发出、结果还没回来，已记下撤回要求：订单一建成就会自动撤回，请不要重新下单。'
    return '好的，不下单。'


async def withdraw_now(t, plan, account=None):
    """One withdrawal for every caller (agent tool, page button, fallback words): returns (plan, revoked_or_flagged).

    Before the click or while the goods are re-checked the confirmation is revoked and nothing is created; once
    create-order may be in flight the order is withdrawn as soon as it exists; an existing order is cancelled.
    t may be None when this process holds no credential: then only the confirmation side can be handled.
    """
    from coupon_split.service import owner as owner_of
    account=account or owner_of(t)
    revoked,flagged=confirmations.withdraw(account,plan['plan_id'])
    plan=service.load(plan['plan_id'],t) if t else plan
    if flagged:
        # The creating side sees the flag and withdraws the order itself; wait for it (bounded).
        until=time.monotonic()+WITHDRAW_WAIT
        while time.monotonic()<until and confirmations.creating(account,plan['plan_id']):
            await asyncio.sleep(.1)
        plan=service.load(plan['plan_id'],t) if t else plan
    elif t and plan['state']=='placing':
        # Placed from another path (no confirmation record): let it finish, then withdraw what it created.
        for _ in range(100):
            await asyncio.sleep(.1)
            plan=service.load(plan['plan_id'],t)
            if plan['state']!='placing':break
    if t and plan['state']!='quoted':plan=await withdraw_created(t,plan)
    return plan,revoked+flagged


async def withdraw_by_words(t, plan, option_id):
    """Fallback: a strong withdraw word arrived in place_orders.user_confirmation (the agent should have called withdraw_order first).

    Works in every phase (see ConfirmationStore.withdraw): before the click or while the goods are
    being re-checked the confirmation is revoked and nothing is created; once create-order may be
    in flight the order is withdrawn as soon as it exists.
    """
    plan,count=await withdraw_now(t,plan)
    if plan['state']=='quoted':
        # Nothing exists yet: refuse the call. The plan stays quoted; a later explicit confirmation starts afresh.
        raise ValueError('用户表示不下单或要先等一等，订单没有创建，之前的确认页已作废（作废 %d 个）。方案还保留着。请先在对话中明确确认下单：用户重新说“确认”后才会重新开始，在那之前不要再调用 place_orders。'%count)
    note=withdrawal_text(plan)
    data=dict(public(plan),withdrawn=True,next_tool=None)
    if plan['state'] in {'verifying'}:
        data.update(next_tool='wait_payment',next_arguments={'plan_id':plan['plan_id'],'timeout':60})
    return result(data,note+'\n\n'+markdown(plan))


@mcp.tool(meta={"anthropic/requiresUserInteraction":True}, annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True))
async def place_orders(plan_id: str, confirm_token: str, user_confirmation: Annotated[str, Field(description='逐字复制本轮最后一条用户消息；严禁 Agent 代造、拼接或补写金额。有金额须匹配报价；超过¥200须完整原文“确认下单 ¥金额”（两位小数）。'+confirm_rule_text()+'用户想撤回时请直接调用 withdraw_order，不要靠这个参数；若这里收到取消、撤回、不要了、别下单等明确的词，服务端也会作废确认或撤回，仅作保险。')], option_id: str = "0", take_way: str = "", high_amount_confirmation: str = "", ctx: Context | None = None) -> CallToolResult:
    """仅逐字收到用户明确确认后调用（宿主有审批时须通过）。默认真实下单；真实下单还需用户本人在确认弹窗（填入实付金额后接受）或本机确认页（点“确认下单”）再确认一次，任何宿主都一样、没有豁免，白名单确认句本身不足以下单；模拟模式只需白名单确认句。超过200元须额外确认具体金额。确认凭证10分钟有效。"""
    t=token(); plan=service.load(plan_id,t); claim={}
    from coupon_split import confirmation as cf
    selected=next(p for p in plan['plans'] if p['option_id']==option_id)
    # Decision order, see coupon_split/confirmation.py. 1: withdrawal beats everything else.
    if cf.is_negative(user_confirmation):
        return await withdraw_by_words(t,plan,option_id)
    if plan['state']=='quoted':
        # M03: a wrong credential or an expired quote is refused on the very first call, before any confirmation page exists.
        import hmac
        if not hmac.compare_digest(str(confirm_token),plan['confirm_token']):
            raise ValueError('确认凭证不匹配：这个 confirm_token 不是当前报价给的。请重新调用 quote_best 获取新报价，再让用户确认。')
        if time.time()-plan['created']>600:
            raise ValueError('报价已过期（超过 10 分钟），价格和优惠可能已变。请重新调用 quote_best 获取新报价，再让用户确认。')
        failure=confirmations.take_failure(owner(t),plan_id)
        if failure:raise ValueError(failure)
        # 3: a live confirmation page exists, the click authorises (words must not contradict it);
        # 4: otherwise the words must be a standard confirmation (whitelist), anything else is only a hint.
        if confirmations.find_any(owner(t),plan_id,option_id,selected['price']):
            cf.validate_follow_up(user_confirmation,selected['price'])
        else:
            cf.validate_confirmation(user_confirmation,selected['price'],high_amount_confirmation)
        try:
            selected=await prepare_place(t,plan,option_id,confirm_token,take_way)
        except ValueError as exc:
            if '授权失效' not in str(exc):raise
            # Dead account authorisation is reported now, with the page that fixes it, not at some later step.
            url=await local_url()
            return result({'stage':'setup','setup_url':url+'/setup','needs_authorization':True,'links':{'setup_url':url+'/setup'},'next_tool':None},f'麦当劳账户授权已失效，订单没有创建。请重新连接：[打开授权页]({url}/setup)。保存后回来说“好了”，再重新报价确认。')
        pending=await human_confirmation(ctx,t,plan,selected,option_id,dict(plan_id=plan_id,confirm_token=confirm_token,user_confirmation=user_confirmation,option_id=option_id,take_way=take_way,high_amount_confirmation=high_amount_confirmation),claim)
        if pending is not None:return pending
    # 2: not quoted any more means the click already placed it; this only reads the result back.
    key = claim.get('key') if plan['state']=='quoted' else None
    if plan['state']=='quoted' and not key:
        # Last line of defence: a real order is never created without a verified human step (accepted elicitation or page click).
        from coupon_split import humanconfirm as hc0
        if hc0.requires_human(upstream.allow_real or plan.get('real_orders_enabled'),SIMULATION_GATE) and claim.get('proof')!='elicitation':
            raise ValueError('真实下单需要你本人在确认页或确认弹窗中确认一次，订单没有创建。')
        # Accepted dialog / simulation: still register the attempt, so a "别下单" that arrives now can find it.
        take=(plan.get('order_elements') or {}).get('takeWayCode','')
        from coupon_split import humanconfirm as hc
        record=confirmations.create(owner(t),plan_id,option_id,take,selected['price'],hc.order_summary(plan,selected,take))
        confirmations.transition(record['key'],'placing',('pending',))
        key=record['key']
    try:
        plan = await finish_place(t,plan_id,confirm_token,option_id,key)
    except Withdrawn:
        raise ValueError('用户表示不下单，订单没有创建。方案还保留着；用户重新明确确认后才会重新开始。')
    data=public(plan)
    if plan.get('dry_run'):data.update(next_tool=None,payment_wait_skipped=True)
    if plan['state']=='verifying':
        data.update(next_tool='wait_payment',next_arguments={'plan_id':plan_id,'timeout':60},
                    before_next_tool='先在可见正文原样输出 display_markdown，再调用 wait_payment 等核对结论；期间不要重新下单。')
    if plan['state']=='place_failed':data.update(next_tool=None)
    if plan['state']=='awaiting_payment' and not plan.get('dry_run'):
        data.update(next_tool='wait_payment',next_arguments={'plan_id':plan_id,'timeout':60},
                    before_next_tool='先在可见正文原样输出 display_markdown 付款卡，再循环 wait_payment；已付款后告知真实取餐码和门店。')
    return result(data,markdown(plan))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def pay_card(plan_id: str) -> CallToolResult:
    """只读查询已创建订单状态和付款页；临期撤回由后台巡检负责。"""
    plan = await service.inspect_payment(token(),plan_id)
    for o in plan["orders"]:
        o["remaining_seconds"] = max(0,int(deadline(o["expirePayTime"])-time.time()))
    if plan.get('payment_key'):plan['payment_url']=(await local_url())+'/payment/'+plan['payment_key']
    return result(public(plan),markdown(plan))


async def attach_payment_url(plan):
    if not plan.get('payment_key'):
        plan['payment_key']=secrets.token_urlsafe(32)
        service.save(plan)
    plan['payment_url']=(await local_url())+'/payment/'+plan['payment_key']
    return plan


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def wait_payment(plan_id: str, timeout: Annotated[int, Field(ge=0,le=60)] = 60) -> CallToolResult:
    """只读等待付款，每轮最多60秒、每3秒查状态，不写本地或上游；临期撤回由后台巡检负责。模拟模式无需调用。"""
    plan=await service.wait_payment(token(),plan_id,timeout)
    if plan.get('payment_key'):plan['payment_url']=(await local_url())+'/payment/'+plan['payment_key']
    from coupon_split.payment import payment_message
    if plan['state'] in {'verifying','place_failed'}:
        from coupon_split.presentation import markdown as card
        text=card(plan)
    else:text=('还没付款。\n' if plan['state']=='awaiting_payment' else '')+payment_message(plan)
    codes=[str(o[k]) for o in plan.get('orders',[]) for k in ('takeCode','pickupCode','orderCode','mealCode') if o.get(k)]
    return result(dict(public(plan),next_tool='wait_payment' if plan['state'] in {'awaiting_payment','verifying'} and not plan.get('dry_run') else '',pickup_codes=list(dict.fromkeys(codes))),text)


async def withdraw_tool_result(plan_id, abandon):
    t=token()
    plan=service.load(plan_id,t)
    if plan['state'] in {'cancelled','place_failed'} and not plan.get('orders'):
        return result(dict(public(plan),withdrawn=True,next_tool=None),'这单本来就已经取消或没有下成，没有订单，没扣钱。')
    plan,count=await withdraw_now(t,plan)
    if plan['state']=='quoted':
        if abandon:
            plan=await service.cancel(t,plan_id)
            note='好的，方案已放弃，订单没有创建，没扣钱。'
        else:
            note='好的，不下单。订单没有创建，之前的确认页已作废（作废 %d 个）。方案还保留着，想下单请用户重新说“确认下单”。'%count
        return result(dict(public(plan),withdrawn=True,next_tool=None),note)
    note=withdrawal_text(plan)
    data=dict(public(plan),withdrawn=True,next_tool=None)
    if plan['state']=='verifying':
        data.update(next_tool='wait_payment',next_arguments={'plan_id':plan['plan_id'],'timeout':60})
    return result(data,note+'\n\n'+markdown(plan))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True))
async def withdraw_order(plan_id: str) -> CallToolResult:
    """撤回这单。用户在任何时候表达不想要、取消、改天再说、不吃了、等一下、先别、我再想想等不想继续的意思，必须立刻调用，不用等特定词、不用先确认：确认页还没点就作废确认页，订单已建就撤回订单。把返回结果如实告诉用户；拿不准用户是不是想撤，先问“要撤回这单吗”。不要香菜、换大杯这类只是改餐品，不用撤回。"""
    return await withdraw_tool_result(plan_id,False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True))
async def cancel_plan(plan_id: str) -> CallToolResult:
    """放弃方案或撤回订单（与 withdraw_order 相同，另外把还没下单的方案标记为放弃）。用户不想要、取消、改天、不吃了、等一下时立刻调用，并如实告知结果；只取消当前账户自己的方案。"""
    return await withdraw_tool_result(plan_id,True)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def preview_selection(store_code: str, items: list[dict], elements: OrderElements | None = None) -> CallToolResult:
    """只读核价，返回本次官方可选堂食/外带方式；不会创建方案或订单。"""
    from coupon_split.optimizer import parse_cart
    parse_cart(items)
    t=token();elements=normalize_elements(elements)
    if not elements.get('account_name') or not elements.get('account_last4'):
        account=(await account_info()).structuredContent
        elements.update(account_name=account['account_name'],account_last4=account['account_last4'])
    context=order_context(get_store_context(t,store_code),elements,require_take=False)
    await validate_selected(t,store_code,items,context,fill_defaults=True)
    r=await upstream.call(t,"calculate-price",dict(context,storeCode=store_code,items=items))
    return result({"takeWayList":r["data"].get("takeWayList",[]),"price":r["data"]["price"]},"请选择本次官方返回的取餐方式。")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def account_info() -> CallToolResult:
    """读取本人账户，仅返回账户后四位与本机别名，不返回完整账户号。"""
    t=token()
    data=(await upstream.call(t,"query-my-account",{}))["data"]
    last4=str(data.get("accountId",""))[-4:]
    if len(last4)!=4:raise ValueError("无法核实下单账户")
    previous=memory.read(owner(t)).get('elements',{})
    name=previous.get('account_name') or next((str(data[k]) for k in ('nickname','nickName','accountName','userName','name') if data.get(k)),None) or '本人账户'
    name=name.replace('（默认）','')
    return result({"account_last4":last4,"account_name":name},name+' · 尾号 '+last4)


def get_store_context(t,store_code):
    return store_context.get((owner(t),store_code)) or memory.read(owner(t)).get("stores",{}).get(store_code,{})


async def validate_selected(t, store, items, context, fill_defaults=False):
    summaries=[]
    if context.get('beType')==2:
        current=(await upstream.call(t,'query-meals',dict(context,storeCode=store)))['data']['meals']
        unavailable=[(n,i) for n,i in enumerate(items,1) if current.get(i['productCode'],{}).get('available') is False or current.get(i['productCode'],{}).get('isSelling') is False]
        if unavailable:
            pickup=(await upstream.call(t,'query-meals',{'storeCode':store,'beType':1,'orderType':1}))['data']['meals']
            labels=[pickup.get(i['productCode'],{}).get('name') or f'你选的第 {n} 件餐品' for n,i in unavailable]
            raise ValueError('、'.join(dict.fromkeys(labels))+'暂不支持这次外送，请换一道餐品或改为到店自取')
    for item in items:
        try:detail=(await upstream.call(t,"query-meal-detail",dict(context,storeCode=store,code=item["productCode"])))["data"]
        except ValueError:
            if context.get('beType')==2:
                label=current.get(item['productCode'],{}).get('name','这道餐品')
                raise ValueError(label+'暂时无法提供这次外送的规格，请换一道餐品或稍后重试') from None
            raise
        rounds={str(r["id"]):r for r in detail.get("rounds",[])}
        defaulted=False
        if fill_defaults and rounds and not item.get('roundList'):
            from coupon_split.menu import default_choices
            item['roundList']=[{'round':str(r['id']),'comboItemList':default_choices(r)} for r in detail['rounds']]
            defaulted=True
        chosen=item.get("roundList",[])
        if len(chosen)!=len(rounds) or len({r["round"] for r in chosen})!=len(chosen):
            raise ValueError("套餐选配未完整填写")
        for selected in chosen:
            r=rounds.get(selected["round"])
            if not r:raise ValueError("未知套餐选配")
            choices={str(c["code"]):c for c in r["choices"]}
            count=sum(c["quantity"] for c in selected["comboItemList"])
            if not r.get("minQuantity",1)<=count<=r.get("maxQuantity",1):
                raise ValueError("套餐选配数量不符")
            for c in selected["comboItemList"]:
                if str(c["code"]) not in choices or not isinstance(c["quantity"],int) or c["quantity"]<1:
                    raise ValueError("选配规格未获官方核实")
        modifications=[]
        choice_labels=[]
        def check_mod(selection,schema):
            allowed={}
            groups=[]
            def collect(value):
                if isinstance(value,dict):
                    if isinstance(value.get('values'),list):
                        pairs={(str(v['code']),str(v[k])) for v in value['values'] if isinstance(v,dict) and 'code' in v for k in ('selectedKey','unselectedKey') if k in v}
                        if pairs:groups.append((pairs,value.get('maxValues',len(pairs))))
                    if 'code' in value:
                        for field in ('selectedKey','unselectedKey'):
                            if field in value:
                                is_default=(field=='selectedKey' and value.get('selectedQuantity',0)>0) or (field=='unselectedKey' and not value.get('selectedQuantity',0))
                                allowed[(str(value['code']),str(value[field]))]=(('不要' if field=='unselectedKey' else '')+value.get('name','已选特制').replace('切块生菜','生菜'),value.get('maxQuantity',1),is_default)
                    for x in value.values():collect(x)
                elif isinstance(value,list):
                    for x in value:collect(x)
            collect(schema)
            seen=set()
            for mod in selection.get('modification',{}).get('values',[]):
                pair=(str(mod.get('code')),str(mod.get('key')))
                if pair not in allowed or pair in seen or not isinstance(mod.get('quantity'),int) or not 0<=mod['quantity']<=allowed[pair][1]:
                    raise ValueError('特制项未获官方核实')
                seen.add(pair)
                if not allowed[pair][2] and allowed[pair][0]!='标准':modifications.append(allowed[pair][0])
            for pairs,maximum in groups:
                if len(seen & pairs)>maximum:raise ValueError('同组特制选项不能同时选择，请按官方单选/多选限制填写')
        check_mod(item,detail.get('modification',{}))
        choices=[]
        for selected in chosen:
            for child in selected['comboItemList']:
                official=next(c for c in rounds[selected['round']]['choices'] if str(c['code'])==str(child['code']))
                choices.append(official.get('name',official['code'])+(' ×'+str(child['quantity']) if child['quantity']>1 else ''))
                check_mod(child,official.get('modification',{}))
        summaries.append({'productCode':item['productCode'],'name':detail.get('name',item['productCode']),'quantity':item['quantity'],'choices':choices,'modifications':modifications,
            'selected_codes':[str(c['code']) for r in chosen for c in r['comboItemList']],
            'names':{str(c['code']):c.get('name',str(c['code'])) for r in detail.get('rounds',[]) for c in r['choices']}})
    return summaries


memory = LocalState(runtime/"memory")
carts = CartStore(runtime/'carts.sqlite3')
pending_addresses = {}
bridge_port = None
bridge_server = None
bridge_task = None


def owned_cart(session_id):
    cart=carts.get(session_id)
    if not cart or cart['owner']!=owner(token()):raise ValueError('购物车不存在或不属于此账户')
    return cart


def cart_credential(cart):
    return active_credentials.get(cart['owner']) or (UPSTREAM_TOKEN if UPSTREAM_TOKEN and owner(UPSTREAM_TOKEN)==cart['owner'] else '')


async def bridge_health(request):
    from coupon_split.bridge import identity
    return JSONResponse({'identity':identity(runtime)},headers={'Cache-Control':'no-store'})


async def resolve_cart_intent(cart,t,menu=None):
    if not cart.get('store_code') or not cart.get('intent') or cart.get('intent_resolved'):return
    store=cart['store_code'];e=cart['elements']
    # Account information is filled from the authorized account, never requested.
    a=(await account_info()).structuredContent
    e.update(account_name=a['account_name'],account_last4=a['account_last4'])
    context=order_context(get_store_context(t,store),e,require_take=False)
    base=dict(context,storeCode=store)
    menu=menu or (await upstream.call(t,'query-meals',base))['data']
    try:selected=resolve_intent(cart['intent'],menu_rows(menu))
    except AmbiguousIntent as exc:
        cart.update(intent_question=str(exc),intent_options=[{'n':n,'does':r['name'],'productCode':r['productCode']} for n,r in enumerate(exc.options,1)])
        return
    if selected:
        await validate_selected(t,store,selected,context,fill_defaults=True)
        cart.pop('intent_question',None);cart.pop('intent_options',None)
        names={r['productCode']:r['name'] for r in menu_rows(menu)}
        cart.update(items=selected,intent_resolved=True,item_summary='、'.join(names.get(i['productCode'],'所选餐品')+' ×'+str(i['quantity']) for i in selected))


async def local_url():
    if request_base.get():return request_base.get()
    if os.environ.get('COUPON_SPLIT_STDIO'):
        from coupon_split.bridge import ensure_bridge
        return await asyncio.to_thread(ensure_bridge,runtime)
    global bridge_port,bridge_server,bridge_task
    if bridge_port is None:
        import uvicorn
        sock=socket.socket()
        sock.bind(("127.0.0.1",0))
        sock.listen(128)
        bridge_port=sock.getsockname()[1]
        bridge_server=uvicorn.Server(uvicorn.Config(app,log_level="error",access_log=False))
        bridge_task=asyncio.create_task(bridge_server.serve(sockets=[sock]))
        while not bridge_server.started:
            await asyncio.sleep(.01)
    return f"http://127.0.0.1:{bridge_port}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
@start_guidance
async def mcd_start(utterance: Annotated[str, Field(description='逐字复制本轮最后一条用户消息。严禁拼接上一轮原话、历史摘要、已知餐品或要素；历史由服务端自动保留。')], recommend: bool | None = None, plan: Literal['帮我配','我自己点','照上次那份'] | None = None, ctx: Context | None = None, session_id: Annotated[str, Field(description='同一对话后续每轮必填：复制前一轮返回的 session_id，否则会开启新会话并丢失人数/预算/忌口。仅全新对话留空，禁止查询或猜旧 ID。')] = "") -> CallToolResult:
    """用户一提到想吃/点麦当劳（如“我要吃麦当劳”），立刻调用本工具，不要先追问；开始或修改点餐、继续刚才的餐品；报价后的“确认下单”“确认”“同意下单”“就这样下单”（可带金额，如“确认下单 ¥金额”）必须直接调用 place_orders，绝不调用本工具。无需用 shell 读取 SKILL.md。

    每轮逐字传用户原话；同一对话后续轮传前一轮 session_id，新对话留空。
    按 next_tool 和 next_arguments 同回合继续，只追问 missing。
    原样展示 display_markdown（中文，禁止英文元话）。先向用户发出点餐页链接，
    再 get_cart(wait=25,link_displayed=true)，同一回合链接只发一次，超时继续等。
    收到 submitted 自动 quote_best；收到“好了”仍调用本工具恢复。
    报价完整展示后停等明确确认；place_orders 逐字传用户最后一句，宿主有审批时仍须允许，不得自主下单。
    缺授权只给本机 setup_url，绝不索要聊天 Token；禁止创建地址。
    """
    import re
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from coupon_split.slang import classify,resolve
    slang=classify(utterance)
    if slang and slang['kind'] in {'other_brand','unsupported','brand'}:
        clarification=resolve(utterance,[])
        return result({'stage':'ask','next_tool':None,'session_id':session_id,'missing':['food'],'items':[]},clarification['message'])
    url=await local_url()
    global UPSTREAM_TOKEN
    if not credential.get() and memory.read('onboarding').get('utterance'):
        # The authorization page is served by another process. Recover its newly
        # saved credential before checking the old in-memory value.
        UPSTREAM_TOKEN=load_token(prefer_saved=True)
    if not (credential.get() or UPSTREAM_TOKEN):
        memory.write('onboarding',{'utterance':utterance})
        return result({'stage':'setup','setup_url':url+'/setup','needs_authorization':True,'links':{'setup_url':url+'/setup'}},f'先连上你自己的麦当劳账户，大约 2 分钟：[打开授权页]({url}/setup)。弄好后回来说一声就行。')
    try:
        await upstream.call(token(),'query-my-account',{})
    except ValueError as exc:
        if '授权失效' not in str(exc):raise
        memory.write('onboarding',{'utterance':utterance})
        return result({'stage':'setup','setup_url':url+'/setup','needs_authorization':True,'links':{'setup_url':url+'/setup'}},f'麦当劳账户授权已失效，请重新连接：[打开授权页]({url}/setup)。保存后回来说“好了”，继续点餐。')
    onboarding=memory.read('onboarding')
    if onboarding.get('utterance') and utterance.strip().strip('。！!')=='好了':
        memory.write('onboarding',{})
        resumed=await mcd_start(onboarding['utterance'],recommend,plan,ctx)
        data=resumed.structuredContent
        return result(data,'已连接。可以说“帮我配”“我自己点”或“外送到公司”。\n\n'+data['display_markdown'])
    if onboarding.get('utterance'):
        memory.write('onboarding',{})
    t=token();state=memory.read(owner(t))
    existing=carts.get(state.get('active_session',''))
    from coupon_split import confirmation as cf
    if cf.classify(utterance)[0]=='confirm':
        if existing and (existing.get('conversation_id')==conversation_id or session_id==state.get('active_session')) and existing.get('pending_plan_id'):
            pending=service.load(existing['pending_plan_id'],t)
            if pending['state']=='quoted':
                data=quote_summary(pending)
                data.update(session_id=state['active_session'],next_tool='place_orders',
                    next_arguments={'plan_id':pending['plan_id'],'confirm_token':pending['confirm_token'],'user_confirmation':utterance})
                return result(data,'请调用 place_orders，使用原报价凭证和本轮确认原话；不要重新开始点餐。')
    if (existing and existing.get('last_utterance')==utterance
            and (existing.get('conversation_id')==conversation_id or session_id==state.get('active_session'))
            and (plan is None or plan==existing.get('plan'))
            and existing.get('store_code')):
        # Host retries can arrive after the page submitted. Never turn that
        # accepted cart back into an unsubmitted one or discard its page state.
        if existing.get('intent_question'):
            return result({'stage':'ask','next_tool':None,'session_id':state['active_session'],'reply_options':existing['intent_options']},existing['intent_question'])
        return await get_cart(state['active_session'],wait=0)
    if utterance.strip().strip('。！!') in {'好了','选好了'}:
        sid=state.get('active_session','')
        if sid and carts.get(sid):return await get_cart(sid,wait=0)
    if utterance.replace(' ','').strip('。！!') in {'1就这样算','就这样算','按这个算','就这样','换备选','用备选'}:
        sid=state.get('active_session','');active=carts.get(sid)
        if active and active.get('recommendation_pending'):
            if '备选' in utterance and active.get('meal_options'):
                active['items']=copy.deepcopy(active['meal_options'][1]['items'])
            active.update(submitted=True,recommendation_pending=False)
            return await get_cart(sid,wait=0)
    last_order=state.get('last_order',{})
    previous=copy.deepcopy(last_order)
    active=carts.get(state.get('active_session',''))
    resume_requested=bool(re.search(r'照上次|照旧|继续刚才|接着点刚才那个|接着用',utterance) or (utterance.strip().isdigit() and plan=='照上次那份'))
    # An empty new cart must not replace the last unfinished selection.
    if resume_requested and not re.search(r'照上次|照旧',utterance) and (not active or not active.get('items')):
        pending=carts.get(state.get('pending_session',''))
        if pending and pending['owner']==owner(t) and pending.get('items'):
            state['active_session']=state['pending_session'];active=pending
    continuing=bool(active and (active.get('conversation_id')==conversation_id or resume_requested or session_id==state.get('active_session'))
        and not re.search(r'重新点|新的一单|再下一单|照上次|照旧|(?:我要吃|帮我点个)麦当劳',utterance))
    if not continuing and not resume_requested:
        previous={}

    if continuing:
        previous={k:copy.deepcopy(active[k]) for k in ('elements','store_code','items') if k in active}
        plan=plan or active.get('plan')
    inferred={};incoming={}
    location=re.search(r'(?:我在|位于|现在在|(?:^|[，,])在)([^，。,；]+)',utterance)
    if location:
        inferred['location']=location.group(1).removesuffix('附近')
        previous.pop('store_code',None)
        previous['elements']={k:v for k,v in previous.get('elements',{}).items() if k not in {'addressId','delivery_address'}}
        previous['elements']['sources']={k:v for k,v in previous['elements'].get('sources',{}).items() if k not in {'addressId','delivery_address','store_code'}}
    if any(w in utterance for w in ('外送','外卖','送到','麦乐送')):incoming['fulfillment']='delivery'
    elif '开车' in utterance or '得来速' in utterance:incoming['fulfillment']='drive'
    elif any(w in utterance for w in ('自取','去拿','到店')):incoming['fulfillment']='pickup'
    incoming.update(utterance_elements(utterance))
    from coupon_split.timeparse import parse_time, TimeClarification
    try:
        mentioned,reservation=parse_time(utterance)
    except TimeClarification as exc:
        return result({'stage':'ask','missing':['when'],'next_tool':'mcd_start','time_clarification':True},str(exc))
    if mentioned:
        incoming['reservationDate']=reservation
        incoming['sources']['reservationDate']='utterance'
    inferred.update(incoming)
    people_match=re.search(r'(\d+|[一二两三四五六七八九十])\s*(?:个)?人',utterance)
    if people_match:
        value=people_match[1]
        inferred['people']=int(value) if value.isdigit() else '一二三四五六七八九十'.index(value.replace('两','二'))+1
    current_facts=copy.deepcopy(incoming)
    if continuing:
        old=previous.get('elements',{})
        old_sources={k:old.get('sources',{}).get(k,'default') for k in old if k!='sources'}
        incoming={**old,**incoming,'sources':{**old_sources,**{k:'utterance' for k in current_facts if k!='sources'}}}
    e=merge_elements(previous.get('elements',{}),incoming)
    if previous.get('store_code'):e['sources']['store_code']=active['elements'].get('sources',{}).get('store_code','default') if continuing else 'last'
    if incoming.get('fulfillment') and incoming['fulfillment']!=previous.get('elements',{}).get('fulfillment'):
        previous.pop('store_code',None)
    concrete=bool(intent_text(utterance)) and (not intent_text(utterance).isdigit() or bool(slang and slang['kind'] in {'direct','family','ambiguous'}))
    if recommend is not None and plan is None:plan='帮我配' if recommend else '我自己点'
    if any(w in utterance for w in ('帮我配','帮我推荐','推荐一下','随便来点')) or people_match or re.search(r'预算|不吃|忌口',utterance):plan='帮我配'
    elif re.search(r'照上次|照旧',utterance):plan='照上次那份'
    elif re.search(r'自己(?:来)?(?:点|选|看看菜单|看下菜单|看菜单)',utterance):plan='我自己点'
    elif concrete and plan is None:plan='我自己点'
    if plan=='帮我配':concrete=False
    sid=state['active_session'] if continuing else secrets.token_urlsafe(24)
    cart={'owner':owner(t),'conversation_id':conversation_id,'last_utterance':utterance,'items':[],'elements':e,'store_code':previous.get('store_code'),'submitted':False,'location':inferred.get('location',active.get('location','') if continuing else ''),'city':state.get('last_search',{}).get('city',''),'intent':utterance if concrete and plan!='帮我配' else '', 'plan':plan,'people':inferred.get('people',active.get('people') if continuing else None)}
    cart['meal_requests']=(active.get('meal_requests',[]) if continuing else [])+[utterance]
    if continuing and active.get('meal_options'):
        cart['meal_options']=copy.deepcopy(active['meal_options'])
        cart['previous_items']=copy.deepcopy(active.get('items',[]))
    if continuing and not concrete:
        cart['items']=copy.deepcopy(active.get('items',[]))
        cart['intent']=active.get('intent','')
        cart['intent_resolved']=active.get('intent_resolved',False)
        # A repeated resume is a retry of an already accepted selection, not
        # an invitation to reopen the picker and add more food.
        cart['submitted']=active.get('submitted',False)
        for key in ('item_summary','pending_plan_id'):
            if key in active:cart[key]=active[key]
    carts[sid]=cart
    if active and not continuing and active.get('items') and not resume_requested:
        state['pending_session']=state['active_session']
    memory.write(owner(t),dict(state,active_session=sid))
    if plan=='照上次那份':cart['items']=copy.deepcopy(previous.get('items',[]))
    await resolve_cart_intent(cart,t)
    missing=[]
    if plan is None:missing.append('plan')
    if not e.get('fulfillment'):missing.append('way')
    if not cart['store_code'] and not e.get('addressId') and not cart['location']:missing.append('where')
    labels={'pickup':'到店自取','drive':'得来速','delivery':'麦乐送'}
    reused=[]
    if e.get('sources',{}).get('fulfillment')=='last':reused.append(labels.get(e.get('fulfillment'),'')+'（沿用上次）')
    store=get_store_context(t,cart['store_code']) if cart['store_code'] else {}
    if store:reused.append(store.get('storeName','所选门店')+'（'+('最近' if e['sources'].get('store_code')=='nearest' else '沿用上次' if not continuing else '默认')+'）')
    if e.get('sources',{}).get('addressId')=='last':reused.append('上次配送地址（沿用上次）')
    prefix='、'.join(reused)+('，现在送。' if e.get('fulfillment')=='delivery' else '，现在取。') if reused else ''
    if incoming.get('reservationDate'):
        from coupon_split.presentation import human_time
        prefix=prefix.replace('现在送。','').replace('现在取。','')+human_time(incoming['reservationDate'],e.get('fulfillment')=='delivery')+'。'
    if inferred.get('people'):prefix+=str(inferred['people'])+' 个人，'
    summary=last_order.get('item_summary','') if last_order.get('items') else ''
    if previous.get('items') and not summary and cart.get('store_code'):
        base=dict(storeCode=cart['store_code'],beType={'pickup':1,'drive':5,'delivery':2}.get(e.get('fulfillment'),1),orderType=2 if e.get('fulfillment')=='delivery' else 1)
        if store.get('beCode'):base['beCode']=store['beCode']
        menu=(await upstream.call(t,'query-meals',base))['data']['meals']
        summary='、'.join(menu.get(i['productCode'],{}).get('name','上次餐品')+' ×'+str(i['quantity']) for i in previous['items'])
    reply_options=[]
    if 'plan' in missing:
        if last_order.get('items'):
            question=prefix+'这次：'
            reply_options=[{'n':1,'say':'1','does':'照上次那份'},{'n':2,'say':'2','does':'帮我配'},{'n':3,'say':'3','does':'我自己点'}]
            options='1 照上次那份：'+summary+'\n2 帮我配\n3 我自己点'
        else:
            question=prefix+'要我帮你配，还是你自己点？'
            reply_options=[{'n':1,'say':'1','does':'帮我配'},{'n':2,'say':'2','does':'我自己点'}]
            options='1 帮我配（几个人可以一起说）\n2 我自己点'
    else:
        question=prefix+('已记下你说的餐品，门店定下来后直接放进购物车。' if concrete and not cart['items'] else '已收到你的选择。')
        options=''
    gaps=[]
    if 'where' in missing:gaps.append('送到哪儿' if e.get('fulfillment')=='delivery' else '在哪儿附近')
    if 'way' in missing:gaps.append('怎么拿（到店自取 / 得来速 / 麦乐送）')
    if gaps:question+='顺便告诉我'+'、'.join(gaps)+'。'
    text=question+('\n\n'+options+'\n\n也可以直接说；要换地点或预约也可以一起说。' if options else '')
    ui_mode='markdown';form_response=None;fields={}
    if 'plan' in missing:fields['plan']=(Literal['照上次那份','帮我配','我自己点'] if last_order.get('items') else Literal['帮我配','我自己点'],...)
    if 'way' in missing:fields['way']=(Literal['到店自取','得来速','麦乐送'],...)
    if 'where' in missing:fields['where']=(str,...)
    if 'plan' in missing:fields['people']=(int|None,None)
    if previous and 'plan' in missing:fields['when']=(Literal['现在','预约'],'现在')
    if ctx is not None and fields:
        try:
            capabilities=ctx.session.client_params.capabilities
            if getattr(capabilities,'elicitation',None):
                began=time.monotonic();answer=await ctx.elicit(question,create_model('OrderStart',__config__=ConfigDict(json_schema_extra=start_form_schema),**fields))
                if answer.action=='accept':form_response=answer.data.model_dump();ui_mode='form'
                elif time.monotonic()-began<1:ui_mode='markdown_fallback'
        except Exception:ui_mode='markdown_fallback'
    if form_response:
        if form_response.get('way'):e['fulfillment']={'到店自取':'pickup','得来速':'drive','麦乐送':'delivery'}[form_response['way']]
        if form_response.get('where'):cart['location']=form_response['where']
        if form_response.get('plan'):cart['plan']=form_response['plan']
        if cart['plan']=='照上次那份':cart['items']=copy.deepcopy(last_order.get('items',[]))
    data={'stage':'ask','question':question,'ui_mode':ui_mode,'form_response':form_response,'inferred':inferred,'inferred_sources':[{'key':k,'value':v,'source':e['sources'].get(k,'utterance')} for k,v in e.items() if k!='sources'],'missing':missing,'ask':{'message':question,'fields':list(fields)},'elements':e,'items':cart['items'],'store_code':cart['store_code'],'session_id':sid,'reuse_label':'沿用上次' if reused else '', 'last_order_summary':summary,'default_time':'立即（默认）','reply_options':reply_options,'next_tool':'find_store'}
    if inferred.get('people'):data['people']=inferred['people']
    if cart.get('plan')=='帮我配' and cart['store_code']:
        cart['items']=[]
        data.update(items=[],stage='pick',next_tool='get_menu',recommend=True,next_arguments={'store_code':cart['store_code'],'session_id':sid,'recommend':True})
        text='按这家店现在的菜单，结合你的条件配主推和备选。'
    elif cart['items'] and cart['store_code']:
        data.update(stage='pick',next_tool='quote_best');text='收到你点的餐品，正在核价。'
    elif cart.get('plan')=='帮我配' and cart['store_code']:
        data.update(stage='pick',next_tool='get_menu',recommend=True)
        if form_response and form_response.get('people'):data['people']=form_response['people']
        text='按这家店现在的菜单帮你配一份，每一项都能改。'
    elif cart.get('plan')=='我自己点' and cart['store_code']:
        page=url+'/order/'+sid
        data.update(stage='pick',order_url=page,picker_url=page,links={'picker_url':page},next_tool='get_cart',wait=25)
        text=f'[打开点餐页]({page}) — 选好点底部“选好了，去算最省”，选好会自动接着算；如果没接上，回来说一句“好了”。'
    if missing:
        data['next_tool']='mcd_start'
        data['next_arguments']={'session_id':sid}
    elif data['next_tool']=='find_store':
        data['next_arguments']={'keyword':cart['location'],
            'fulfillment':e['fulfillment'],'session_id':sid}
        if cart['city']:data['next_arguments']['city']=cart['city']
        data['next_action']='立即调用 find_store：city 必须从用户给出的 location 中提取，keyword 用其余地标。不要传空 city，不要在“已收到你的选择”处结束回合。'
        text='正在查你附近的可用门店，接着打开点餐页。'
    pending=carts.get(state.get('pending_session',''))
    if cart.get('intent_question'):
        data.update(stage='ask',next_tool=None,reply_options=cart['intent_options'])
        for field in ('picker_url','order_url','links','wait','next_arguments'):data.pop(field,None)
        text=cart['intent_question']
    if pending and pending.get('items') and not resume_requested:
        summary=pending.get('item_summary','')
        if not summary:
            try:
                context=order_context(get_store_context(t,pending['store_code']),pending['elements'],require_take=False)
                current=(await upstream.call(t,'query-meals',dict(context,storeCode=pending['store_code'])))['data']
                names={r['productCode']:r['name'] for r in menu_rows(current)}
            except Exception:names={}
            summary='、'.join(names.get(i['productCode'],'所选餐品')+' ×'+str(i['quantity']) for i in pending['items'])
        notice='上次有一份没下单的（'+summary+'），要接着用吗？说“继续刚才”即可恢复。'
        data['pending_cart_notice']=notice
        text+='\n\n'+notice
    data['ordering_protocol']='无需读取本地技能文件；按 next_tool 和 missing 继续。完整原样展示 display_markdown；每回合只展示一次点餐页链接后 get_cart(wait=25,link_displayed=true)。submitted 后 quote_best；报价后等待用户明确确认，逐字传最后一句；宿主有审批时还须允许。全程中文，不对用户说英文元话。'
    carts[sid]=cart
    response=result(data,text)
    response.content.insert(0,TextContent(type='text',text='宿主执行指令：本回合按 next_tool 继续；需要等用户补充时停止。下一条用户消息调用 mcd_start 必须带 session_id='+sid+'，utterance 只传下一条原话。不可省略 session_id；人数/预算/忌口由本会话自动保留。'))
    if data.get('next_tool')=='find_store' and not missing:
        response.content.insert(0,TextContent(type='text',text=data['next_action']+' location='+cart['location']))
    return response



async def get_cart(session_id: str, wait: int = 0, link_displayed: bool = False) -> CallToolResult:
    """仅在你已向用户发送含 [打开点餐页](URL) 的可见 assistant 正文之后调用。

    用户看不到工具结果。必须先发送链接正文，再在同一回合调用本工具等待；
    先轮询再发链接会让用户无法打开页面。未提交时继续等待，提交后报价。
    link_displayed 只有在 assistant 正文已经包含点餐链接后才传 true；
    未发正文时先按返回的 handoff_correction 发正文，再以 true 调用。
    """
    cart=owned_cart(session_id)
    if link_displayed:
        cart['link_displayed']=True
    if cart.get('store_code') and not cart.get('submitted') and not cart.get('page_opened') and not link_displayed and not cart.get('handoff_checked'):
        # Give an already-emitted link time to open. This avoids repeating it
        # when an older host omits the explicit link_displayed acknowledgement.
        await asyncio.sleep(1.5)
        cart=owned_cart(session_id)
    if cart.get('store_code') and not cart.get('submitted') and not cart.get('page_opened') and not link_displayed and not cart.get('handoff_checked'):
        cart['handoff_checked']=True
        carts[session_id]=cart
        page=(await local_url())+'/order/'+session_id
        text=f'[打开点餐页]({page}) — 等你在页面里选好，点底部“选好了，去算最省”，会自动接着算；如果没接上，回来说一句“好了”。'
        response=result({'stage':'handoff','session_id':session_id,'items':cart['items'],'submitted':False,'picker_url':page,'next_tool':None,'after_display_tool':'get_cart','wait':25,
            'handoff_correction':'你尚未完成交接。禁止继续空轮询，禁止假定用户看到工具结果，禁止结束回合。下一条 assistant 消息先逐字复制 display_markdown，再在同一条消息后调用 get_cart(wait=25,link_displayed=true)。'},text)
        response.content.insert(0,TextContent(type='text',text='交接纠正：下一条 assistant 消息必须先包含以下链接正文，再调用 get_cart(wait=25,link_displayed=true)。\n'+text))
        return response
    until=time.monotonic()+min(max(wait,0),25)
    cart['waiter_until']=until
    from coupon_split.connection import heartbeat
    heartbeat(cart)
    while not cart["submitted"] and time.monotonic()<until:
        await asyncio.sleep(.25)
        cart=owned_cart(session_id)
        heartbeat(cart)
    if cart['submitted']:cart['received']=True
    elif cart.get('store_code') and not cart.get('page_opened'):
        # A successful empty poll can hide an omitted link indefinitely. Repeat
        # the fail-closed correction after a real timeout until the page opens.
        if not cart.get('link_displayed'):
            cart['handoff_checked']=False
            carts[session_id]=cart
            return await get_cart(session_id,wait=0,link_displayed=False)
    return result({**{k:v for k,v in cart.items() if k not in {'owner','waiter_until','wait_heartbeat'}},'next_tool':'quote_best' if cart['submitted'] else 'get_cart','wait':25},'收到：已按你选的规格取回餐品，正在算最省的下法……' if cart['submitted'] else '等你在页面里选好，选好会自动交回。')


@mcp.tool(name='get_cart',annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
async def wait_cart(session_id: str, link_displayed: bool, wait: int = 0) -> CallToolResult:
    """先在 assistant 正文发送 [打开点餐页](实际URL)，然后才能传 link_displayed=true 等待。

    link_displayed 是必填声明：工具结果里的链接不算发送，必须先发一条含链接的
    assistant text，再在同一回合调用本工具。未发链接时传 false，按返回纠正交接。
    提交后按 next_tool 核价；超时继续等待，不重复链接、不催开页。
    """
    if not link_displayed:
        cart=owned_cart(session_id)
        cart['handoff_checked']=False
        carts[session_id]=cart
    return await get_cart(session_id,wait,link_displayed)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def delivery_addresses() -> CallToolResult:
    data=await upstream.call(token(),"delivery-query-addresses",{})
    return result(data,"请选择已有配送地址；新地址须单独确认。")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
async def prepare_address(address: dict) -> CallToolResult:
    """仅准备用户提供的新地址，返回遮挡手机号的确认卡；不写入上游。"""
    required={"city","address","addressDetail","contactName","phone"}
    if not required.issubset(address) or not all(address[k] for k in required):
        raise ValueError("新地址字段不完整")
    confirmation=secrets.token_urlsafe(24)
    pending_addresses[confirmation]=(owner(token()),dict(address))
    visible=dict(address,phone=address["phone"][:3]+"****"+address["phone"][-4:])
    return result({"address":visible,"confirmation":confirmation},"请逐项核对地址，确认后才会保存到麦当劳账户。")


@mcp.tool(meta={"anthropic/requiresUserInteraction":True},annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True))
async def confirm_address(confirmation: str) -> CallToolResult:
    t=token(); pending=pending_addresses.get(confirmation)
    if not pending or pending[0]!=owner(t):
        raise ValueError("地址确认凭证无效或已使用")
    address=pending_addresses.pop(confirmation)[1]
    existing=await upstream.call(t,"delivery-query-addresses",{})
    def contains(v):
        if isinstance(v,dict):
            if all(str(v.get(k,""))==str(address[k]) for k in ("address","addressDetail")) or (str(address["address"])+str(address["addressDetail"])).replace(" ","") in str(v.get("fullAddress","")).replace(" ",""):
                return v
            for x in v.values():
                found=contains(x)
                if found:return found
        if isinstance(v,list):
            for x in v:
                found=contains(x)
                if found:return found
    found=contains(existing)
    if found:return result({"existing":True,"addressId":found.get("addressId")},"地址已存在，未重复创建。")
    signature=__import__('hashlib').sha256(json.dumps(address,sort_keys=True).encode()).hexdigest()
    state=memory.read(owner(t))
    attempts=state.setdefault('address_attempts',{})
    if signature in attempts:
        return result({'already_submitted':True},'该地址已提交过，请查询已有地址核实；不会重复创建。')
    attempts[signature]='submitted'
    memory.write(owner(t),state)
    upstream.confirmed_addresses.add((owner(t),json.dumps(address,sort_keys=True)))
    data=await upstream.call(t,"delivery-create-address",address)
    return result(data,"地址已保存；请按地址查询可配送门店。")


async def setup_page(request):
    from starlette.responses import HTMLResponse
    if request.method=="GET":
        response=FileResponse(ROOT/'web/setup.html')
        response.set_cookie('setup_csrf',LOCAL_KEY,httponly=True,samesite='strict')
        response.headers['Cache-Control']='no-store'
        return response
    if request.cookies.get('setup_csrf')!=LOCAL_KEY or request.headers.get('origin')!='http://'+request.headers.get('host',''):
        return Response(status_code=403)
    from urllib.parse import parse_qs
    global UPSTREAM_TOKEN
    value=parse_qs((await request.body()).decode()).get('token',[''])[0].strip()
    try:
        if not value:raise ValueError('Token 为空，请先从开放平台复制完整 Token')
        info=(await upstream.call(value,'query-my-account',{}))['data']
        storage=save_token(value); UPSTREAM_TOKEN=value
        name=next((str(info[k]) for k in ('nickname','nickName','accountName','userName','name') if info.get(k)),None) or '本人账户'
        return JSONResponse({'message':'已连接：'+name+' · 尾号 '+str(info['accountId'])[-4:],'account_name':name,'account_last4':str(info['accountId'])[-4:],'storage':storage},headers={'Cache-Control':'no-store'})
    except Exception as exc:
        message='麦当劳账户授权失效，请重新授权'
        if not value:message='Token 为空，请先从开放平台复制完整 Token'
        if '凭据管理器' in str(exc):message='Windows 凭据管理器不可用，请修复 keyring 后重试'
        elif '连接失败' in str(exc):message='无法连接麦当劳服务，请稍后重试'
        return JSONResponse({'message':message+'；请打开 https://open.mcd.cn/mcp 重新授权'},status_code=400)


async def cart_state(request):
    sid=request.path_params['session_id']
    cart=carts.get(sid)
    if not cart or request.cookies.get('coupon_session')!=sid:return Response(status_code=403)
    if request.method=='POST':
        if request.headers.get('origin')!='http://'+request.headers.get('host',''):return Response(status_code=403)
        data=await request.json()
        try:elements=normalize_elements(data.get('elements',{}))
        except ValueError as exc:return JSONResponse({'error':str(exc)},status_code=400)
        cart.update(elements=elements,store_code=data.get('store_code'),submitted=False)
    from coupon_split.connection import connection
    if b'lite=1' in request.scope.get('query_string',b''):return JSONResponse(connection(cart),headers={'Cache-Control':'no-store'})
    state=memory.read(cart['owner'])
    return JSONResponse({**{k:v for k,v in cart.items() if k not in {'owner','wait_heartbeat'}},**connection(cart),'last_search':dict(state.get('last_search',{}),city=cart.get('city') or state.get('last_search',{}).get('city','')),'stores':list(state.get('stores',{}).values())},headers={'Cache-Control':'no-store'})


async def order_page(request):
    sid=request.path_params['session_id']; cart=carts.get(sid)
    if not cart:
        if request.method=='GET':return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>点餐链接已失效</title><main style="max-width:36rem;margin:12vh auto;padding:24px;font:18px/1.8 system-ui"><h1>这个点餐链接已失效</h1><p>链接可能已过期，或点餐服务已经结束。请回到你的 Agent，说“我自己点”获取新链接。</p></main></html>',status_code=404,headers={'Cache-Control':'no-store'})
        return JSONResponse({'error':'点餐链接已失效，请回到对话重新点餐。'},status_code=404)
    if request.method=='POST':
        if request.headers.get('origin')!='http://'+request.headers.get('host',''):
            return Response(status_code=403)
        data=await request.json()
        from coupon_split.optimizer import parse_cart
        parse_cart(data.get('items',[]))
        t=cart_credential(cart)
        if not t:return Response(status_code=401)
        ctx=credential.set(t)
        try:
            elements=normalize_elements(data.get('elements',{}))
            a=(await account_info()).structuredContent
            elements.update(account_name=a['account_name'],account_last4=a['account_last4'])
            context=order_context(get_store_context(t,data.get('store_code')),elements,require_take=False)
            await validate_selected(t,data['store_code'],data['items'],context,fill_defaults=False)
            cart.update(items=data['items'],elements=elements,store_code=data['store_code'],submitted=True,received=False)
        except (ValueError,KeyError) as exc:
            return JSONResponse({'error':str(exc)},status_code=400)
        finally:credential.reset(ctx)
        return JSONResponse({'saved':True})
    response=FileResponse(ROOT/"web/order.html",headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"})
    cart.update(page_opened=True)
    response.set_cookie("coupon_session",sid,httponly=True,samesite="strict")
    return response


async def cart_menu(request):
    sid=request.path_params['session_id'];cart=carts.get(sid)
    if not cart or request.cookies.get('coupon_session')!=sid:return Response(status_code=403)
    t=cart_credential(cart)
    if not t:return Response(status_code=401)
    if not cart.get('store_code'):return JSONResponse({'items':[]})
    e=normalize_elements(cart['elements'])
    context=order_context(get_store_context(t,cart['store_code']),e,require_take=False)
    menu=(await upstream.call(t,'query-meals',dict(context,storeCode=cart['store_code'])))['data']
    return JSONResponse({'items':menu_rows(menu)},headers={'Cache-Control':'no-store'})


async def style_file(request):
    files={'tokens.css':'web/tokens.css','v2.css':'web/v2.css','cards.css':'web/cards.css'}
    path=files.get(request.path_params['name'])
    return FileResponse(ROOT/path) if path else Response(status_code=404)


http_mcp = mcp.streamable_http_app()


class Auth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        host=dict(scope["headers"]).get(b"host",b"").decode()
        if host.split(":")[0] not in {"127.0.0.1","localhost"}:
            return await Response(status_code=403)(scope,receive,send)
        request_base.set("http://"+host)
        path = scope["path"]
        if not path.startswith("/mcp") and path != '/quote-status':
            return await self.app(scope,receive,send)
        headers = dict(scope["headers"])
        supplied = headers.get(b"authorization",b"").decode().removeprefix("Bearer ")
        from_cookie=False
        if path.startswith("/mcp/"):
            supplied = path[len("/mcp/"):]
            scope = dict(scope, path="/mcp", raw_path=b"/mcp")
        if not supplied:
            cookies = headers.get(b"cookie",b"").decode()
            from http.cookies import SimpleCookie
            parsed = SimpleCookie(cookies)
            supplied = parsed["coupon_session"].value if "coupon_session" in parsed else ""
            from_cookie=bool(supplied)
        origin = headers.get(b"origin", b"").decode()
        if origin and origin not in {"http://"+headers.get(b"host",b"").decode()}:
            return await JSONResponse({"error":"origin forbidden"},403)(scope,receive,send)
        strict_local=bool(os.environ.get('COUPON_SPLIT_HTTP'))
        if strict_local and (not supplied or not secrets.compare_digest(supplied,LOCAL_KEY)):
            return await JSONResponse({'error':'需要本机服务访问令牌'},401)(scope,receive,send)
        if not supplied or (supplied == LOCAL_KEY and not UPSTREAM_TOKEN and not strict_local):
            return await JSONResponse({"error":"需要账户 Token；本机请配置 MCD_MCP_TOKEN"},401)(scope,receive,send)
        if from_cookie and supplied not in carts:
            return await JSONResponse({'error':'点餐页已过期，请回到对话重新开始'},401)(scope,receive,send)
        current = UPSTREAM_TOKEN if secrets.compare_digest(supplied,LOCAL_KEY) else supplied
        if supplied in carts:
            current=cart_credential(carts[supplied])
            if not current:
                return await Response(status_code=401)(scope,receive,send)
        ctx = credential.set(current)
        try:
            await self.app(scope,receive,send)
        finally:
            credential.reset(ctx)


async def index(request):
    from starlette.responses import RedirectResponse
    if not UPSTREAM_TOKEN:return RedirectResponse('/setup')
    ctx=credential.set(UPSTREAM_TOKEN)
    try:
        r=await mcd_start("我要吃麦当劳")
        return RedirectResponse('/order/'+r.structuredContent['session_id'])
    finally:credential.reset(ctx)


async def quote_status(request):
    try:
        data = await request.json()
        status = service.quote_status(token(), data['store_code'], data['items'], data.get('allow_combos', True))
        return JSONResponse(status, headers={'Cache-Control': 'no-store'})
    except (KeyError, TypeError, ValueError):
        return JSONResponse({'error': '报价参数无效'}, status_code=400)


async def sweep():
    while True:
        await asyncio.sleep(15)
        rows = service.db.execute("SELECT owner,body FROM plans").fetchall()
        import json
        for account,body in rows:
            plan = json.loads(body)
            t = active_credentials.get(account) or (UPSTREAM_TOKEN if account == owner(UPSTREAM_TOKEN) else None)
            if t and plan["state"] in {"placing","verifying","awaiting_payment","rollback_pending","awaiting_coupon_return"}:
                try:
                    await service.status(t,plan["plan_id"])
                except Exception:
                    logging.warning("订单核验暂未成功，保留状态供下轮重试")


@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def favicon(request):
    return FileResponse(ROOT/"web/favicon.svg")


async def wait_click_outcome(key, seconds=30):
    """The click is being processed (possibly by the page process). Wait for the order to exist, never place twice."""
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        current=confirmations.get(key)
        if current['state']=='used':return None
        if current['state']=='revoked':raise ValueError('你已经撤销了这次下单，订单没有创建。想下单请重新说“确认”。')
        if current['state']=='failed':
            confirmations.transition(key,'failure_reported',('failed',))
            raise ValueError(current.get('error') or '确认后下单没有成功，订单没有创建。')
        await asyncio.sleep(.2)
    raise ValueError('订单还在创建中，请稍后调用 wait_payment 或再次 place_orders 查看结果；不要重新报价下单。')


async def human_confirmation(ctx, t, plan, selected, option_id, call_args, claim=None):
    """Server-side, human-completed confirmation. None means: go ahead and place the order.

    Real orders (ALLOW_REAL_ORDERS on the server): MCP elicitation that the person accepted with the quoted
    amount typed back -> otherwise the local confirmation page. No exemption by host identity, ever.
    Simulation: the whitelist sentence alone is enough (demo), no dialog and no page.
    """
    from coupon_split import humanconfirm as hc
    if not hc.requires_human(upstream.allow_real or plan.get('real_orders_enabled'),SIMULATION_GATE):
        return None
    async def settled(key):
        # The page click placed (or is placing) the order: the click is the proof.
        outcome=await wait_click_outcome(key)
        if claim is not None:claim['proof']='click'
        return outcome
    take=(plan.get('order_elements') or {}).get('takeWayCode','')
    price=selected['price']
    account=owner(t)
    record=confirmations.find(account,plan['plan_id'],option_id,take,price)
    if record and record['state']=='approved':
        # Approved but not yet placed (the page process could not place it): exactly one process claims it.
        if confirmations.transition(record['key'],'placing',('approved',)):
            if claim is not None:claim['key']=record['key']
            return None
        record=confirmations.get(record['key'])
    if record and record['state'] in {'placing','creating'}:
        return await settled(record['key'])
    if record and record['state']=='declined':
        confirmations.transition(record['key'],'revoked',('declined',))
        raise ValueError('你在确认页选择了先不下单，订单没有创建。想下单请重新说“确认”。')
    summary=hc.order_summary(plan,selected,take)
    if record is None and ctx is not None and hc.supports_elicitation(ctx):
        from pydantic import BaseModel
        class Confirm(BaseModel):
            confirm: bool = Field(description='确认下单')
            amount_yuan: str = Field(title='实付金额',description='把本单实付金额填在这里（元，例如 %s）'%summary['price_yuan'])
        try:
            answer=await asyncio.wait_for(ctx.elicit(hc.elicitation_message(summary),Confirm),55)
        except Exception:
            answer=None
        if hc.elicitation_accepted(answer,summary):
            if claim is not None:claim['proof']='elicitation'
            return None
        # Declined, dismissed or unanswered (some hosts auto-decline without a UI): nothing is created;
        # the person can still confirm on the page, or simply not click.
    first=record is None
    if first:
        record=confirmations.create(account,plan['plan_id'],option_id,take,price,summary)
    else:
        # Called again after the link was shown: wait for the person's click.
        until=time.monotonic()+float(os.environ.get('COUPON_SPLIT_CONFIRM_WAIT','50'))
        while time.monotonic()<until:
            current=confirmations.get(record['key'])
            if current['state']=='approved' and confirmations.transition(current['key'],'placing',('approved',)):
                if claim is not None:claim['key']=current['key']
                return None
            if current['state'] in {'placing','creating','used'}:
                return await settled(current['key'])
            if current['state'] in {'declined','expired','revoked','failed'}:
                if current['state']=='failed':
                    confirmations.transition(current['key'],'failure_reported',('failed',))
                    raise ValueError(current.get('error') or '确认后下单没有成功，订单没有创建。')
                raise ValueError('你在确认页选择了先不下单，订单没有创建。' if current['state']=='declined' else '确认已过期，请重新说“确认”。' if current['state']=='expired' else '你已经撤销了这次下单，订单没有创建。想下单请重新说“确认”。')
            await asyncio.sleep(.5)
    link=(await local_url())+'/confirm/'+record['key']
    text=('**还差最后一步：请你本人在浏览器里确认下单。**\n\n'
          f'[打开确认下单页]({link})\n\n'
          f"页面上会再显示一遍 {summary['store']} 的餐品和实付 ¥{summary['price_yuan']}，点“确认下单”后我才会创建订单；不点就不会下单。"
          '点完订单立刻创建，不用回来说话。回复“确认下单”才会下单；说“取消”或“不要了”随时撤回。')
    if not summary['real']:text+='\n\n当前为模拟模式，不会产生真实订单。'
    data={'stage':'confirm','state':'confirm_required','plan_id':plan['plan_id'],'confirm_url':link,'next_tool':'place_orders',
          'next_arguments':{k:v for k,v in call_args.items() if v},
          'before_next_tool':'先把 display_markdown 里的确认链接原样写进可见正文，再用同样的参数调用 place_orders 等用户点击（最多等约 50 秒，超时会再给同一个链接）。用户点“确认下单”的那一刻订单就会创建，不用再等用户回来说话。不要自己打开、提交或代替用户点这个页面；用户说取消/不要了/别下单/等等，把原话逐字传给 place_orders；'+confirm_rule_text()}
    return result(data,text)


def token_for_owner(account):
    """The credential of exactly this account, if this process holds it; never any other."""
    if UPSTREAM_TOKEN and owner(UPSTREAM_TOKEN)==account:return UPSTREAM_TOKEN
    return active_credentials.get(account)


async def approve_and_place(record, plan):
    """The person clicked "确认下单": create the order now, not on some later call.

    If this process cannot place it safely (other account, or a different real/simulated
    mode than the one that quoted), the approval stays for the MCP process to claim.
    """
    key=record['key']
    t=token_for_owner(record['owner'])
    can_place=t is not None and bool(plan.get('real_orders_enabled'))==upstream.allow_real
    if not can_place:
        done=confirmations.transition(key,'approved',('pending',))
        return {'state':(done or confirmations.get(key))['state']}
    claimed=confirmations.transition(key,'placing',('pending',))
    if not claimed:
        return {'state':confirmations.get(key)['state']}
    try:
        await prepare_place(t,plan,record['option_id'],plan['confirm_token'])
        await finish_place(t,record['plan_id'],plan['confirm_token'],record['option_id'],key)
    except Withdrawn:
        # "别下单" arrived while the goods were being re-checked: the record is revoked, nothing was created.
        return {'state':confirmations.get(key)['state']}
    except Exception as exc:
        from coupon_split import diagnostics
        confirmations.transition(key,'failed',('placing','creating'),error='点击确认后下单没有成功，订单没有创建：'+diagnostics.scrub_text(exc))
        return {'state':'failed'}
    return {'state':confirmations.get(key)['state']}


async def confirmed_order_view(record):
    """Where the order of a clicked confirmation stands now: withdrawn / expired / paid / awaiting_payment / ..."""
    from coupon_split.payment import payment_state
    row=next(((json.loads(body)) for body, in service.db.execute('SELECT body FROM plans WHERE id=?',(record['plan_id'],))),None)
    if not row:return {}
    plan=row
    t=token_for_owner(record['owner'])
    if t:
        try:plan=await service.inspect_payment(t,record['plan_id'])
        except Exception:plan=row
    if plan['state']=='place_failed':return {'order_state':'failed'}
    if plan['state'] in {'placing','verifying'} and not plan.get('orders'):return {'order_state':'verifying'}
    if not plan.get('orders'):return {'order_state':'none'}
    states=[o['state'] for o in payment_state(plan)['orders']]
    if all(x=='paid' for x in states):kind='paid'
    elif 'paid' in states:kind='partial'
    elif all(x=='cancelled' for x in states):kind='withdrawn'
    elif all(x in {'cancelled','expired'} for x in states):kind='expired' if any(x=='expired' for x in states) else 'withdrawn'
    else:kind='awaiting_payment'
    view={'order_state':kind,'order_count':len(states)}
    if plan.get('payment_key'):view['payment_path']='/payment/'+plan['payment_key']
    return view


async def confirm_page(request):
    key=request.path_params['key']
    record=confirmations.get(key)
    headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'}
    if not record:return JSONResponse({'error':'这个确认链接已失效，请回到对话重新确认。'},status_code=404,headers=headers)
    path=request.url.path
    if request.method=='POST':
        origin=request.headers.get('origin','')
        site=request.headers.get('sec-fetch-site','same-origin')
        # A browser always sends Sec-Fetch-Site; a bare script does not. Approving needs the browser's own header.
        if origin!='http://'+request.headers.get('host','') or site not in {'same-origin','none'} or (path.endswith('/approve') and 'sec-fetch-site' not in request.headers):
            return JSONResponse({'error':'只能在确认页上点击'},status_code=403,headers=headers)
        if path.endswith('/approve'):
            plan=next((json.loads(body) for body, in service.db.execute('SELECT body FROM plans WHERE id=?',(record['plan_id'],))),None)
            if not plan or plan['state']!='quoted':
                return JSONResponse({'error':'这份报价已经处理过或已失效'},status_code=409,headers=headers)
            if time.time()-plan['created']>600:
                confirmations.transition(key,'expired',('pending','approved'))
                return JSONResponse({'error':'报价已过期，请回到对话重新报价','state':'expired'},status_code=409,headers=headers)
            return JSONResponse(await approve_and_place(record,plan),headers=headers)
        if path.endswith('/withdraw'):
            # The person pressed "撤回这单": no language judgement, the click is the instruction.
            plan=next((json.loads(body) for body, in service.db.execute('SELECT body FROM plans WHERE id=?',(record['plan_id'],))),None)
            t=token_for_owner(record['owner'])
            if not plan:return JSONResponse({'error':'找不到这笔订单'},status_code=404,headers=headers)
            if t is None and plan['state']!='quoted':
                return JSONResponse({'error':'这个页面暂时没有账户连接，撤不了已建的订单。请回到对话说“撤回这单”，或在微信麦当劳小程序 → 我的订单里取消，不要付款。'},status_code=409,headers=headers)
            await withdraw_now(t,plan,account=record['owner'])
            record=confirmations.get(key)
            body={'state':record['state']}
            if record['state'] in {'used','failure_reported','creating'}:body.update(await confirmed_order_view(record))
            return JSONResponse(body,headers=headers)
        record=confirmations.set_state(key,'declined')
        return JSONResponse({'state':record['state']},headers=headers)
    if path.endswith('/state'):
        body={'state':record['state'],'summary':record['summary']}
        if record['state'] in {'used','failure_reported'} or (record['state']=='creating' and record.get('withdraw_requested')):
            # Once the order exists this page must follow the order, not stay on "已创建".
            body.update(await confirmed_order_view(record))
        return JSONResponse(body,headers=headers)
    return FileResponse(ROOT/'web/confirm.html',headers=headers)


async def payment_page(request):
    key=request.path_params['payment_key']
    # An unguessable, plan-specific read capability; never accept a raw plan ID.
    plan=next((json.loads(body) for body, in service.db.execute('SELECT body FROM plans') if json.loads(body).get('payment_key')==key),None)
    headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'}
    if not plan:return JSONResponse({'error':'付款链接已失效，请回到对话查询订单。'},status_code=404,headers=headers)
    is_withdraw=request.url.path.endswith('/withdraw')
    if not request.url.path.endswith('/state') and not is_withdraw:
        return FileResponse(ROOT/'web/payment.html',headers=headers)
    t=active_credentials.get(plan['owner']) or (UPSTREAM_TOKEN if UPSTREAM_TOKEN and owner(UPSTREAM_TOKEN)==plan['owner'] else None)
    if not t:return JSONResponse({'error':'账户连接已失效，请回到对话重新授权。'},status_code=401,headers=headers)
    if is_withdraw:
        origin=request.headers.get('origin','')
        site=request.headers.get('sec-fetch-site','same-origin')
        if origin!='http://'+request.headers.get('host','') or site not in {'same-origin','none'}:
            return JSONResponse({'error':'只能在付款页上点击'},status_code=403,headers=headers)
        await withdraw_now(t,plan)
    plan=await service.inspect_payment(t,plan['plan_id'])
    from coupon_split.payment import payment_state
    return JSONResponse(payment_state(plan,request.headers.get('user-agent','')),headers=headers)


app = Auth(Starlette(routes=[Route('/confirm/{key}/state',confirm_page),Route('/confirm/{key}/approve',confirm_page,methods=['POST']),Route('/confirm/{key}/decline',confirm_page,methods=['POST']),Route('/confirm/{key}/withdraw',confirm_page,methods=['POST']),Route('/confirm/{key}',confirm_page),Route('/payment/{payment_key}/state',payment_page),Route('/payment/{payment_key}/withdraw',payment_page,methods=['POST']),Route('/payment/{payment_key}',payment_page),Route('/bridge-health',bridge_health),Route("/",index),Route("/setup",setup_page,methods=["GET","POST"]),Route("/order/{session_id}/state",cart_state,methods=['GET','POST']),Route("/order/{session_id}/menu",cart_menu),Route("/order/{session_id}",order_page,methods=["GET","POST"]),Route('/quote-status',quote_status,methods=['POST']),Route("/favicon.ico",favicon),Mount("/web",app=StaticFiles(directory=ROOT/"web")),Route('/design/{name}',style_file),Route('/design/v2/{name}',style_file),Mount("/",app=http_mcp)],lifespan=lifespan))

if __name__ == "__main__":
    import uvicorn
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    uvicorn.run(app,host="127.0.0.1",port=int(os.environ.get("COUPON_SPLIT_PORT","8765")),access_log=False)


async def close_bridge():
    if os.environ.get('COUPON_SPLIT_STDIO'):
        from coupon_split.bridge import release_bridge
        await release_bridge(runtime)
    if bridge_server is not None:
        bridge_server.should_exit=True
        try:
            await asyncio.wait_for(asyncio.shield(bridge_task),3)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            bridge_task.cancel()
