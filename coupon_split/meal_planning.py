"""Host recommendation protocol. All upstream operations here are read-only."""
import copy
import json
import re
from coupon_split.menu import menu_rows, default_item
from coupon_split.recommendation import role, available, spicy, conflicts, budget, fallback
from coupon_split.local import order_context


RULES = ('由宿主模型配餐，不要调用外部模型。每人一份主食、一杯饮料，每两人一份小食；多人考虑套餐/分享餐，'
         '依据官方组成折算，份量未知不得猜。避开所有忌口；辣度/过敏资料未知要披露，不能承诺安全。'
         '遵守预算，按类别轮换。给一个主推和一个备选，各一句理由；只能用菜单商品码和官方选配。'
         '分页不足继续 get_menu(recommend=true,offset=next_offset,session_id=本会话)。选配不全用 get_item_detail。'
         '然后调用 propose_cart(session_id,options=[{title,reason,items:[{productCode,quantity,roundList?,modification?}]}])。'
         '未获用户认可不得 quote_best，更不得 place_orders。')


def base_for(s, cart):
    return dict(order_context(s.get_store_context(s.token(), cart['store_code']), cart['elements'], require_take=False), storeCode=cart['store_code'])


async def context(s, store_code, session_id, people, offset):
    if not session_id:
        session_id = s.memory.read(s.owner(s.token())).get('active_session', '')
    cart = s.owned_cart(session_id)
    if cart.get('store_code') != store_code: raise ValueError('请先在本会话确定门店')
    cart['elements'].update({k:v for k,v in (await s.account_info()).structuredContent.items() if k in ('account_name','account_last4')})
    if people is not None:
        if type(people) is not int or not 1 <= people <= 12: raise ValueError('人数范围 1–12')
        cart['people'] = people
    s.carts[session_id] = cart
    base = base_for(s, cart)
    menu = (await s.upstream.call(s.token(), 'query-meals', base))['data']
    rows = menu_rows(menu)
    # Interleave roles so even the first compact page can compose a whole meal.
    groups = [[r for r in rows if role(r)==key] for key in ('主食','小食','饮料','分享餐','套餐','甜品','未知')]
    rows = [group[i] for i in range(max(map(len,groups),default=0)) for group in groups if i<len(group)]
    if offset < 0 or offset >= max(1, len(rows)): raise ValueError('菜单分页位置无效')
    raw = '\n'.join(cart.get('meal_requests', [cart.get('last_utterance', '')]))
    data = dict(stage='compose', session_id=session_id, store_code=store_code,
                people=cart.get('people') or 1, people_source='用户' if cart.get('people') else '暂按1人，可修改',
                user_requests=raw, budget_cents=budget(raw), rules=RULES,
                previous_items=cart.get('previous_items',cart.get('items',[])),
                items=[], total=len(rows), next_offset=None, next_tool='propose_cart',
                next_action='宿主根据菜单和 user_requests 自己配出两个不同方案，调用 propose_cart；此时不能报价。')
    text = '我会按你的条件配一个主推和一个备选，核实价格后给你选。'
    for row in rows[offset:offset+8]:
        detail = (await s.upstream.call(s.token(), 'query-meal-detail', dict(base, code=row['productCode'])))['data']
        item = dict(productCode=row['productCode'], name=row['name'], price_yuan=row['price_yuan'], category=role(row),
                    official_category=row['category'], spicy=spicy({**row, **detail}),
                    portion=detail.get('portion') or detail.get('servingSize') or row.get('portion') or '未知',
                    available=available(row) and available(detail),
                    composition=detail.get('description') or row.get('description') or '未知',
                    rounds=[dict(id=str(r['id']),name=r.get('name',''),min=r.get('minQuantity',1),max=r.get('maxQuantity',1),
                                 choices=[{k:c[k] for k in ('code','name','quantity','isDefault','diffPrice') if k in c} for c in r['choices']]) for r in detail.get('rounds',[])],
                    modification='有特制项时调用 get_item_detail' if detail.get('modification') else {},
                    allergens=detail.get('allergens', row.get('allergens', '未知')))
        # Large official choice groups remain accessible through the detail tool.
        if len(json.dumps(item, ensure_ascii=False).encode()) > 2600:
            item.update(rounds='请调用 get_item_detail 获取完整官方选配', modification='请调用 get_item_detail')
            item['composition'] = str(item['composition'])[:180]
        data['items'].append(item)
        data['next_offset'] = offset+len(data['items']) if offset+len(data['items']) < len(rows) else None
        if len(s.result(data, text).model_dump_json().encode()) > 8000:
            data['items'].pop()
            data['next_offset'] = offset+len(data['items'])
            break
    if not data['items']:
        raise ValueError('配餐上下文超过8KB或当前菜单为空，请缩短条件后重试')
    return s.result(data, text)


