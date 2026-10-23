import copy
from collections import Counter
import re


def food_name(value, names):
    """Identifiers are never a display fallback, including embedded coupon labels."""
    return re.sub(r'(?<!\d)\d{4,}(?!\d)',lambda m: str(names.get(m[0],'所选餐品')) if not str(names.get(m[0],'')).isdigit() else '所选餐品',str(value))


def resolve_display_names(plan):
    names=dict(plan.get('names',{}))
    for spec in plan.get('selected_specifications',[]):
        for code,name in {spec['productCode']:spec['name'],**spec.get('names',{})}.items():
            if name and not str(name).isdigit():names[str(code)]=name
    for spec in plan.get('selected_specifications',[]):
        spec['name']=food_name(spec['name'],names)
        spec['choices']=[food_name(v,names) for v in spec['choices']]
        spec['names']={c:food_name(v,names) for c,v in spec.get('names',{}).items()}
    for candidate in plan.get('plans',[]):
        candidate['label']=food_name(candidate.get('label',''),names)
        for order in candidate['orders']:
            order['coupons']=[food_name(v,names) for v in order.get('coupons',[])]
            for row in order['productList']:
                row['productName']=food_name(row['productName'],names)
                for content in row.get('contents',[]):content['name']=food_name(content['name'],names)
    plan['names']={c:food_name(n,names) for c,n in names.items()}



def money(value):
    return f"¥{value/100:.2f}"


def distance(value):
    try:
        meters = float(value)
        return f"{meters:.0f} 米" if meters < 1000 else f"{meters/1000:.1f} 公里"
    except (ValueError, TypeError):
        return ""


def compact_option(p):
    return {k:p[k] for k in ["option_id","price","savings","buys_card","coverage","extras"]} | {"orders":[{k:o[k] for k in ["price","productList","coupons","coupon_count","evidence_id","priced_at","deliveryPrice","packingPrice"] if k in o} for o in p["orders"]]}


def option_difference(candidate, current):
    """Compare complete structured items, never character slices of display labels."""
    if candidate['option_id'] == current['option_id']:
        return ''

    def food(p):
        counts = Counter()
        for order in p['orders']:
            for row in order['productList']:
                if row['productCode'] == '521234':
                    continue
                # Contents already contain totals for all units of this package.
                for item in row.get('contents') or [dict(name=row['productName'], quantity=row['quantity'])]:
                    counts[item['name']] += item['quantity']
        return counts

    def describe(counts):
        return '、'.join(f'{name} ×{q}' for name, q in sorted(counts.items()))

    before, after = food(current), food(candidate)
    removed, added = before-after, after-before
    changes = []
    if removed and added:
        changes.append(f'{describe(removed)} → 换成 {describe(added)}')
    elif removed:
        changes.append(f'减少 {describe(removed)}')
    elif added:
        changes.append(f'增加 {describe(added)}')
    if candidate['buys_card'] != current['buys_card']:
        changes.append('不买卡 → 换成 买卡' if candidate['buys_card'] else '买卡 → 换成 不买卡')
    if len(candidate['orders']) != len(current['orders']):
        changes.append(f"{len(current['orders'])} 单 → 换成 {len(candidate['orders'])} 单")
    coupons = lambda p: Counter(c for o in p['orders'] for c in o.get('coupons', []))
    old_coupons, new_coupons = coupons(current), coupons(candidate)
    if old_coupons != new_coupons:
        changes.append(f"用券：{'；'.join(old_coupons.elements()) or '不用券'} → 换成 {'；'.join(new_coupons.elements()) or '不用券'}")
    if not changes and candidate['label'] != current['label']:
        changes.append(f"下法：{current['label']} → 换成 {candidate['label']}")
    return '；'.join(changes)


