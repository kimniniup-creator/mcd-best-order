"""Versioned aliases identify intent; availability and all prices come from the menu."""
import json
import re
from pathlib import Path

ENTRIES=json.loads((Path(__file__).parent/'data/mcd-slang.json').read_text(encoding='utf-8'))['entries']


def norm(value):
    return re.sub(r'\s+','',value).casefold().replace('汉堡','堡').replace('（','(').replace('）',')')


def classify(text):
    query=norm(text)
    hits=[(len(norm(alias)),entry,alias) for entry in ENTRIES for alias in entry['aliases'] if norm(alias) in query]
    # Longer aliases own overlapping text: 新穷鬼套餐 is not 穷鬼套餐 or 套餐.
    if not hits:return None
    _,entry,alias=max(hits,key=lambda hit:hit[0])
    return dict(entry,matched_alias=alias)


def available(row):
    return not (row.get('available') is False or row.get('isSelling') is False or row.get('onSale') is False or row.get('soldOut') is True)


def resolve(text,rows):
    entry=classify(text)
    if not entry:return None
    alias=entry['matched_alias'];kind=entry['kind']
    official=[r for r in rows if len(norm(r['name']))>len(norm(alias)) and norm(r['name']) in norm(text) and available(r)]
    if official:
        return dict(entry=entry,rows=[max(official,key=lambda r:len(r['name']))],message='',selected=True)
    if kind=='direct' and re.search('套餐|三件套',text) and not any('套餐' in a or '三件套' in a for a in entry['aliases']):
        packages=[r for r in rows if available(r) and re.search('套餐|件套',r['name']) and any(norm(t) in norm(r['name']) for t in entry['targets'])]
        if len(packages)==1:return dict(entry=entry,rows=packages,message='',selected=True)
        if packages:return dict(entry=entry,rows=packages[:8],message='你说的对应几款套餐，请选：'+'、'.join(r['name'] for r in packages[:8]),selected=False)
    targets=entry.get('by_alias',{}).get(alias,entry['targets'])
    found=[r for r in rows if any(norm(t) in norm(r['name']) for t in targets)]
    found.sort(key=lambda r:(next((i for i,t in enumerate(targets) if norm(t) in norm(r['name'])),999),len(r['name'])))
    selling=[r for r in found if available(r)]
    if entry['id'] in {4,5}:
        options=selling[:8] or [r for r in rows if available(r)][:4]
        return dict(entry=entry,rows=options,message=f'你说的 {alias} 是指哪种？我这边能找到的是：'+('、'.join(r['name'] for r in options) or '暂无对应餐品')+'。请说明组合内容，不能沿用过期券包。',selected=False)
    if kind in {'other_brand','membership','service','brand','unsupported','activity'}:
        message=entry.get('note') or ('这是品牌称呼，不是餐品。你想吃麦当劳的什么？' if kind=='brand' else '这是活动称呼，不能沿用历史优惠。请先核对当前官方活动和门店券；你想吃什么餐品？')
        return dict(entry=entry,rows=selling[:8],message=message,selected=False)
    if not selling:
        alternatives=[r for r in rows if available(r)][:4]
        message=f'你说的{alias}对应餐品在这家店当前菜单中不在售或今日售罄。可以换成：'+('、'.join(r['name'] for r in alternatives) or '其他门店或时段的餐品')+'。你想换哪一种？'
        return dict(entry=entry,rows=alternatives,message=message,selected=False)
    if len(selling)>1 and (kind=='ambiguous' or (alias=='板烧' and any('麦满分' in r['name'] for r in rows)) or alias=='麦辣'):
        return dict(entry=entry,rows=selling[:8],message=f'你说的 {alias} 是指哪种？我这边能找到的是：'+'、'.join(r['name'] for r in selling[:8])+'。'+entry.get('note',''),selected=False)
    exact=[r for r in selling if norm(r['name']) in [norm(t) for t in targets]]
    chosen=(exact or selling)[0]
    return dict(entry=entry,rows=[chosen],message='',selected=True)
