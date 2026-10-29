"""Bounded local coverage search, followed by authoritative concurrent price checks."""
import asyncio
import copy
import itertools
import uuid
import time
from collections import Counter
from decimal import Decimal

DEFAULT_CART = []
MAX_CALLS = 30


def canonical(code):
    return str(code)


def atom(code, quantity=1):
    return {"productCode": str(code), "quantity": quantity}


def parse_cart(items):
    result = Counter()
    for item in items:
        if isinstance(item,dict):
            code,qty=str(item["productCode"]),str(item["quantity"])
        else:
            if ":" not in str(item):
                raise ValueError("items 的每一项是 商品码:数量（如 901120:1）或 {productCode, quantity} 对象，不接受餐品名称；会话里餐品已定时可以只传 session_id")
            code, qty = item.rsplit(":", 1)
        if not code.isdigit() or not qty.isdigit() or not 1 <= int(qty) <= 20:
            raise ValueError("商品格式应为 sku:数量，数量 1–20")
        result[canonical(code)] += int(qty)
    if not result or sum(result.values()) > 40:
        raise ValueError("餐品总数需在 1–40 份之间")
    return result


def coverage(items):
    result = Counter()
    for item in items:
        q = item["quantity"]
        if item.get("roundList"):
            for r in item["roundList"]:
                for c in r["comboItemList"]:
                    code = str(c["code"])
                    result[canonical(code)] += q * c["quantity"]
        else:
            result[canonical(item["productCode"])] += q
    return result


def fits(have, need):
    return all(have[k] <= need[k] for k in have)


