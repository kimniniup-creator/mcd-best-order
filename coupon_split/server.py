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
from starlette.responses import FileResponse, Response, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from coupon_split.optimizer import DEFAULT_CART, atom, validate_quantities
from coupon_split.elements import OrderElements, normalize_elements, merge_elements, utterance_elements
from coupon_split.menu import menu_rows, matches, intent_text, resolve_intent, default_item, recommend_rows, AmbiguousIntent
from coupon_split.presentation import markdown, public, money, distance, quote_summary, option_difference
from coupon_split.service import Service, owner, deadline
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
runtime = state_dir() if os.environ.get("COUPON_SPLIT_STDIO") else ROOT / ".runtime"
runtime.mkdir(exist_ok=True)
access_file = runtime / "access.key"
if not access_file.exists():
    access_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
LOCAL_KEY = access_file.read_text(encoding="utf-8").strip()
UPSTREAM_TOKEN = load_token()
request_base = contextvars.ContextVar("request_base",default="")
credential = contextvars.ContextVar("credential", default="")
upstream = Upstream(allow_real=os.environ.get("ALLOW_REAL_ORDERS", "true").lower() == "true" and os.environ.get("COUPON_SPLIT_SIMULATE", "false").lower() != "true")
service = Service(upstream, str(runtime / "plans.sqlite3"))
active_credentials = {}  # memory only; local default is recovered on restart
store_context = {}
menu_context = {}
mcp = FastMCP("最优下单", instructions="用户看不到工具结果。点餐页链接必须先原样写进用户可见的 assistant 正文，再调用 get_cart，禁止只有工具调用。任何点餐原话（包括只说单个餐品）都必须先调用 mcd_start(utterance=用户原话)，不能跳过起步直接查地址、菜单或报价。用户说了餐品就沿用 session 的 items，不再问推荐；仅补缺的方式和地点，不能擅选外送地址。每条后续点餐原话也先调用 mcd_start(utterance=完整原话)，服务端保留当前会话。用户说“好了”也调用 mcd_start 恢复购物车。严格依 next_tool、display_markdown、reply_options 继续。原样输出 display_markdown，不得改写或删行。先把 display_markdown 中含 [打开点餐页] 链接的整段话原样输出给用户，再调用 get_cart(wait=25) 轮询提交，自动报价；不输出英文元话或内部说明。报价后结束回合等明确确认，禁止自主下单；收到用户明确确认且宿主审批通过后才能下单。", stateless_http=True, json_response=True)


def token():
    value = credential.get() or (UPSTREAM_TOKEN if os.environ.get("COUPON_SPLIT_STDIO") else "")
    if not value:
        raise ValueError("缺少鉴权")
    active_credentials[owner(value)] = value
    return value


def result(data, text):
    if data.get('picker_url'):
        data=dict(data,next_action='先发送可见 assistant 正文，再调用 get_cart(wait=25)',
                  before_next_tool='display_markdown 必须已经在 assistant 正文输出；工具结果不算输出。')
    data = dict(data, markdown=text, display_markdown=text,
                stage=data.get('stage',{'quoted':'quote','awaiting_payment':'placed'}.get(data.get('state'),'pick')),
                say_hint=('先把 display_markdown 中含 [打开点餐页] 的整段话原样输出给用户，再调用 get_cart(wait=25)。链接只发一次；超时继续等待，不催开页。' if data.get('picker_url') else '原样输出 display_markdown，不得改写或删行；包括账户、餐具、时间、用券、规格和覆盖行。'),
                reply_options=data.get('reply_options', []), links=data.get('links', {}))
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=data)


