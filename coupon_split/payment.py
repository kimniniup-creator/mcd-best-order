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
        state='expired' if o.get('auto_expired') else 'cancelled' if status=='60' else 'expired' if status=='10' and not remaining else 'pending' if status=='10' else 'paid'
        orders.append(dict(amount=o['price'],items=[dict(name=r['productName'],quantity=r['quantity']) for r in o.get('productList',[])],
                           state=state,status_text={'pending':'待付款','paid':'已付款','cancelled':'已取消','expired':'已过期'}[state],
                           remaining_seconds=remaining,qr_svg=o.get('qr_svg','') if state=='pending' else '',
                           mobile_url=o.get('mobilePayUrl','') if state=='pending' and device!='desktop' else '',
                           pickup_code=next((o[k] for k in ('takeCode','pickupCode','orderCode','mealCode') if o.get(k)),'')))
    return dict(device=device,store=plan.get('storeName','所选门店'),orders=orders,payment_count=len(orders),dry_run=plan.get('dry_run',False))