async def propose(s, session_id, options):
    cart = s.owned_cart(session_id)
    if not cart.get('store_code'): raise ValueError('请先确定门店')
    cart['elements'].update({k:v for k,v in (await s.account_info()).structuredContent.items() if k in ('account_name','account_last4')})
    if not isinstance(options, list) or len(options) != 2: raise ValueError('请提供一个主推方案加一个备选方案')
    base = base_for(s, cart)
    menu = (await s.upstream.call(s.token(), 'query-meals', base))['data']
    rows = {r['productCode']: r for r in menu_rows(menu)}
    raw = '\n'.join(cart.get('meal_requests', [cart.get('last_utterance', '')]))
    errors, accepted = [], []
    for n, option in enumerate(options, 1):
        try:
            if not isinstance(option, dict) or not all(isinstance(option.get(k),str) and 0<len(option[k])<=180 for k in ('title','reason')): raise ValueError('方案标题和一句理由必填（各≤180字）')
            items = copy.deepcopy(option.get('items'))
            if not isinstance(items,list) or not 1 <= len(items) <= 30: raise ValueError('方案餐品需1–30项')
            from coupon_split.optimizer import parse_cart
            parse_cart(items)
            unknown_spice=False
            for item in items:
                if not isinstance(item,dict) or type(item.get('quantity')) is not int or not isinstance(item.get('productCode'),str): raise ValueError('商品码必须为字符串，数量必须为整数')
                if set(item)-{'productCode','quantity','roundList','modification'}: raise ValueError('未知餐品字段；选配只接受 roundList/modification')
                code = item['productCode']
                if code not in rows: raise ValueError('商品码不存在于当前菜单：'+str(code))
                row = rows[code]
                detail = (await s.upstream.call(s.token(),'query-meal-detail',dict(base,code=code)))['data']
                unknown_spice |= spicy({**row,**detail})=='未知'
                if not available(row) or not available(detail): raise ValueError(row['name']+'当前时段无售')
                problems = conflicts({**row,**detail}, raw)
                problems += modifier_conflicts(detail,item,raw)
                if problems: raise ValueError(row['name']+'：'+'；'.join(problems))
                for rnd in item.get('roundList',[]):
                    children=rnd.get('comboItemList',[])
                    if len({str(c['code']) for c in children})!=len(children) or any(type(c.get('quantity')) is not int for c in children): raise ValueError('选配重复或数量不是整数')
            summaries = await s.validate_selected(s.token(),cart['store_code'],items,base,fill_defaults=True)
            portions={'主食':0,'小食':0,'饮料':0}
            main_names=[]
            # Evaluate the actual selected/default children as well as parent names.
            for item in items:
                detail = (await s.upstream.call(s.token(),'query-meal-detail',dict(base,code=item['productCode'])))['data']
                choices = {str(c['code']):c for r in detail.get('rounds',[]) for c in r['choices']}
                if not item.get('roundList'):
                    r={**rows[item['productCode']],**detail}
                    category=role(r)
                    if category in portions:portions[category]+=item['quantity']
                    if category=='主食':main_names.append(r.get('name',''))
                for rnd in item.get('roundList',[]):
                    for child in rnd['comboItemList']:
                        official = choices[str(child['code'])]
                        category=role(official)
                        if category in portions:portions[category]+=item['quantity']*child['quantity']
                        if category=='主食':main_names.append(official.get('name',''))
                        unknown_spice |= spicy(official)=='未知'
                        problems = conflicts(official,raw)
                        problems += modifier_conflicts(official,child,raw)
                        if not available(official): problems.append('选配当前时段无售')
                        if problems: raise ValueError(official.get('name','选配')+'：'+'；'.join(problems))
            people=cart.get('people') or 1
            if portions['主食']!=people or portions['饮料']!=people or portions['小食']<(people+1)//2:
                raise ValueError(f"{people}人需{people}份主食、{people}杯饮料、至少{(people+1)//2}份小食（套餐按官方子项折算）；当前主食{portions['主食']}、饮料{portions['饮料']}、小食{portions['小食']}，请重配，不能把鸡腿等小食当汉堡主食")
            if re.search(r'换成鸡肉|改成鸡肉|全[要部是]*鸡肉',raw) and any('鸡' not in name for name in main_names):
                raise ValueError('用户要求换成鸡肉的，请将两个方案的主食均改成菜单内鸡肉主食')
            description=option['title']+' '+option['reason']
            if unknown_spice and re.search(r'不辣|无辣|放心|安全食用|都能吃|温和|清淡',description):
                raise ValueError('官方辣度/过敏原资料有未知项，不得声称不辣、清淡或放心吃；可说明避开明确标辣餐品，并披露辣度未知')
            if re.search(r'分量更足|份量更足|保证吃饱|足够吃饱',description):
                raise ValueError('官方份量未证实，不得推测份量或保证吃饱')
            response = (await s.upstream.call(s.token(),'calculate-price',dict(base,items=items)))['data']
            if not s.validate_quantities(dict(base,items=items),response): raise ValueError('直连核价返回的餐品数量不符')
            price = response.get('price')
            if type(price) is not int or price <= 0: raise ValueError('直连核价未返回有效金额')
            limit = budget(raw)
            if limit is not None and price>limit: raise ValueError(f'照原样价 ¥{price/100:.2f} 超预算 ¥{limit/100:.2f}，请重配')
            accepted.append(dict(title=option['title'],reason=option['reason'],items=items,price=price,
                                 item_summary='、'.join(x['name']+' ×'+str(x['quantity'])+('（'+'、'.join(x['choices'])+'）' if x['choices'] else '') for x in summaries)))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({'option':n,'reason':str(exc)})
    if errors:
        return s.result(dict(stage='compose',valid=False,errors=errors,session_id=session_id,next_tool='propose_cart',
                             next_action='按具体错误重新配两个方案；原购物车未修改。'),'方案需要调整：\n'+'\n'.join(f"方案{x['option']}：{x['reason']}" for x in errors))
    if accepted[0]['items'] == accepted[1]['items']:
        return s.result(dict(valid=False,errors=[{'reason':'主推与备选餐品相同，请给不同方案'}],session_id=session_id,next_tool='propose_cart'),'主推和备选需要是不同的方案，请重新配。')
    cart.update(items=copy.deepcopy(accepted[0]['items']),meal_options=accepted,recommendation_pending=True,submitted=False,intent_resolved=True)
    cart.pop('pending_plan_id',None)
    s.carts[session_id]=cart
    text='\n\n'.join(f"{'主推' if n==0 else '备选'}：{x['title']}\n{x['item_summary']}\n理由：{x['reason']}\n照原样价 ¥{x['price']/100:.2f}" for n,x in enumerate(accepted))
    text+='\n\n辣度、份量及过敏原以官方资料为准；未知项不能保证。回复“就这样”或“换备选”，也可以说“不要辣的”“再加一份薯条”。认可后再算最省价。'
    return s.result(dict(stage='proposal',valid=True,session_id=session_id,options=accepted,next_tool=None,
                         after_user_confirmation_tool='mcd_start',next_action='原样展示两个方案并停止等待认可，不能立刻 quote_best。'),text)