def start_form_schema(schema):
    titles={'plan':'这次怎么点','way':'怎么拿','where':'在哪儿附近','people':'几个人（选填）','when':'什么时候'}
    for name,field in schema.get('properties',{}).items():
        field['title']=titles.get(name,name)
        if name=='people':
            field.pop('anyOf',None);field.pop('default',None)
            field.update(type='integer',minimum=1,maximum=12)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def find_store(city: str, keyword: str, fulfillment: Literal['pickup','drive','delivery'] = "pickup", address_id: str = "", session_id: str = "", plan: Literal['帮我配','我自己点','照上次那份'] | None = None, utterance: str = "") -> CallToolResult:
    """mcd_start 后，按用户给出的城市、地标和方式查询门店；不能从地址簿擅自推断用户想外送。"""
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
        cart['elements'].setdefault('sources',{})['store_code']='utterance'
        if address_id: cart['elements']['addressId']=address_id
        if plan: cart['plan']=plan
        if stores:
            await resolve_cart_intent(cart, token())
            data.update(session_id=session_id, selected_items=cart['items'], elements=cart['elements'])
            if cart.get('intent_options'):
                data.update(next_tool='get_menu',reply_options=cart['intent_options'])
                text+='\n'+cart['intent_question']
            elif cart['items']:
                data.update(next_tool='quote_best', stage='pick')
                text += '\n已放入你说的餐品，接着核价。'
            elif cart.get('plan')=='我自己点':
                page=(await local_url())+'/order/'+session_id
                data.update(picker_url=page, order_url=page, links={'picker_url':page}, next_tool='get_cart', wait=25, stage='pick')
                text += f'\n[打开点餐页]({page}) — 选好点底部“选好了，去算最省”，选好会自动接着算；如果没接上，回来说一句“好了”。'
            elif cart.get('plan')=='帮我配':
                data.update(next_tool='get_menu',recommend=True,people=cart.get('people') or 1)
        carts[session_id]=cart
    return result(data,text)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def get_menu(store_code: str, search: str = "", offset: int = 0, elements: OrderElements | None = None, people: int | None = None, recommend: bool = False, session_id: str = "") -> CallToolResult:
    """浏览全菜单；套餐返回官方选配和特制字段，按原样提交，不推测杯型。"""
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
    if search: rows = matches(search, rows)
    recommended=[]
    if recommend:
        rows=[row for row, _ in recommend_rows(rows, people)]
    page=rows[max(0,offset):max(0,offset)+8]
    for row in page:
        if recommend:
            detail=(await upstream.call(t,'query-meal-detail',dict(base,code=row['productCode'])))['data']
            quantity=next(n for r,n in recommend_rows(rows,people) if r['productCode']==row['productCode'])
            item=default_item(detail, quantity)
            recommended.append({'name':row['name'],'item':item})
    selected=[]
    if cart:
        cart['store_code']=store_code
        await resolve_cart_intent(cart,t,menu)
        selected=cart['items']
    elif search and offset==0:
        try:resolved=resolve_intent(search,rows)
        except AmbiguousIntent as exc:
            return result({'items':exc.options,'reply_options':[{'n':n,'does':r['name'],'productCode':r['productCode']} for n,r in enumerate(exc.options,1)],'stage':'pick','next_tool':'get_item_detail'},str(exc))
        for item in resolved:
            detail=(await upstream.call(t,'query-meal-detail',dict(base,code=item['productCode'])))['data']
            selected.append(default_item(detail,item['quantity']))
    compact=[{k:r[k] for k in ('productCode','name','price_yuan','category')} for r in page]
    text=('按'+str(people or 1)+'个人配好了（来自这家店现在的菜单，都能改）：' if recommend else '这家店现在的菜单（也可以直接说餐品名称和数量）：')+'\n'+'\n'.join(f"{n} {r['name']}"+(f" ×{people or 1}" if recommend else '')+f"　¥{float(r['price_yuan']):.2f}" for n,r in enumerate(page,1))
    if recommend: text+='\n回复 **1 就这样算**，或者说要改什么。'
    if recommend:
        active_sid=session_id or memory.read(owner(t)).get('active_session','')
        active=carts.get(active_sid)
        if active and active['owner']==owner(t):
            active.update(items=[copy.deepcopy(row['item']) for row in recommended],recommendation_pending=True)
    data={'items':compact,'recommended_items':recommended,'total':len(rows),'next_offset':offset+8 if offset+8<len(rows) else None,'selected_items':selected,'stage':'pick','next_tool':'quote_best' if selected or recommended else 'get_item_detail'}
    # Variable-length menus/default rounds must never overflow the model's budget.
    while len(json.dumps(data,ensure_ascii=False).encode())+len(text.encode())*2>3700 and len(compact)>1:
        compact.pop();data['next_offset']=offset+len(compact)
        text='这家店现在的菜单（也可以直接说名称和数量）：\n'+'\n'.join(f"{n} {r['name']}　¥{float(r['price_yuan']):.2f}" for n,r in enumerate(compact,1))
    return result(data,text)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True))
