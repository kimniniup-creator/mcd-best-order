"""Price comparison with Pinduoduo (多多进宝 pdd.ddk.goods.search), PRD R21, docs/specs/pdd-compare.md.

Read-only: one search per item name per ten minutes, one link call when Pinduoduo is lower. Nothing here can fail an order:
every error gives None. Credentials come from PDD_* environment variables or ~/.pdd/.env and never leave this module.
"""
import asyncio
import hashlib
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import httpx

GATEWAY = 'https://gw-api.pinduoduo.com/api/router'
KEYS = ('PDD_CLIENT_ID', 'PDD_CLIENT_SECRET', 'PDD_PID', 'PDD_CUSTOM_UID')
TIMEOUT = 3.0
CACHE_SECONDS = 600
CARD_CODE = '521234'
RISK = '第三方代下单，非麦当劳官方。可能不积分，也可能被麦当劳取消。规格和门店以对方页面为准。'
# Titles that are about McDonald's but are not a meal: merchandise and toys
NOT_FOOD = ('周边', '玩具', '公仔', '盲盒', '手办', '钥匙扣', '杯子', '水杯', '模型', '贴纸', '徽章', '衣服', 'T恤')

_cache = {}


def credentials():
    """PDD_* from the environment, falling back to ~/.pdd/.env; None unless client id, secret and pid are all there."""
    if os.environ.get('COUPON_SPLIT_PDD_COMPARE', '').strip().lower() in {'0', 'false', 'no', 'off'}:return None
    found = {k: os.environ.get(k, '') for k in KEYS}
    path = Path(os.environ.get('COUPON_SPLIT_PDD_ENV') or Path.home() / '.pdd' / '.env')
    if not all(found[k] for k in KEYS[:3]) and path.is_file():
        try:
            for line in path.read_text(encoding='utf-8').splitlines():
                k, sep, v = line.strip().partition('=')
                if sep and k.strip() in KEYS and not found[k.strip()]:found[k.strip()] = v.strip().strip('"\'')
        except OSError:return None
    return found if all(found[k] for k in KEYS[:3]) else None


def _value(v):
    if isinstance(v, bool):return 'true' if v else 'false'
    if isinstance(v, (list, dict)):return json.dumps(v, ensure_ascii=False, separators=(',', ':'))
    return str(v)


def make_sign(params, secret):
    """Official PopSDK makeSign: MD5(secret + sorted key+value without sign and empty values + secret), upper case."""
    body = ''.join(f'{k}{params[k]}' for k in sorted(params) if k != 'sign' and params[k] != '')
    return hashlib.md5(f'{secret}{body}{secret}'.encode('utf-8')).hexdigest().upper()


def build_request(api_type, biz, cred, timestamp=None):
    params = dict(type=api_type, client_id=cred['PDD_CLIENT_ID'], timestamp=str(int(timestamp if timestamp is not None else time.time())), data_type='JSON', version='V1')
    params.update({k: _value(v) for k, v in biz.items() if v is not None and v != ''})
    params['sign'] = make_sign(params, cred['PDD_CLIENT_SECRET'])
    return params


def custom_parameters(uid):
    return json.dumps({'uid': uid}, separators=(',', ':')) if uid else None


def search_params(cred, keyword):
    # page_size 20 and no deep paging (help centre 388); block_cat_packages left out, [2] would hide the virtual vouchers
    return dict(keyword=keyword, pid=cred['PDD_PID'], custom_parameters=custom_parameters(cred.get('PDD_CUSTOM_UID')), page=1, page_size=20, with_coupon=False, sort_type=0)


async def call(api_type, biz, cred):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(GATEWAY, data=build_request(api_type, biz, cred))
    r.raise_for_status()
    data = r.json()
    if 'error_response' in data:
        e = data['error_response']
        raise RuntimeError(f"拼多多返回错误 {e.get('error_code')} {e.get('sub_code') or ''}".strip())
    return data


def norm(text):
    text = unicodedata.normalize('NFKC', str(text or '')).lower()
    return re.sub(r'[\s\W_]+', '', text)