def key(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def normalized(value):
    """Order-independent signature for cart, orders and nested package choices."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and isinstance(v.get('quantity'), int) for v in value):
            grouped = {}
            for row in value:
                fields = normalized({k:v for k,v in row.items() if k != 'quantity'})
                signature = key(fields)
                if signature not in grouped:
                    grouped[signature] = dict(fields, quantity=0)
                grouped[signature]['quantity'] += row['quantity']
            return sorted(grouped.values(), key=key)
        return sorted((normalized(v) for v in value), key=key)
    return value


def composition_key(value):
    return key(normalized(value))


def specification_coverage(items):
    """Exact leaf codes, quantities and modifications, including single-product options."""
    result = Counter()
    for item in items:
        children = [c for r in item.get('roundList', []) for c in r['comboItemList']]
        for child in children or [dict(code=item['productCode'], quantity=1)]:
            signature = composition_key(dict(code=str(child['code']),
                modification=child.get('modification', item.get('modification', {}))))
            result[signature] += item['quantity'] * child['quantity']
    return result


def variants(detail, need):
    """Only meal rounds whose contents cover the requested food; no guessed equivalence."""
    rows = [[]]
    for r in detail.get("rounds", []):
        choices = [c for c in r["choices"] if canonical(c["code"]) in need]
        count = r.get("minQuantity", r.get("quantity", 1))
        if not choices or not 1 <= count <= 4:
            return []
        options = []
        for seq in itertools.combinations_with_replacement(choices, count):
            counts = Counter(c["code"] for c in seq)
            options.append({"round": str(r["id"]), "comboItemList": [{"code": c, "quantity": n} for c, n in counts.items()]})
        rows = [a + [b] for a in rows for b in options][:32]
    return [dict(atom(detail["code"]), roundList=rs) for rs in rows if fits(coverage([dict(atom(detail["code"]), roundList=rs)]), need)] if rows and detail.get("rounds") else []


def coupon_on(items, coupon):
    """Split one unit off before attaching a coupon: quantity=2 is never submitted."""
    codes = {p["productCode"] for p in coupon["products"]}
    out = copy.deepcopy(items)
    for i, row in enumerate(out):
        if row["productCode"] in codes and not row.get("couponId"):
            one = copy.deepcopy(row)
            one.update(quantity=1, couponId=coupon["couponId"], couponCode=coupon["couponCode"])
            if row["quantity"] == 1:
                out[i] = one
            else:
                row["quantity"] -= 1
                out.append(one)
            return out
    return None


def fee_kind(row):
    name = str(row.get('productName', row.get('name', '')))
    if str(row.get('productCode')) == '920174' or any(w in name for w in ('打包费', '包装费', '餐盒费')):
        return 'packingPrice'
    if any(w in name for w in ('配送费', '外送费', '运费')):
        return 'deliveryPrice'
    if row.get('isFee') is True or row.get('productType') in ('FEE', 'fee'):
        return 'otherFee'
    return None


def delivery_fees(data):
    rows = [r for r in data['productList'] if fee_kind(r)]
    values = {}
    for field in ('deliveryPrice', 'packingPrice'):
        value = data.get(field)
        if value is None:
            matches = [r for r in rows if fee_kind(r) == field]
            value = sum(r['subtotal'] for r in matches) if matches else (0 if field == 'packingPrice' else None)
        if type(value) is not int or value < 0:
            raise ValueError('暂时无法确认'+('配送费' if field=='deliveryPrice' else '打包费')+'（上游没有返回有效值）')
        values[field] = value
    return values, rows


def validate_quantities(request, response):
    expected, actual = Counter(), Counter()
    for i in request["items"]:
        expected[i["productCode"]] += i["quantity"]
    for i in response["productList"]:
        if request.get('beType') == 2 and fee_kind(i):
            continue
        actual[i["productCode"]] += i["quantity"]
    # withOrder adds a membership product; only that extra line may be absent from requested items.
    return all(actual[c] == n for c, n in expected.items()) and all(c in expected or (request.get("withOrder") and c == "521234" and n == 1) for c, n in actual.items())


def select_ranked(plans):
    ranked = sorted(plans, key=lambda p: (p["price"], len(p["orders"])))
    single = min(p["price"] for p in ranked if len(p["orders"]) == 1)
    return [p for p in ranked if len(p["orders"]) == 1 or p["price"] <= single - 500]


async def quote(upstream, token, store, items, allow_combos=True, context=None):
    started = time.monotonic()
    parsed = parse_cart(items)
    plain = copy.deepcopy(items) if items and isinstance(items[0],dict) else [atom(c,n) for c,n in parsed.items()]
    need = coverage(plain)
    # A host-proposed package expresses food demand, not a purchase strategy.
    # Retain the original package as the official baseline; search its exact leaves.
    residual_rows={};mixed_modifications=False
    for row in plain:
        children=[c for r in row.get('roundList',[]) for c in r['comboItemList']]
        for child in children or [dict(code=row['productCode'])]:
            leaf=atom(child['code'])
            mod=child.get('modification',row.get('modification'))
            if mod:leaf['modification']=copy.deepcopy(mod)
            if str(child['code']) in residual_rows and composition_key(residual_rows[str(child['code'])])!=composition_key(leaf):mixed_modifications=True
            residual_rows[str(child['code'])]=leaf
    # Keep mixed modifications of the same leaf separate; don't collapse them.
    if mixed_modifications:allow_combos=False
    def residual(left):
        return [dict(copy.deepcopy(residual_rows[c]),quantity=n) for c,n in left.items()]
    base = {"storeCode": store, "beType": 1, "orderType": 1, **(context or {})}
    menu_r, coupon_r = await asyncio.gather(upstream.call(token, "query-meals", base), upstream.call(token, "query-store-coupons", base))
    menu, coupons = menu_r["data"]["meals"], coupon_r["data"]
    names = {c:m.get("name",m.get("productName",c)) for c,m in menu.items()}
    if base.get('beType')==2 and any(i['productCode'] not in names for i in plain):
        pickup=(await upstream.call(token,'query-meals',{'storeCode':store,'beType':1,'orderType':1}))['data']['meals']
        names.update({c:m.get('name','所选餐品') for c,m in pickup.items() if c not in names})
    detail_limit = asyncio.Semaphore(8)
    async def detail(code):
        try:
            async with detail_limit:
                return (await upstream.call(token, "query-meal-detail", dict(base, code=code)))["data"]
        except (ValueError, StopIteration):
            return None
    # Every current product may expose package rounds, regardless of its name/code.
    codes = list(dict.fromkeys([*menu, *(p['productCode'] for c in coupons for p in c['products'])])) if allow_combos else list(dict.fromkeys(i['productCode'] for i in plain))
    details = [d for d in await asyncio.gather(*(detail(c) for c in dict.fromkeys(codes))) if d]
    for d in details:
        if d.get('name') and not str(d['name']).isdigit():names.setdefault(str(d['code']),d['name'])
        for r in d.get("rounds",[]):
            for c in r["choices"]:
                if c.get('name') and not str(c['name']).isdigit():names[str(c['code'])]=c['name']
    excluded = [{"code": d.get("code"), "reason": "不同商品码且未取得一致的官方规格，排除未核实替换"} for d in details if not variants(d, need)]
    options = [v for d in details for v in variants(d, need)] if allow_combos else []
    for v in options:
        for r in v.get('roundList',[]):
            for c in r['comboItemList']:
                mod=residual_rows.get(str(c['code']),{}).get('modification')
                if mod:c['modification']=copy.deepcopy(mod)
    detail_map={str(d['code']):d for d in details}
    def estimate(comp,card=False):
        total=Decimal(0)
        import re
        for item in comp:
            m=menu.get(item['productCode'],{})
            field='originalPrice' if m.get('withOrder') and not card else 'currentPrice'
            amount=Decimal(str(m.get(field,m.get('currentPrice','999'))))
            d=detail_map.get(item['productCode'],{})
            for selected in item.get('roundList',[]):
                r=next((r for r in d.get('rounds',[]) if str(r['id'])==selected['round']),{})
                for child in selected['comboItemList']:
                    choice=next((c for c in r.get('choices',[]) if str(c['code'])==str(child['code'])),{})
                    match=re.search(r'([+-]?)\s*[¥￥]?\s*(\d+(?:\.\d+)?)',str(choice.get('diffPrice','0')))
                    if match:amount+=Decimal((match[1] or '+')+match[2])*child['quantity']
            total+=amount*item['quantity']
        return total

    compositions = [plain]
    singles=residual(need)
    if allow_combos and composition_key(singles)!=composition_key(plain):compositions.append(singles)
    # Bounded beam: grow combinations of exact-content packages and fill residuals with singles.
    frontier = [([], need)]
    for _ in range(4):
        next_rows = []
        for chosen, left in frontier:
            for opt in options:
                cv = coverage([opt])
                if fits(cv, left):
                    remain = left - cv
                    selected = chosen + [opt]
                    full = selected + residual(remain)
                    if composition_key(full) not in {composition_key(x) for x in compositions}:
                        compositions.append(full)
                    beam_estimate = estimate(full,True)
                    next_rows.append((beam_estimate, selected, remain))
        frontier = [(c, r) for _, c, r in sorted(next_rows, key=lambda x: x[0])[:24]]
        if not frontier:
            break
    candidates = []
    def add(orders, label, estimate=0):
        joined = [i for o in orders for i in o["items"]]
        if coverage(joined) != need or specification_coverage(joined)!=specification_coverage(plain):
            return
        ids = [i["couponId"] for i in joined if i.get("couponId")]
        if len(ids) != len(set(ids)) or any(sum(bool(i.get("couponId")) for i in o["items"]) > 1 for o in orders):
            return
        candidates.append({"orders": orders, "label": label, "estimate": float(estimate)})
    add([dict(base, items=plain)], "原样一单直下", -10000)
    for comp in compositions:
        est = estimate(comp)
        add([dict(base, items=comp)], "套餐重组" if any(i.get("roundList") for i in comp) else "单点", est)
        cards = [menu.get(i["productCode"], {}).get("withOrder") for i in comp]
        with_order = None
        for card in [c for c in cards if c]:
            membership=card.get('membershipCode') if isinstance(card,dict) else None
            spec=card.get('membershipSpecId',card.get('specId')) if isinstance(card,dict) else None
            if not membership or not spec:
                excluded.append({'reason':'未取得完整会员卡参数，排除未核实的买卡方案'})
                continue
            with_order = {"membershipCode": membership, "membershipSpecId": spec}
            add([dict(base, items=comp, withOrder=with_order)], "先买卡再省", estimate(comp,True) + Decimal("9.90"))
            break
        for coupon in coupons:
            changed = coupon_on(comp, coupon)
            if changed:
                add([dict(base, items=changed)], "套餐 / 单点 + 一张券", est - 11)
                if with_order:
                    add([dict(base, items=changed, withOrder=with_order)], "买卡 + 一张券", estimate(comp,True) - Decimal("1.10"))
    # Split each selected coupon-bearing unit into its own order; remaining food stays with last coupon.
    for comp in compositions:
        remaining, orders = copy.deepcopy(comp), []
        for coupon in coupons:
            changed = coupon_on(remaining, coupon)
            if changed:
                picked = next(i for i in changed if i.get("couponId"))
                orders.append(dict(base, items=[picked]))
                remaining = [i for i in changed if not i.get("couponId")]
        if len(orders) > 1:
            orders[-1]["items"] += remaining
            add(orders, "拆单用券", 80)
    # Preserve baseline, card and no-card competitors within a strict request budget.
    unique = {}
    for c in sorted(candidates, key=lambda c: c["estimate"]):
        unique.setdefault(composition_key(c["orders"]), c)
    competitors=list(unique.values())
    priority=[c for c in competitors if c['label']=='原样一单直下']
    # Reserve exploration for every strategy before cheap-looking packages fill the budget.
    for label in ('先买卡再省','买卡 + 一张券','套餐 / 单点 + 一张券','套餐重组','单点','拆单用券'):
        priority += [c for c in competitors if c['label']==label][:3]
    chosen, calls = [], set()
    for c in priority+competitors:
        signatures={composition_key(o) for o in c['orders']}
        if c not in chosen and len(calls|signatures)<=MAX_CALLS:
            chosen.append(c)
            calls |= signatures
    cache, traces, rejected = {}, [], []
    async def price(order):
        k = composition_key(order)
        if k not in cache:
            async def calculate():
                try:response = await upstream.call(token, "calculate-price", order)
                except ValueError as exc:
                    if base.get('beType')==2 and '不可售' in str(exc):
                        labels=list(dict.fromkeys(names.get(i['productCode'],'所选餐品') for i in order['items']))
                        raise ValueError('、'.join(labels)+'暂不支持这次外送；可行替代：换一道餐品，或改为到店自取') from None
                    raise
                data = response["data"]
                if not isinstance(data.get("price"), int) or data["price"] <= 0 or not validate_quantities(order, data):
                    raise ValueError("计价返回数量变化或金额无效，已丢弃")
                fees, fee_rows = delivery_fees(data) if base.get('beType') == 2 else ({}, [])
                evidence = {"evidence_id": uuid.uuid4().hex, "request": order, "response": response}
                traces.append(evidence)
                products = copy.deepcopy([r for r in data['productList'] if r not in fee_rows])
                pending = [dict(i,remaining=i["quantity"]) for i in order["items"]]
                for row in products:
                    # The price endpoint may merge repeated package request rows.
                    # Allocate every returned unit back to its actual selected rounds.
                    left=row["quantity"]
                    row["contents"] = []
                    coupon_applied=False
                    for source in pending:
                        if source["productCode"] != row["productCode"] or not left:
                            continue
                        take=min(left,source["remaining"])
                        if not take:
                            continue
                        left-=take
                        source["remaining"]-=take
                        coupon_applied |= bool(source.get("couponId"))
                        row["contents"] += [{"name":names.get(c["code"],c["code"]),"code":c["code"],"quantity":c["quantity"]*take} for r in source.get("roundList",[]) for c in r["comboItemList"]]
                    merged = {}
                    for content in row["contents"]:
                        code=content["code"]
                        if code not in merged:
                            merged[code]=dict(content,quantity=0)
                        merged[code]["quantity"]+=content["quantity"]
                    row["contents"]=list(merged.values())
                    if coupon_applied:
                        row["coupon_original"]=row.get("originalSubtotal")
                    row["equivalences"] = []
                coupon_names = [c.get("couponName","优惠券") for i in order["items"] if i.get("couponId") for c in coupons if c["couponId"] == i["couponId"]]
                waived=fees.get('packingPrice')==0 and any(r.get('originalSubtotal',0)>0 for r in fee_rows if fee_kind(r)=='packingPrice')
                return dict(request=order, price=data["price"], productList=products, feeList=fee_rows, coupons=coupon_names, coupon_count=len(coupon_names), evidence_id=evidence["evidence_id"], priced_at=response.get("datetime",""), takeWayList=data.get("takeWayList", []), deliveryPrice=fees.get('deliveryPrice'), packingPrice=fees.get('packingPrice'),packing_waived=waived)
            cache[k] = asyncio.create_task(calculate())
        return await cache[k]
    async def evaluate(c):
        results = await asyncio.gather(*(price(o) for o in c["orders"]), return_exceptions=True)
        if any(isinstance(r, Exception) for r in results):
            rejected.append({"label":c["label"],"reason":"；".join(str(r) for r in results if isinstance(r,Exception))})
            return None
        return {"label": c["label"], "strategy":c['label'], "orders": results, "price": sum(r["price"] for r in results), "buys_card": any(o.get("withOrder") for o in c["orders"])}
    evaluated = [p for p in await asyncio.gather(*(evaluate(c) for c in chosen)) if p]
    baseline = next((p for p in evaluated if p["label"] == "原样一单直下"), None)
    if baseline is None:
        reasons=list(dict.fromkeys(r['reason'] for r in rejected))
        raise ValueError("暂时无法核对这份餐的价格："+"；".join(reasons[:2]))
    baseline_packing=sum(o.get('packingPrice') or 0 for o in baseline['orders'])
    for candidate in evaluated:
        for order in candidate['orders']:
            if order.get('packingPrice')==0 and baseline_packing>0:order['packing_waived']=True
    ranked = select_ranked(evaluated)
    # Merge rows with the same visible contents, coupon, card and amount.
    unique_rows = {}
    for p in ranked:
        p["label"] = " + ".join(f"{r['productName']} ×{r['quantity']}"+("（"+"、".join(c["name"]+"×"+str(c["quantity"]) for c in r["contents"])+"）" if r["contents"] else "") for o in p["orders"] for r in o["productList"] if r["productCode"] != "521234") + (" · 买卡" if p["buys_card"] else " · 不买卡") + "；".join(c for o in p["orders"] for c in o["coupons"])
        signature = composition_key([p["price"], [dict(request=o["request"], price=o["price"]) for o in p["orders"]]])
        unique_rows.setdefault(signature,p)
    ranked = list(unique_rows.values())
    for i, p in enumerate(ranked):
        actual = coverage([r for o in p["orders"] for r in o["request"]["items"]])
        p.update(option_id=str(i), savings=baseline["price"]-p["price"], coverage=dict(actual), extras=dict(actual-need))
    strategies={}
    for p in evaluated:
        label=p['strategy']
        if label not in strategies or p['price']<strategies[label]['price']:
            strategies[label]=dict(strategy=label,price=p['price'],difference=p['price']-ranked[0]['price'],evidence_ids=[o['evidence_id'] for o in p['orders']])
    comparison=dict(options=list(strategies.values()),selected_strategy=ranked[0]['strategy'],selected_price=ranked[0]['price'],
        explanation='在已按官方计价核验且内容规格完全一致的候选中，选择最低总价；同价优先少付几笔。',
        scope='有界搜索，不承诺穷尽所有买法；无法核价的候选未作价格结论。')
    comparison['text']='｜'.join(p['strategy']+' ¥'+f"{p['price']/100:.2f}"+(f"（比所选多 ¥{p['difference']/100:.2f}）" if p['difference']>0 else '') for p in comparison['options'])+' → 选了'+comparison['selected_strategy']+'，'+comparison['explanation']
    return {"comparison_summary":comparison,"baseline": baseline["price"], "baseline_evidence_id": baseline["orders"][0]["evidence_id"], "plans": ranked, "no_card": next((p for p in ranked if not p["buys_card"]), None), "compared": len(ranked), "evaluated":len(evaluated), "rejected": len(rejected), "exclusions":excluded+rejected, "price_calls": len(cache), "evidence": traces, "storeCode": store, "cart": dict(need), "names":names, "search_limit": MAX_CALLS, "elapsed_seconds":round(time.monotonic()-started,3)}