async def get_item_detail(store_code: str, product_code: str, elements: OrderElements | None = None) -> CallToolResult:
    """按需读取一个商品的官方规格/特制；未指定规格只用官方默认项。"""
    e=normalize_elements(elements); context=order_context(get_store_context(token(),store_code),e,require_take=False) if e else {}
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
    state=memory.read(owner(t))
    active=carts.get(state.get('active_session',''))
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
        raise ValueError('上次的方式与本门店不符，请按新门店重新选择取餐或配送方式。')
    # Model-provided sources are never evidence of what the user said.
    trusted=previous.get('sources',{})
    for key,value in elements.items():
        if key=='sources':continue
        elements['sources'][key]=(trusted.get(key,'default') if active and previous.get(key)==value else
            ('default' if trusted.get(key)=='default' else 'last') if previous.get(key)==value else 'default')
    official=(await account_info()).structuredContent
    if incoming.get("account_last4") and incoming["account_last4"]!=official["account_last4"]:
        raise ValueError("账户后四位与本机授权账户不一致，请刷新账户信息")
    elements['account_last4']=official['account_last4']
    elements['account_name']=official['account_name']
    elements['sources']['account_name']='account'
    elements['sources']['account_last4']='account'
    if not elements['sources'].get('store_code'):
        elements['sources']['store_code']='utterance' if (owner(t),store_code) in store_context else 'last' if memory.read(owner(t)).get('store_code')==store_code else 'utterance'
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
    return result(quote_summary(plan),markdown(plan))


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


@mcp.tool(meta={"anthropic/requiresUserInteraction":True}, annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True))
async def place_orders(plan_id: str, confirm_token: str, user_confirmation: str, option_id: str = "0", take_way: str = "", high_amount_confirmation: str = "") -> CallToolResult:
    """仅逐字收到用户明确确认且宿主审批通过后调用。默认真实下单；超过200元须额外确认具体金额。确认凭证10分钟有效。"""
    t=token(); plan=service.load(plan_id,t)
    import re
    if not re.search(r'确认|同意下单|就这样下单',user_confirmation) or re.search(r'不|别|不要|取消|撤回|先等',user_confirmation):
        raise ValueError('请先在对话中明确确认下单')
    selected=next(p for p in plan['plans'] if p['option_id']==option_id)
    if selected['price']>20000:
        expected=f"确认下单 ¥{selected['price']/100:.2f}"
        if high_amount_confirmation!=expected:
            raise ValueError('金额超过 ¥200，请用户追加回复：'+expected)
    if take_way:
        import hmac
        if not hmac.compare_digest(confirm_token,plan['confirm_token']):raise ValueError('确认凭证不匹配')
        selected=next(p for p in plan['plans'] if p['option_id']==option_id)
        if any(take_way not in {v.get('code',v.get('takeWayCode')) for v in o['takeWayList']} for o in selected['orders']):raise ValueError('请选择本次官方堂食/外带选项')
        plan['order_elements']['takeWayCode']=take_way
        plan['order_elements']['sources']['takeWayCode']='utterance'
        plan.update(elements_pending=[],elements_validated=True);service.save(plan)
    selected=next(p for p in plan['plans'] if p['option_id']==option_id)
    service.validate_before_place(plan,selected)
    for order in selected['orders']:
        await validate_selected(t,plan['storeCode'],order['request']['items'],order['request'],fill_defaults=False)
    plan = await service.place(t,plan_id,confirm_token,option_id)
    return result(public(plan),markdown(plan))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True))
