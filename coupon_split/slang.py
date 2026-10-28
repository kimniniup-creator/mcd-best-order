"""Ordering vocabulary as a small ontology; availability and all prices come from the menu.

Concepts carry a type, labels and relations (broader, byLabel, askAlways, cardVariant,
askWhenSiblings, askLabels). Resolution reasons over those relations instead of
per-entry special cases, then grounds the concept on the live store menu.
"""
import json
import re
from pathlib import Path

from coupon_split.ontology import VOCABULARY as ONTOLOGY
CONCEPTS={c['id']:c for c in ONTOLOGY['concepts']}
# Legacy entry kinds remain the public contract for callers and tests.
KIND={'Product':'direct','Combo':'direct','Family':'family','Ambiguous':'ambiguous','Membership':'membership',
      'Activity':'activity','Service':'service','Brand':'brand','OtherBrand':'other_brand','Unsupported':'unsupported'}
NOT_FOOD={'other_brand','membership','service','brand','unsupported','activity'}


def entry_of(concept):
    entry=dict(id=concept['entry'],kind=KIND[concept['type']],aliases=concept['altLabel'],targets=concept['groundBy'],concept=concept['id'])
    if concept.get('byLabel'):entry['by_alias']=concept['byLabel']
    if concept.get('note'):entry['note']=concept['note']
    return entry


ENTRIES=sorted((entry_of(c) for c in ONTOLOGY['concepts'] if 'entry' in c),key=lambda e:e['id'])


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


def ancestors(concept_id):
    seen=[];queue=list(CONCEPTS[concept_id].get('broader',[]))
    while queue:
        current=queue.pop(0)
        if current not in seen:
            seen.append(current);queue.extend(CONCEPTS[current].get('broader',[]))
    return seen


def siblings(concept_id):
    """Concepts under the nearest broader concept that has other grounded members."""
    for parent in ancestors(concept_id):
        found=[c for c in ONTOLOGY['concepts'] if c['id']!=concept_id and parent in c.get('broader',[]) and c['groundBy']]
        if found:return found
    return []


def grounded(concept,rows,label=None):
    targets=concept.get('byLabel',{}).get(label,concept['groundBy'])
    found=[r for r in rows if any(norm(t) in norm(r['name']) for t in targets)]
    found.sort(key=lambda r:(next((i for i,t in enumerate(targets) if norm(t) in norm(r['name'])),999),len(r['name'])))
    return targets,found


def alternatives(concept,rows):
    """Selling items under the same broader concept replace an unavailable one."""
    selling=[r for r in rows if available(r)]
    related=[r for c in siblings(concept['id']) for r in grounded(c,selling)[1]]
    unique=list({r['productCode']:r for r in related}.values())
    if concept['type']=='Product':
        # A single item is replaced by single items before combos containing them.
        unique.sort(key=lambda r:bool(re.search('套餐|件套',r['name'])))
    return (unique or selling)[:4]


def card_prices(rows):
    """Membership-priced items show the card price beside the price without the card."""
    lines=[f"{r['name']}：含卡价 ¥{float(r['price_yuan']):.2f} / 不带卡价 "+(f"¥{float(r['was_yuan']):.2f}" if r.get('was_yuan') else '待核')
           for r in rows if r.get('requires_card') and r.get('price_yuan')]
    return ('；'.join(lines)+'（卡费另计，报价时比较买卡和不买卡）') if lines else ''


def asks(concept,alias,selling,rows):
    if len(selling)<=1:return False
    if concept['type']=='Ambiguous' or alias in concept.get('askLabels',[]):return True
    return any(norm(s) in norm(r['name']) for s in concept.get('askWhenSiblings',[]) for r in rows)


def resolve(text,rows):
    entry=classify(text)
    if not entry:return None
    concept=CONCEPTS[entry['concept']];alias=entry['matched_alias'];kind=entry['kind']
    official=[r for r in rows if len(norm(r['name']))>len(norm(alias)) and norm(r['name']) in norm(text) and available(r)]
    # the phrase is, word for word, the official name of exactly one selling item: no question needed
    same=[r for r in rows if available(r) and norm(r['name']) in (norm(text),norm(alias))]
    if not official and len(same)==1 and kind not in NOT_FOOD:
        return dict(entry=entry,rows=same,message='',selected=True)
    if official:
        return dict(entry=entry,rows=[max(official,key=lambda r:len(r['name']))],message='',selected=True)
    if kind=='direct' and re.search('套餐|三件套',text) and not any('套餐' in a or '三件套' in a for a in entry['aliases']):
        packages=[r for r in rows if available(r) and re.search('套餐|件套',r['name']) and any(norm(t) in norm(r['name']) for t in entry['targets'])]
        if len(packages)==1:return dict(entry=entry,rows=packages,message='',selected=True)
        if packages:return dict(entry=entry,rows=packages[:8],message='你说的对应几款套餐，请选：'+'、'.join(r['name'] for r in packages[:8]),selected=False)
    targets,found=grounded(concept,rows,alias)
    selling=[r for r in found if available(r)]
    if concept.get('askAlways'):
        options=selling[:8] or [r for r in rows if available(r)][:4]
        return dict(entry=entry,rows=options,message=f'你说的 {alias} 是指哪种？我这边能找到的是：'+('、'.join(r['name'] for r in options) or '暂无对应餐品')+'。请说明组合内容，不能沿用过期券包。',selected=False)
    if kind in NOT_FOOD:
        message=entry.get('note') or ('这是品牌称呼，不是餐品。你想吃麦当劳的什么？' if kind=='brand' else '这是活动称呼，不能沿用历史优惠。请先核对当前官方活动和门店券；你想吃什么餐品？')
        return dict(entry=entry,rows=selling[:8],message=message,selected=False)
    if not selling:
        options=alternatives(concept,rows)
        message=f'你说的{alias}对应餐品在这家店当前菜单中不在售或今日售罄。可以换成：'+('、'.join(r['name'] for r in options) or '其他门店或时段的餐品')+'。你想换哪一种？'
        return dict(entry=entry,rows=options,message=message,selected=False)
    card=card_prices(selling[:8]) if concept.get('cardVariant') else ''
    if asks(concept,alias,selling,rows):
        return dict(entry=entry,rows=selling[:8],message=f'你说的 {alias} 是指哪种？我这边能找到的是：'+'、'.join(r['name'] for r in selling[:8])+'。'+entry.get('note','')+card,selected=False)
    exact=[r for r in selling if norm(r['name']) in [norm(t) for t in targets]]
    if concept['type']=='Family' and not exact and len(selling)>1:
        # a product family with several different selling members is never settled by menu order
        return dict(entry=entry,rows=selling[:8],message=f'你说的 {alias} 是指哪种？我这边能找到的是：'+'、'.join(r['name'] for r in selling[:8])+'。'+entry.get('note','')+card,selected=False)
    chosen=(exact or selling)[0]
    return dict(entry=entry,rows=[chosen],message='',selected=True,card_prices=card_prices([chosen]) if concept.get('cardVariant') else '')
