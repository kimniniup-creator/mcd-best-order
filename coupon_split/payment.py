"""Minimal local cashier view: no account fields, credentials or coupon identifiers."""
import re
import time
from coupon_split.presentation import public
from coupon_split.service import deadline


def payment_state(plan, ua='', now=None):
    now=time.time() if now is None else now
    device='mobile' if re.search(r'Android|iPhone|iPad|Mobile',ua,re.I) else 'desktop' if re.search(r'Windows NT|Macintosh|X11',ua,re.I) else 'unknown'
    orders=[]
    for o in public(plan).get('orders',[]):
        remaining=max(0,int(deadline(o['expirePayTime'])-now))
        status=str(o.get('status','10'))
        state='expired' if status=='60' and o.get('auto_expired') else 'cancelled' if status=='60' else 'expired' if status=='10' and not remaining else 'pending' if status=='10' else 'paid'
        orders.append(dict(amount=o['price'],items=[dict(name=r['productName'],quantity=r['quantity']) for r in o.get('productList',[])],
                           state=state,status_text=('已过期，未付款，已撤回' if status=='60' else '已过期，未付款，等待撤回') if state=='expired' else {'pending':'待付款','paid':'已付款','cancelled':'已取消'}[state],
                           query_error=o.get('query_error',''),
                           remaining_seconds=remaining,qr_svg=o.get('qr_svg','') if state=='pending' else '',
                           mobile_url=o.get('mobilePayUrl','') if state=='pending' and device!='desktop' else '',
                           pickup_code=next((o[k] for k in ('takeCode','pickupCode','orderCode','mealCode') if o.get(k)),'')))
    paid=sum(o['state']=='paid' for o in orders);cancelled=sum(o['state']=='cancelled' or (o['state']=='expired' and str(source.get('status'))=='60') for o,source in zip(orders,plan.get('orders',[])))
    pending=len(orders)-paid-cancelled
    summary=f'部分完成：{paid} 笔已付，{cancelled} 笔已撤回' if paid and cancelled else f'已付款：{paid} 笔' if paid and not pending else f'已撤回：{cancelled} 笔' if cancelled and not pending else f'已付 {paid} 笔，还需支付 {pending} 笔' if paid else f'待付款：{pending} 笔'
    missing=[o for o in orders if o['state'] in {'expired','cancelled'}]
    notice=('还缺：'+'、'.join(r['name']+' ×'+str(r['quantity']) for o in missing for r in o['items'])+'。重新下这一笔需要先重新报价并确认。') if paid and cancelled else ''
    return dict(device=device,store=plan.get('storeName','所选门店'),orders=orders,payment_count=len(orders),dry_run=plan.get('dry_run',False),summary=summary,notice=notice,pending_count=pending)


def payment_message(plan):
    data=payment_state(plan)
    rows=[data['summary'],data['store']]
    for i,o in enumerate(data['orders'],1):
        text=f"第 {i} 笔：{o['status_text']}"
        if o['state']=='paid':text+=' · 取餐码：'+str(o['pickup_code']) if o['pickup_code'] else '，取餐码稍后在麦当劳 App 里查看'
        if o['query_error']:text+=' · '+o['query_error']
        rows.append(text)
    if data['notice']:rows.append(data['notice'])
    return '\n'.join(rows)