async def pay_card(plan_id: str) -> CallToolResult:
    """查询已创建订单状态、官方支付链接和截止时间；临近截止自动撤回待付单。"""
    plan = await service.status(token(),plan_id)
    for o in plan["orders"]:
        o["remaining_seconds"] = max(0,int(deadline(o["expirePayTime"])-time.time()))
    return result(public(plan),markdown(plan))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=True))
async def cancel_plan(plan_id: str) -> CallToolResult:
    """放弃方案或撤回待付订单；只取消当前账户自己的方案。"""
    plan = await service.cancel(token(),plan_id)
    return result(public(plan),markdown(plan))


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
            item['roundList']=[{'round':str(r['id']),'comboItemList':[{'code':c['code'],'quantity':c.get('quantity',1)} for c in r['choices'] if c.get('isDefault') and c.get('quantity',1)>0]} for r in detail['rounds']]
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
        cart.update(items=selected,intent_resolved=True)


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
async def mcd_start(utterance: Annotated[str, Field(description='逐字复制本轮最后一条用户消息。严禁拼接上一轮原话、历史摘要、已知餐品或要素；历史由服务端自动保留。')], recommend: bool | None = None, plan: Literal['帮我配','我自己点','照上次那份'] | None = None, ctx: Context | None = None) -> CallToolResult:
    """任意点餐原话的唯一入口。餐品按当前菜单解析；没定门店先保留原话，不重复问推荐。"""
    import re
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    url=await local_url()
    if not (credential.get() or UPSTREAM_TOKEN):
        memory.write('onboarding',{'utterance':utterance})
        return result({'stage':'setup','setup_url':url+'/setup','needs_authorization':True,'links':{'setup_url':url+'/setup'}},f'先连上你自己的麦当劳账户，大约 2 分钟：[打开授权页]({url}/setup)。弄好后回来说一声就行。')
    try:
        await upstream.call(token(),'query-my-account',{})
    except ValueError as exc:
        if '授权失效' not in str(exc):raise
        return result({'stage':'setup','setup_url':url+'/setup','needs_authorization':True,'links':{'setup_url':url+'/setup'}},f'麦当劳账户授权已失效，请重新连接：[打开授权页]({url}/setup)。保存后回来说“好了”，继续点餐。')
    onboarding=memory.read('onboarding')
    if onboarding.get('utterance') and utterance.strip().strip('。！!')=='好了':
        memory.write('onboarding',{})
        resumed=await mcd_start(onboarding['utterance'],recommend,plan,ctx)
        data=resumed.structuredContent
        return result(data,'已连接。可以说“帮我配”“我自己点”或“外送到公司”。\n\n'+data['display_markdown'])
    t=token();state=memory.read(owner(t))
    if utterance.strip().strip('。！!') in {'好了','选好了'}:
        sid=state.get('active_session','')
        if sid and carts.get(sid):return await get_cart(sid,wait=0)
    if utterance.replace(' ','').strip('。！!') in {'1就这样算','就这样算','按这个算'}:
        sid=state.get('active_session','');active=carts.get(sid)
        if active and active.get('recommendation_pending'):
            active.update(submitted=True,recommendation_pending=False)
            return await get_cart(sid,wait=0)
    previous={k:v for k,v in state.items() if k in {'elements','store_code','items'}}
    active=carts.get(state.get('active_session',''))
    continuing=bool(active and not intent_text(utterance) and '麦当劳' not in utterance)
    if continuing:
        previous={k:copy.deepcopy(active[k]) for k in ('elements','store_code','items') if k in active}
        plan=plan or active.get('plan')
    inferred={};incoming={}
    location=re.search(r'(?:我在|位于|现在在|(?:^|[，,])在)([^，。,；]+)',utterance)
    if location:
        inferred['location']=location.group(1).removesuffix('附近')
        previous.pop('store_code',None)
        previous['elements']={k:v for k,v in previous.get('elements',{}).items() if k not in {'addressId','delivery_address','reservationDate','sources'}}
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
        old_sources={k:('default' if old.get('sources',{}).get(k)=='default' else 'last') for k in old if k!='sources'}
        incoming={**old,**incoming,'sources':{**old_sources,**{k:'utterance' for k in current_facts if k!='sources'}}}
    e=merge_elements(previous.get('elements',{}),incoming)
    if previous.get('store_code'):e['sources']['store_code']='last'
    if incoming.get('fulfillment') and incoming['fulfillment']!=previous.get('elements',{}).get('fulfillment'):
        previous.pop('store_code',None)
    concrete=bool(intent_text(utterance)) and not intent_text(utterance).isdigit()
    if recommend is not None and plan is None:plan='帮我配' if recommend else '我自己点'
    if '帮我配' in utterance or '帮我推荐' in utterance:plan='帮我配'
    elif '自己点' in utterance or '自己选' in utterance:plan='我自己点'
    elif concrete and plan is None:plan='我自己点'
    sid=state['active_session'] if continuing else secrets.token_urlsafe(24)
    cart={'owner':owner(t),'items':[],'elements':e,'store_code':previous.get('store_code'),'submitted':False,'location':inferred.get('location',''),'city':state.get('last_search',{}).get('city',''),'intent':utterance if concrete and plan!='帮我配' else '', 'plan':plan,'people':inferred.get('people')}
    if continuing:
        cart['items']=copy.deepcopy(active.get('items',[]))
        cart['intent']=active.get('intent','')
        cart['intent_resolved']=active.get('intent_resolved',False)
    carts[sid]=cart
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
    if store:reused.append(store.get('storeName','上次门店')+'（沿用上次）')
    if e.get('sources',{}).get('addressId')=='last':reused.append('上次配送地址（沿用上次）')
    prefix='、'.join(reused)+('，现在送。' if e.get('fulfillment')=='delivery' else '，现在取。') if reused else ''
    if incoming.get('reservationDate'):
        from coupon_split.presentation import human_time
        prefix=prefix.replace('现在送。','').replace('现在取。','')+human_time(incoming['reservationDate'],e.get('fulfillment')=='delivery')+'。'
    if inferred.get('people'):prefix+=str(inferred['people'])+' 个人，'
    summary=state.get('item_summary','') if previous.get('items') else ''
    if previous.get('items') and not summary and cart.get('store_code'):
        base=dict(storeCode=cart['store_code'],beType={'pickup':1,'drive':5,'delivery':2}.get(e.get('fulfillment'),1),orderType=2 if e.get('fulfillment')=='delivery' else 1)
        if store.get('beCode'):base['beCode']=store['beCode']
        menu=(await upstream.call(t,'query-meals',base))['data']['meals']
        summary='、'.join(menu.get(i['productCode'],{}).get('name','上次餐品')+' ×'+str(i['quantity']) for i in previous['items'])
    reply_options=[]
    if 'plan' in missing:
        if previous.get('items'):
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
    if 'plan' in missing:fields['plan']=(Literal['照上次那份','帮我配','我自己点'] if previous.get('items') else Literal['帮我配','我自己点'],...)
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
        if cart['plan']=='照上次那份':cart['items']=copy.deepcopy(previous.get('items',[]))
    data={'stage':'ask','question':question,'ui_mode':ui_mode,'form_response':form_response,'inferred':inferred,'inferred_sources':[{'key':k,'value':v,'source':e['sources'].get(k,'utterance')} for k,v in e.items() if k!='sources'],'missing':missing,'ask':{'message':question,'fields':list(fields)},'elements':e,'items':cart['items'],'store_code':cart['store_code'],'session_id':sid,'reuse_label':'沿用上次' if reused else '', 'last_order_summary':summary,'default_time':'立即（默认）','reply_options':reply_options,'next_tool':'find_store'}
    if inferred.get('people'):data['people']=inferred['people']
    if cart['items'] and cart['store_code']:
        data.update(stage='pick',next_tool='quote_best');text='收到你点的餐品，正在核价。'
    elif cart.get('plan')=='帮我配' and cart['store_code']:
        data.update(stage='pick',next_tool='get_menu',recommend=True)
        if form_response and form_response.get('people'):data['people']=form_response['people']
        text='按这家店现在的菜单帮你配一份，每一项都能改。'
    elif cart.get('plan')=='我自己点' and cart['store_code']:
        page=url+'/order/'+sid
        data.update(stage='pick',order_url=page,picker_url=page,links={'picker_url':page},next_tool='get_cart',wait=25)
        text=f'[打开点餐页]({page}) — 选好点底部“选好了，去算最省”，选好会自动接着算；如果没接上，回来说一句“好了”。'
    if cart.get('intent_options'):
        data.update(next_tool='get_menu',reply_options=cart['intent_options']);text=cart['intent_question']
    # Claude can discover MCP tools without loading an installed companion skill.
    # Route the first entry through Skill explicitly; no hook is required.
    if ctx is not None and not cart.get('skill_requested'):
        client=getattr(getattr(ctx.session,'client_params',None),'clientInfo',None)
        if 'claude' in str(getattr(client,'name','')).lower():
            cart['skill_requested']=True
            data.update(next_tool='Skill',skill='mcd-best-order:mcd-order',
                after_skill_tool=data.get('next_tool'),
                next_action='先调用 Skill(skill="mcd-best-order:mcd-order") 读取已安装的点餐技能，然后按本结果继续。')
    carts[sid]=cart
    return result(data,text)