def quote_summary(plan, option_id="0"):
    p=next(p for p in plan['plans'] if p['option_id']==option_id)
    names=coverage_names(plan);n=len(p['orders'])
    result={k:plan[k] for k in ('plan_id','confirm_token','baseline','baseline_evidence_id','storeCode','real_orders_enabled','state','compared','price_calls','search_limit','elapsed_seconds')}
    orders=[]
    for i,o in enumerate(p['orders'],1):
        orders.append(dict(n=i,evidence_id=o['evidence_id'],priced_at=o.get('priced_at',''),lines=[dict(name=r['productName'],qty=r['quantity'],detail=line_detail(plan,r),amount_yuan=f"{r['subtotal']/100:.2f}") for r in o['productList']],
            delivery_fee_yuan=None if o.get('deliveryPrice') is None else f"{o['deliveryPrice']/100:.2f}",packing_fee_yuan=None if o.get('packingPrice') is None else f"{o['packingPrice']/100:.2f}",
            packing_note='已免' if o.get('packing_waived') else '本单无打包费' if o.get('packingPrice')==0 else '',
            coupon='；'.join(o.get('coupons',[])) or '不用',subtotal_yuan=f"{o['price']/100:.2f}"))
    result.update(stage='quote',price=p['price'],savings=p['savings'],order_count=n,payment_count=n,orders_count=n,payments_count=n,payments_note=f'需依次支付 {n} 笔' if n>1 else '付 1 次',
        headline=(f"帮你省 {money(p['savings'])}：照原样 {money(plan['baseline'])} → 实付 {money(p['price'])}" if p['savings'] else f"这单已是最低价 {money(p['price'])}"),
        save_yuan=f"{p['savings']/100:.2f}",baseline_yuan=f"{plan['baseline']/100:.2f}",pay_yuan=f"{p['price']/100:.2f}",
        card_fee=sum(r['subtotal'] for o in p['orders'] for r in o['productList'] if r['productCode']=='521234'),
        elements=element_rows(plan),elements_pending=plan.get('elements_pending',[]),orders=orders,
        coverage=[dict(name=names.get(c,c),quantity=q,basis='same_code') for c,q in p['coverage'].items()],
        extras=[dict(name=names.get(c,c),quantity=q) for c,q in p['extras'].items()],
        no_card={k:plan['no_card'][k] for k in ('option_id','price','savings')} if p.get('buys_card') and plan.get('no_card') else None,
        excluded_count=len(plan.get('exclusions',[])),next_tool='place_orders',
        reply_options=([{'n':i,'say':str(i),'does':v.get('name') or v.get('takeWayName') or v.get('title') or '门店取餐','take_way':v.get('code',v.get('takeWayCode'))} for i,v in enumerate(plan['take_options'],1)] if plan.get('take_options') else [{'n':1,'say':'1','does':'堂食并确认'},{'n':2,'say':'2','does':'外带并确认'}]) if plan.get('elements_pending') else [])
    return public(result)


def official_payment_url(value):
    from urllib.parse import urlsplit
    if not value:
        return False
    u=urlsplit(value)
    return u.scheme == "https" and u.netloc == "m.mcd.cn" and u.path == "/mcp/scanToPay"


