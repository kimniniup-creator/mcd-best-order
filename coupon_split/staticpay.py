"""Static payment pages: one self-contained HTML file per created order, opened with file://.

Nothing here talks to a server. The page carries the amount, what was bought, the official mobile-payment link, an inline QR code
(made from that link) and the pay-before time, so it still works after every MCP / page-service process has exited.
"""
import html
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from coupon_split.presentation import public
from coupon_split.service import deadline

CSS = ("*{box-sizing:border-box}body{margin:0;background:#F7F2E8;color:#1A1813;font:16px/1.6 system-ui,sans-serif}"
       "main{max-width:480px;margin:auto;padding:28px 16px}.tag{font-size:13px;letter-spacing:.1em;color:#6B6459}"
       "h1{font-size:40px;margin:6px 0 2px;font-variant-numeric:tabular-nums}.sub{color:#615B51;margin:0 0 14px}"
       ".card{background:#FFFCF6;border:1px solid #D9D1C2;border-radius:20px;padding:20px;text-align:center}"
       ".qr{background:#fff;padding:10px;width:260px;max-width:100%;margin:12px auto}.qr svg{display:block;width:100%;height:auto}"
       ".app{display:block;background:#C8430E;color:#fff;text-decoration:none;border-radius:12px;min-height:48px;padding:12px;margin:12px 0 0;font-weight:650}"
       ".items{font-size:14px;color:#615B51;margin:8px 0}.due{font-weight:650}.note{font-size:14px;color:#615B51}"
       ".sim{background:#F6E3C8;border-radius:12px;padding:8px 12px;margin:0 0 12px;font-weight:650}")


def clock(value):
    return datetime.fromtimestamp(deadline(value), ZoneInfo('Asia/Shanghai')).strftime('%H:%M')


def order_facts(plan, index):
    """Facts about one created order: amount, items, official links, QR (None when the upstream gave no official payment URL)."""
    order = plan['orders'][index]
    view = public({'orders': [order]})['orders'][0]
    return dict(amount_cents=order['price'], items=[(r['productName'], r['quantity']) for r in order.get('productList', [])],
                mobile_url=view.get('mobilePayUrl', ''), qr_svg=view.get('qr_svg', ''), due=clock(order['expirePayTime']),
                order_id=str(order.get('orderId', '')))


def render(plan, index):
    f = order_facts(plan, index)
    count = len(plan['orders'])
    e = html.escape
    items = ' · '.join(f'{e(n)} ×{q}' for n, q in f['items'])
    sim = '<p class="sim">模拟订单：没有真实下单，也不能付款</p>' if plan.get('dry_run') else ''
    pay = (f'<a class="app" href="{e(f["mobile_url"])}">打开麦当劳 App 付款</a><div class="qr">{f["qr_svg"]}</div>'
           '<p class="note">电脑上用手机麦当劳 App 或微信扫一扫</p>') if f['mobile_url'] else '<p class="note">没有拿到付款链接，请在麦当劳 App 或微信小程序「我的订单」里付款。</p>'
    title = f'¥{f["amount_cents"] / 100:.2f}'
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="referrer" content="no-referrer"><title>付款 {title}</title><style>{CSS}</style></head><body><main>'
            f'<span class="tag">最优下单 · 付款{f"（第 {index + 1}/{count} 笔）" if count > 1 else ""}</span>'
            f'<h1>{title}</h1><p class="sub">{e(plan.get("storeName") or "")}</p><div class="card">{sim}<p class="items">{items}</p>'
            f'{pay}<p class="due">{f["due"]} 前付款</p><p class="note">不付款，麦当劳到时间会自动取消。不想要了，回对话里说一声。</p></div></main></body></html>')


def write_pages(plan, directory):
    """Write one page per created order. Returns [{n, path, file_url, amount_cents, mobile_url, due}] (re-writing is harmless)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob('pay-*.html'):  # a payment is only open for 15 minutes; pages older than three days are noise
        try:
            if time.time() - old.stat().st_mtime > 3 * 86400:old.unlink()
        except OSError:pass
    out = []
    for i in range(len(plan.get('orders', []))):
        path = directory / f'pay-{plan["plan_id"][:10]}-{i + 1}.html'
        path.write_text(render(plan, i), encoding='utf-8')
        f = order_facts(plan, i)
        out.append(dict(n=i + 1, path=str(path), file_url=path.resolve().as_uri(), amount_cents=f['amount_cents'], mobile_url=f['mobile_url'], due=f['due']))
    return out