async def get_cart(session_id: str, wait: int = 0, link_displayed: bool = False) -> CallToolResult:
    """仅在你已向用户发送含 [打开点餐页](URL) 的可见 assistant 正文之后调用。

    用户看不到工具结果。必须先发送链接正文，再在同一回合调用本工具等待；
    先轮询再发链接会让用户无法打开页面。未提交时继续等待，提交后报价。
    link_displayed 只有在 assistant 正文已经包含点餐链接后才传 true；
    未发正文时先按返回的 handoff_correction 发正文，再以 true 调用。
    """
    cart=owned_cart(session_id)
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
    while not cart["submitted"] and time.monotonic()<until:
        await asyncio.sleep(.25)
        cart=owned_cart(session_id)
    if cart['submitted']:cart['received']=True
    elif cart.get('store_code') and not cart.get('page_opened'):
        # A successful empty poll can hide an omitted link indefinitely. Repeat
        # the fail-closed correction after a real timeout until the page opens.
        cart['handoff_checked']=False
        carts[session_id]=cart
        return await get_cart(session_id,wait=0,link_displayed=False)
    return result({**{k:v for k,v in cart.items() if k not in {'owner','waiter_until'}},'next_tool':'quote_best' if cart['submitted'] else 'get_cart','wait':25},'收到：已按你选的规格取回餐品，正在算最省的下法……' if cart['submitted'] else '等你在页面里选好，选好会自动交回。')


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
        return JSONResponse({'message':message+'；申请入口 https://open.mcd.cn/mcp'},status_code=400)


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
    state=memory.read(cart['owner'])
    return JSONResponse({**{k:v for k,v in cart.items() if k!='owner'},'last_search':dict(state.get('last_search',{}),city=cart.get('city') or state.get('last_search',{}).get('city','')),'stores':list(state.get('stores',{}).values())},headers={'Cache-Control':'no-store'})


async def order_page(request):
    sid=request.path_params['session_id']; cart=carts.get(sid)
    if not cart:return Response(status_code=404)
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
        if not supplied or (supplied == LOCAL_KEY and not UPSTREAM_TOKEN):
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
            if t and plan["state"] in {"placing","awaiting_payment","rollback_pending","awaiting_coupon_return"}:
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


app = Auth(Starlette(routes=[Route('/bridge-health',bridge_health),Route("/",index),Route("/setup",setup_page,methods=["GET","POST"]),Route("/order/{session_id}/state",cart_state,methods=['GET','POST']),Route("/order/{session_id}/menu",cart_menu),Route("/order/{session_id}",order_page,methods=["GET","POST"]),Route('/quote-status',quote_status,methods=['POST']),Route("/favicon.ico",favicon),Mount("/web",app=StaticFiles(directory=ROOT/"web")),Route('/design/{name}',style_file),Route('/design/v2/{name}',style_file),Mount("/",app=http_mcp)],lifespan=lifespan))

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