def public(plan):
    result = copy.deepcopy(plan)
    result.pop("owner", None)
    for field in ["evidence","names","before_order_ids","pending_requests"]:
        result.pop(field,None)
    if result.get("state") not in {None,"quoted"}:
        for field in ["plans","no_card","confirm_token"]:
            result.pop(field,None)
    # Coupon identifiers remain only in the private local evidence database.
    def scrub(value):
        if isinstance(value, dict):
            return {k:scrub(v) for k,v in value.items() if k not in {"couponId", "couponCode", "payId", "request"}}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    result = scrub(result)
    # Official payment URL is preserved; mobile jump is an optional additional link.
    from urllib.parse import urlsplit,parse_qs,urlencode
    import qrcode
    import qrcode.image.svg
    import io
    for order in result.get("orders",[]):
        value = order.get("payH5Url")
        if value:
            url = urlsplit(value)
            if not official_payment_url(value):
                order.pop("payH5Url",None)
                order["payment_error"] = "非预期官方支付地址，禁止跳转"
                continue
            buffer=io.BytesIO()
            qrcode.make(value,image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
            order["qr_svg"]=buffer.getvalue().decode()
            oid=parse_qs(url.query).get("orderId",[])
            if len(oid)==1:
                order["mobilePayUrl"]="https://m.mcd.cn/mcp/jumpToApp/?"+urlencode({"orderId":oid[0]})
    return result


def human_time(value, delivery=False, now=None):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    if not value:return '立即送达' if delivery else '立即取餐'
    date=datetime.fromisoformat(value)
    today=(now or datetime.now(ZoneInfo('Asia/Shanghai'))).date()
    label='今天' if date.date()==today else '明天' if date.date()==today+timedelta(days=1) else f'{date.month}月{date.day}日'
    return label+' '+date.strftime('%H:%M')+' · 预约'


def element_rows(plan):
    e=plan.get('order_elements',{});sources=e.get('sources',{})
    def row(key,label,value):return {'key':key,'label':label,'value':value,'mark':{'last':'沿用上次','default':'默认','utterance':'你说的'}.get(sources.get(key),'')}
    delivery=e.get('fulfillment')=='delivery'
    result=[row('fulfillment','方式',{'pickup':'到店自取','drive':'得来速','delivery':'麦乐送'}.get(e.get('fulfillment'),'到店自取'))]
    if delivery:result.append(row('addressId','送到',e.get('delivery_address','所选地址')+'，'+plan.get('storeName','所选门店')+'配送'+(' · '+plan['distance'] if plan.get('distance') else '')))
    else:result.append(row('store_code','门店',plan.get('storeName','所选门店')))
    result.append(row('reservationDate','时间',human_time(e.get('reservationDate'),delivery)))
    if not delivery:result.append(row('takeWayCode','取餐方式' if e.get('fulfillment')=='drive' else '堂食/外带',e.get('takeWayName') or {'eat-in':'堂食','take-in-store':'外带'}.get(e.get('takeWayCode'),'（待选）')))
    result += [row('needTableware','餐具','需要' if e.get('needTableware') else '不要'),row('account_name','账户',e.get('account_name','待核实')+' · 尾号 '+e.get('account_last4','待核实'))]
    return result


def coverage_names(plan):
    names=dict(plan.get('names',{}))
    for spec in plan.get('selected_specifications',[]):
        names.update(spec.get('names',{}))
        if spec.get('productCode'):
            choices=[c.removesuffix('（官方默认）') for c in spec.get('choices',[]) if c not in {'标准','切块生菜'}]
            detail=' + '.join(choices+spec.get('modifications',[]))
            names[spec['productCode']]=spec['name']+('（'+detail+'）' if detail else '')
            selected=set(spec.get('selected_codes',[]))
            if len(selected)==1 and len(spec.get('choices',[]))==1:
                child=next(iter(selected))
                if child!=spec['productCode']:names[child]=names[spec['productCode']]
    return names


def line_detail(plan,row):
    specs=[s for s in plan.get('selected_specifications',[]) if s.get('productCode')==row['productCode']]
    if specs:
        spec=specs[0]
        if row.get('contents'):
            parts=[c['name']+(' ×'+str(c['quantity']//row['quantity']) if c['quantity']!=row['quantity'] else '') for c in row['contents']]
        else:parts=[c.removesuffix('（官方默认）') for c in spec['choices'] if c.removesuffix('（官方默认）') not in {'标准','切块生菜'}]
        if spec['modifications'] and parts:parts[0]+='（'+'、'.join(spec['modifications'])+'）'
        elif spec['modifications']:parts.append('、'.join(spec['modifications']))
        return ' + '.join(parts)
    return ' + '.join(c['name']+((' ×'+str(c['quantity']//row['quantity'])) if c['quantity']!=row['quantity'] else '') for c in row.get('contents',[]))


def markdown(plan):
    if plan["state"] == "quoted":
        p=plan['plans'][0];n=len(p['orders']);e=plan.get('order_elements',{})
        way={'pickup':'到店自取','drive':'得来速','delivery':'麦乐送'}.get(e.get('fulfillment'),'到店自取')
        headline=(f"**帮你省 {money(p['savings'])}**　照原样 {money(plan['baseline'])} → 实付 **{money(p['price'])}**" if p['savings'] else f"**这单已是最低价 {money(p['price'])}**　没找到比照原样更省的下法")
        rows=[headline+f'　·　{n} 单 · '+(f'需依次支付 {n} 笔' if n>1 else '付 1 次'),'','| | |','|---|---|']
        rows += [f"| {r['label']} | {r['value']}"+('（'+r['mark']+'）' if r.get('mark') else '')+' |' for r in element_rows(plan)]
        for i,o in enumerate(p['orders'],1):
            rows += [f"\n单 {i}｜{plan.get('storeName','所选门店')} · {way}",'']
            for row in o['productList']:
                fee='【卡费】' if row['productCode']=='521234' else ''
                rows.append(f"- {fee}{row['productName']} ×{row['quantity']}　{money(row['subtotal'])}")
                detail=line_detail(plan,row)
                if detail:rows.append(('  每套：' if len(row.get('contents',[]))>1 else '  规格：')+detail)
            if e.get('fulfillment')=='delivery':
                rows += [f"- 配送费　{money(o['deliveryPrice'])}",f"- 打包费　{money(o['packingPrice'])}"+('（已免）' if o.get('packing_waived') else '（本单无打包费）' if o['packingPrice']==0 else '')]
            rows += ['- 用券：'+('；'.join(o.get('coupons',[])) or '不用'),f"- 本单实付 **{money(o['price'])}**"]
        names=coverage_names(plan)
        rows += ['\n对得上你点的：'+' · '.join(f'{names.get(c,c)} ×{q}' for c,q in p['coverage'].items())+'；多出来的：'+('、'.join(f'{names.get(c,c)} ×{q}' for c,q in p['extras'].items()) or '无')]
        if p.get('buys_card') and plan.get('no_card'):
            rows += [f"\n不想买卡：不买卡最低 {money(plan['no_card']['price'])}（省 {money(plan['no_card']['savings'])}），说“不买卡”换这个。"]
            if p.get('buys_card'):rows += [f"买卡后本单比不买卡再省 {money(plan['no_card']['price']-p['price'])}。"]
        if plan.get('take_options'):
            rows += ['\n请选择取餐方式：'+'；'.join(str(i)+' '+(v.get('name') or v.get('takeWayName') or v.get('title') or '门店取餐') for i,v in enumerate(plan['take_options'],1))+'。']
        else:rows += ['\n还差一项：回复 **1 堂食并确认下单** 或 **2 外带并确认下单**。' if plan.get('elements_pending') else '\n回复 **确认** 下单，宿主还会弹出审批。']
        if p['price']>20000:rows += [f"本单超过 ¥200，还需追加确认：**确认下单 ¥{p['price']/100:.2f}**。"]
        if not plan.get('real_orders_enabled'):rows += ['当前为模拟模式，不产生真实订单。']
        return '\n'.join(rows)
    if plan.get("dry_run"):
        n=len(plan["orders"])
        cancelled=plan["state"]=="cancelled"
        return ("**模拟 · 已撤回，没有产生任何订单。**" if cancelled else "**模拟下单，未产生真实订单，也不能付款。**")+ ("" if cancelled else f"\n需依次支付 {n} 笔（真实下单时）。" if n>1 else "\n真实下单时付 1 次。")+"\n"+"\n".join(f"单 {i}｜{money(o['price'])}｜{'已撤回' if cancelled else o['orderStatus']}" for i,o in enumerate(plan["orders"],1))
    if plan['state'] == 'cancelled':
        return "**已撤回。**\n本可省 " + money(plan.get('selected',{}).get('savings',0)) + "，已撤回。\n" + "\n".join(f"单 {i}｜{money(o['price'])}｜已撤回" for i,o in enumerate(plan['orders'],1))
    state={"awaiting_payment":"待支付","cancelled":"已撤回","rollback_pending":"撤回待核验","awaiting_coupon_return":"已撤回，退券待核验","paid":"已支付","placing":"处理中"}.get(plan["state"],"核验中")
    rows = [f"订单状态：{state}。需依次支付 {len(plan['orders'])} 笔。"]
    for i,o in enumerate(plan["orders"],1):
        payment=public({'orders':[o]})['orders'][0]
        link = f"[官方支付页]({o['payH5Url']}) · [手机打开麦当劳 App]({payment['mobilePayUrl']})" if plan['state']=='awaiting_payment' and str(o.get('status'))=='10' and official_payment_url(o.get('payH5Url')) and payment.get('mobilePayUrl') else "当前状态请勿付款"
        rows.append(f"单 {i}｜{money(o['price'])}｜{o['orderStatus']}｜{link}｜{o['expirePayTime']} 前付款。")
    return "\n\n".join(rows)+"\n\n请在15分钟内付款。钱直接付到麦当劳官方收银台，我们不经手。想撤回，说‘撤回订单’；取消成功后，券退回仍需核验。"
