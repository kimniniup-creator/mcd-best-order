"""Coupon views for the user: what this store accepts now, and what is in the wallet.

Read-only. Coupon codes and ids stay internal; the user sees titles, validity and the
products a coupon applies to. Whether a coupon actually lowers the price is only known
from quote_best, which prices every candidate.
"""
import re


def _window(text):
    """'2026-10-19 10:30:00-2026-10-23 23:59:59' -> ('10-19 10:30', '10-23 23:59')."""
    parts = re.findall(r'\d{4}-(\d{2}-\d{2}) (\d{2}:\d{2})', str(text or ''))
    return tuple(f'{d} {t}' for d, t in parts[:2])


def store_view(rows):
    out = []
    for c in rows or []:
        start, end = (_window(c.get('tradeDateTime')) + ('', ''))[:2]
        names = list(dict.fromkeys(p.get('productName') for p in c.get('products') or [] if p.get('productName')))
        out.append({'title': c.get('title') or c.get('couponName') or '优惠券', 'valid_until': end, 'valid_from': start, 'products': names})
    return out


def wallet_view(data):
    rows = (data or {}).get('coupons') or []
    out = []
    for c in rows:
        info = c.get('discountInfo') or {}
        out.append({'title': c.get('title') or '优惠券', 'subtitle': c.get('subtitle') or '', 'usable_now': c.get('enable') == 1,
                    'valid_until': c.get('tradeEndDate') or '', 'when': c.get('datetimeText') or '',
                    'discount': info.get('discountDesc') or '', 'tags': [t.get('label') for t in c.get('tags') or [] if t.get('label')]})
    return out, int((data or {}).get('totalCount') or len(rows)), int((data or {}).get('totalPages') or 1)


def store_text(store_name, coupons):
    if not coupons:
        return f'{store_name}现在没有你能用的券。'
    lines = [f'{store_name}现在能用的券（{len(coupons)} 张）：']
    for c in coupons:
        tail = '，适用：' + '、'.join(c['products'][:3]) + ('等' if len(c['products']) > 3 else '') if c['products'] else ''
        until = f'（到 {c["valid_until"]}）' if c['valid_until'] else ''
        lines.append(f'- {c["title"]}{until}{tail}')
    lines.append('报价时会自动挑最省的用法；每单最多用一张。')
    return '\n'.join(lines)


def wallet_text(coupons, total):
    if not coupons:
        return '卡包里现在没有券。'
    usable = [c for c in coupons if c['usable_now']]
    lines = [f'卡包里共 {total} 张券，现在可用 {len(usable)} 张：']
    for c in (usable or coupons)[:20]:
        until = f'（到 {c["valid_until"]}）' if c['valid_until'] else ''
        extra = f'，{c["discount"]}' if c['discount'] else ''
        lines.append(f'- {c["title"]}{until}{extra}')
    if len(usable or coupons) > 20:
        lines.append(f'……还有 {len(usable or coupons) - 20} 张')
    lines.append('卡包里的券不一定能在这家店、这种取餐方式下用；要看这家店能用哪些，说“这家店能用什么券”。')
    return '\n'.join(lines)