async def simple(s, store_code, people, elements):
    e=s.normalize_elements(elements)
    e.update({k:v for k,v in (await s.account_info()).structuredContent.items() if k in ('account_name','account_last4')})
    base=dict(order_context(s.get_store_context(s.token(),store_code),e,require_take=False),storeCode=store_code)
    rows=menu_rows((await s.upstream.call(s.token(),'query-meals',base))['data'])
    details={}
    if (people or 1)>=3:
        for row in rows:
            if role(row)!='分享餐' or not available(row):continue
            detail=(await s.upstream.call(s.token(),'query-meal-detail',dict(base,code=row['productCode'])))['data']
            details[row['productCode']]=detail
            from coupon_split.menu import default_choices
            by_code={str(c['code']):c for r in detail.get('rounds',[]) for c in r['choices']}
            selected=[dict(by_code[p['code']],quantity=p['quantity']) for r in detail.get('rounds',[]) for p in default_choices(r) if p['code'] in by_code]
            coverage={k:sum(c['quantity'] for c in selected if role(c)==k) for k in ('主食','小食','饮料')}
            if selected and all(role(c) in coverage for c in selected):row['servingsByRole']=coverage
    selected=[]
    for row, quantity in fallback(rows,people):
        detail=details.get(row['productCode']) or (await s.upstream.call(s.token(),'query-meal-detail',dict(base,code=row['productCode'])))['data']
        if available(detail): selected.append({'name':row['name'],'item':default_item(detail,quantity)})
    label="简易推荐，想要更合口味的回对话说'帮我配'"
    return s.result(dict(recommended_items=selected,next_tool=None,label=label),label)


def modifier_conflicts(detail, item, raw):
    """Only active default/custom modifiers count; unselected alternatives do not."""
    selected={(str(v.get('code')),str(v.get('key'))):v.get('quantity') for v in item.get('modification',{}).get('values',[])}
    problems=[]
    def walk(value):
        if isinstance(value,dict):
            if 'name' in value and 'selectedKey' in value:
                code=str(value.get('code'))
                active=bool(value.get('selectedQuantity',0))
                if any(c==code for c,k in selected):
                    active=bool(selected.get((code,str(value['selectedKey'])),0))
                if active: problems.extend(conflicts(value,raw))
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    walk(detail.get('modification',{}))
    return problems
