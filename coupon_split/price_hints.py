"""On-demand, bounded suggestions. Never change the chosen cart or store."""
import copy
import re

from coupon_split.optimizer import validate_quantities, delivery_fees

STORE_CAP = 3  # includes the selected store
STORE_MIN_CENTS = 300
STORE_MIN_RATIO = .08
SIZE_CALL_CAP = 2
SIZE_DETAIL_CAP = 2


def usable(data):
    return not data.get('violations') and data.get('businessStatus') not in ('CLOSED', 'closed', False)


async def compare_stores(upstream, token, plan, stores, elements):
    if 'store_comparison_calls' in plan:return
    plan['store_comparison_calls']=0
    # Preserve the cart; compare against the actual selected total, including split savings.
    selected=plan.get('plans',[{}])[0]
    if selected.get('orders'):
        request=selected['orders'][0]['request'] if len(selected['orders'])==1 else next(e['request'] for e in plan['evidence'] if e['evidence_id']==plan['baseline_evidence_id'])
        current_price=selected['price']
    else:
        baseline=next(e for e in plan['evidence'] if e['evidence_id']==plan['baseline_evidence_id'])
        request=baseline['request']
        current_price=plan['baseline']
    best=None
    seen={str(plan['storeCode'])}
    for store in stores:
        code=str(store.get('storeCode',''))
        if not code or code in seen or not usable(store):continue
        if len(seen)>=STORE_CAP:break
        seen.add(code)
        from coupon_split.grounding import ground_time
        when={'kind':'reserve','at':elements['reservationDate']} if elements.get('reservationDate') else {'kind':'immediate'}
        _,violations=ground_time(when,store)
        if violations:continue
        alternate=copy.deepcopy(request)
        alternate['storeCode']=code
        # Pickup take-way codes belong to the original store, never reuse them.
        alternate.pop('takeWayCode',None)
        if request.get('beType') in (2,5):
            if not store.get('beCode'):continue
            alternate['beCode']=store['beCode']
        plan['store_comparison_calls']+=1
        try:
            response=await upstream.call(token,'calculate-price',alternate)
            data=response['data']
            if not usable(response) or not usable(data) or type(data.get('price')) is not int or data['price']<=0 or not validate_quantities(alternate,data):continue
            if request.get('beType')==2:delivery_fees(data)
            saving=current_price-data['price']
            if saving<=0 or (saving<STORE_MIN_CENTS and saving/current_price<STORE_MIN_RATIO):continue
            if best is None or data['price']<best['price']:
                best=dict(storeCode=code,storeName=store.get('storeName','附近门店'),price=data['price'],saving=saving,request=alternate,response=response)
        except ValueError:continue
    if best:
        best['text']=f"附近 {best['storeName']} 同一份 ¥{best['price']/100:.2f}，比这家便宜 ¥{best['saving']/100:.2f}，要换吗？"
        plan['store_offer']=best


def size_name(name):
    match=re.search(r'(小|中|大)(?:杯|份)?',name)
    if not match:return None
    return re.sub(r'(小|中|大)(?:杯|份)?','',name), '小中大'.index(match[1])


async def bigger_size(upstream, token, plan):
    if 'size_comparison_calls' in plan:return
    plan['size_comparison_calls']=0
    plan['size_detail_calls']=0
    selected=plan['plans'][0]
    # Explore the first order with package rounds. The global size/detail budgets remain
    # shared across all orders; a one-order change must leave the total unchanged.
    package_orders=[o for o in selected['orders'] if any(i.get('roundList') for i in o['request']['items'])]
    if not package_orders:return
    order=package_orders[0]
    original=order['request']
    # Only inspect rounds on already selected meals, with at most two extra prices.
    for i,item in enumerate(original['items']):
        if not item.get('roundList'):continue
        if plan['size_detail_calls']>=SIZE_DETAIL_CAP:return
        plan['size_detail_calls']+=1
        try:
            detail=(await upstream.call(token,'query-meal-detail',{k:v for k,v in original.items() if k not in ('items','withOrder')} | {'code':item['productCode']}))['data']
        except ValueError:continue
        for ri,round_ in enumerate(item['roundList']):
            official=next((r for r in detail.get('rounds',[]) if str(r['id'])==str(round_['round'])),{})
            choices=official.get('choices',[])
            for ci,child in enumerate(round_['comboItemList']):
                current=next((c for c in choices if str(c['code'])==str(child['code'])),{})
                before=size_name(current.get('name',''))
                if not before:continue
                for choice in choices:
                    after=size_name(choice.get('name',''))
                    if not after or before[0]!=after[0] or after[1]<=before[1]:continue
                    if plan['size_comparison_calls']>=SIZE_CALL_CAP:return
                    changed=copy.deepcopy(original)
                    changed['items'][i]['roundList'][ri]['comboItemList'][ci]['code']=str(choice['code'])
                    plan['size_comparison_calls']+=1
                    try:
                        response=await upstream.call(token,'calculate-price',changed)
                        data=response['data']
                        if usable(response) and usable(data) and data.get('price')==order['price'] and validate_quantities(changed,data):
                            cart=[copy.deepcopy(i) for o in selected['orders'] for i in (changed['items'] if o is order else o['request']['items'])]
                            plan['size_offer']=dict(text=choice['name']+'同价，要换吗？',items=cart,request=changed,response=response)
                            return
                    except ValueError:continue