def coupon_price_fen(g):
    """Group price minus the 多多进宝 coupon when its threshold is met; shop and platform coupons are not stacked."""
    price = g.get('min_group_price')
    if price is None:return None
    disc = g.get('coupon_discount') or 0
    if g.get('has_coupon') and disc and price >= (g.get('coupon_min_order_amount') or 0):return max(price - disc, 0)
    return price


def same_item(g, name):
    title = g.get('goods_name') or ''
    if '麦当劳' not in title + (g.get('mall_name') or ''):return False
    if any(w in title for w in NOT_FOOD):return False
    return bool(norm(name)) and norm(name) in norm(title)


def single_item(plan):
    """(official name, quantity) when the order holds one kind of meal (cards aside), else None."""
    rows = [r for o in (plan.get('selected') or {}).get('orders', []) for r in o.get('productList', [])
            if not (r.get('is_card') or str(r.get('productCode')) == CARD_CODE)]
    if not rows or len({str(r.get('productCode') or r.get('productName')) for r in rows}) != 1:return None
    return rows[0].get('productName') or '', sum(int(r.get('quantity') or 1) for r in rows)


async def search(name, cred):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < CACHE_SECONDS:return hit[1]
    body = (await call('pdd.ddk.goods.search', search_params(cred, name), cred)).get('goods_search_response') or {}
    goods = [dict(goods_name=g.get('goods_name'), goods_sign=g.get('goods_sign'), mall_name=g.get('mall_name'), price=coupon_price_fen(g))
             for g in body.get('goods_list') or [] if same_item(g, name) and coupon_price_fen(g) is not None and g.get('goods_sign')]
    _cache[name] = (time.time(), dict(goods=goods, search_id=body.get('search_id')))
    return _cache[name][1]


async def link(goods_sign, search_id, cred):
    biz = dict(p_id=cred['PDD_PID'], goods_sign_list=[goods_sign], search_id=search_id, custom_parameters=custom_parameters(cred.get('PDD_CUSTOM_UID')), generate_short_url=True)
    rows = ((await call('pdd.ddk.goods.promotion.url.generate', biz, cred)).get('goods_promotion_url_generate_response') or {}).get('goods_promotion_url_list') or []
    return next((r.get('mobile_short_url') or r.get('short_url') or r.get('url') for r in rows if r.get('mobile_short_url') or r.get('short_url') or r.get('url')), '')


def pdd_cheaper_head(count, pdd_yuan, cheaper_yuan):
    """The comparison is said first (PRD R21: the point is that we compared), then what was found."""
    which = '最便宜的 ' if count > 1 else ''
    return f'比过拼多多 {count} 件同款，{which}¥{pdd_yuan} 起，比这单低 ¥{cheaper_yuan}'


def yuan(cents):
    return f'{cents / 100:.2f}'


async def compare(plan):
    """The comparison result for a placed plan, or None (not comparable, no credentials, nothing found, any error)."""
    try:
        item, cred = single_item(plan), credentials()
        if not item or not cred:return None
        name, qty = item
        mcd = (plan.get('selected') or {}).get('price')
        if not mcd:return None
        found = await search(name, cred)
        if not found['goods']:return None
        best = min(found['goods'], key=lambda g: g['price'])
        pdd = best['price'] * qty
        if pdd >= mcd:
            count = len(found['goods'])
            return dict(kind='mcd_cheaper', platform='拼多多', count=count, line=f'比过拼多多 {count} 件同款，这单更便宜')
        url = await link(best['goods_sign'], found.get('search_id'), cred)
        if not url:return None
        count = len(found['goods'])
        head = pdd_cheaper_head(count, yuan(pdd), yuan(mcd - pdd))
        return dict(kind='pdd_cheaper', platform='拼多多', count=count, pdd_yuan=yuan(pdd), cheaper_yuan=yuan(mcd - pdd), url=url, title=best['goods_name'] or '',
                    head=head, line=f'{head}，第三方代下单，非麦当劳官方：{url}')
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, asyncio.TimeoutError):
        return None


async def compare_within(plan, seconds=TIMEOUT):
    try:
        return await asyncio.wait_for(compare(plan), seconds)
    except asyncio.TimeoutError:
        return None
