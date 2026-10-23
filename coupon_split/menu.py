"""Menu-derived discovery, intent matching and defaults. No catalogue constants."""
import re
import difflib
from decimal import Decimal


def menu_rows(menu):
    categories = {}
    for category in menu.get('categories', []):
        for item in category.get('meals', []):
            categories[str(item.get('code', item.get('productCode')))] = category['name']
    return [dict(productCode=str(code), name=m.get('name', m.get('productName', code)),
                 price_yuan=str(m.get('currentPrice', '')), was_yuan=str(m.get('originalPrice', '')),
                 image=m.get('image', ''), category=categories.get(str(code), '其他餐品'),
                 **{k:m[k] for k in ('aliases', 'alias', 'sales', 'salesVolume', 'description') if k in m})
            for code, m in menu['meals'].items()]


def normalized_name(text):
    return re.sub(r'[\W_]|鸡腿|经典|风味|™', '', text).replace('汉堡', '堡').replace('套餐', '件套')


def matches(text, rows):
    """Rank menu names and official aliases; partial and typo forms remain menu-bound."""
    query = normalized_name(text)
    found = []
    for row in rows:
        aliases = row.get('aliases', row.get('alias', []))
        if isinstance(aliases, str): aliases = [aliases]
        terms = [normalized_name(row['name']), *map(normalized_name, aliases)]
        scores = []
        for term in filter(None, terms):
            if term in query: scores.append(100 + len(term))
            elif query and query in term: scores.append(70 - (len(term) - len(query)))
            elif len(query)>=2 and re.fullmatch('.*'.join(map(re.escape, query)),term):
                scores.append(65-(len(term)-len(query)))
            elif len(query) >= 2:
                scores.append(50 * difflib.SequenceMatcher(None, query, term).ratio())
        score = max(scores, default=0)
        if score >= 40: found.append((score, row))
    return [row for _, row in sorted(found, key=lambda pair: (-pair[0], len(pair[1]['name']), pair[1]['productCode']))]


def intent_text(utterance):
    from coupon_split.timeparse import strip_time
    utterance = strip_time(utterance)
    utterance = re.sub(r'只要|刚才说的餐品|刚才那份|按官方默认规格|官方默认规格|默认规格|保留原来的餐品', '', utterance)
    text = re.sub(r'(?<!现)(?:我在|位于|现在在|在)(?:[^，。,；]+?)(?:附近|[，。,；]|$)', '', utterance)
    text = re.sub(r'(?:帮我配|帮我推荐|帮我点个|帮我点|帮我|给我|我自己点|自己点|自己选|照上次那份|照上次|照旧)', '', text)
    text = re.sub(r'(?:不要餐具|需要餐具|要餐具|不带餐具|不用餐具|餐具|到店自取|开车去|开车|去拿|得来速|到店|自取|堂食|外带|外送|外卖|麦乐送|立即送|立即取|现在取|现在送)', '', text)
    text = re.sub(r'(?:明天|明早|今天|今晚|晚上|中午|早上)?\s*\d{1,2}\s*(?:点|:)\s*\d{0,2}', '', text)
    text = re.sub(r'(?:\d+|[一二两三四五六七八九十]+)\s*(?:个\s*人|个|份|杯|人|块|套)', '', text)
    text = re.sub(r'我要|我想|想要|帮我|给我|点个|来份|来个|点一|点|想吃|要吃|吃|麦当劳|外送|外卖|送到|送份|开车|去拿|到店|自取|得来速|附近|饿了|推荐|随便|我们|自己选|自己点|帮我配|照上次那份|照上次|照旧|立即|现在|中午|明早|今晚|晚上|今天|明天|的|吧|了|，|。|、|！|\s', '', text)
    return re.sub(r'[\W_]', '', text).removeprefix('我')


class AmbiguousIntent(ValueError):
    def __init__(self, rows):
        self.options = rows[:8]
        super().__init__('你说的对应几款餐品，请选编号：\n'+'\n'.join(f"{n} {r['name']}　¥{r['price_yuan']}" for n,r in enumerate(self.options,1)))


def resolve_intent(utterance, rows):
    if not intent_text(utterance): return []
    # Split requests without splitting product names such as "双层吉士".
    parts = re.split(r'[、，,。]|再来|加一份|加个|和', utterance)
    result = []
    for part in filter(None, parts):
        ranked = matches(intent_text(part), rows) if intent_text(part) else []
        if ranked:
            query=normalized_name(intent_text(part))
            exact=[r for r in ranked if normalized_name(r['name'])==query or query in [normalized_name(a) for a in ([r.get('aliases',r.get('alias',[]))] if isinstance(r.get('aliases',r.get('alias',[])),str) else r.get('aliases',r.get('alias',[])))]]
            contained=[r for r in ranked if normalized_name(r['name']) in query]
            partial=[r for r in ranked if query in normalized_name(r['name'])]
            candidates=exact or contained or partial or ranked[:3]
            if not re.search(r'套餐|件套|组合|分享餐',part):
                singles=[r for r in candidates if not re.search(r'套餐|件套|组合|分享餐',r['name'])]
                if singles:candidates=singles
            if len(candidates)>1:raise AmbiguousIntent(candidates)
            row = candidates[0]
            quantity = re.search(r'(\d+|一|两|二|三|四|五|六|七|八|九|十)(?:个|份|杯|套)', part)
            q = quantity.group(1) if quantity else '1'
            q = int(q) if q.isdigit() else '一二三四五六七八九十'.index(q.replace('两', '二')) + 1
            result.append({'productCode': row['productCode'], 'quantity': min(q, 20)})
    return result


def default_item(detail, quantity=1):
    item = {'productCode': str(detail['code']), 'quantity': quantity}
    if detail.get('rounds'):
        item['roundList'] = [dict(round=str(r['id']), comboItemList=[dict(code=str(c['code']), quantity=c.get('quantity', 1))
            for c in r['choices'] if c.get('isDefault') and c.get('quantity', 1) > 0]) for r in detail['rounds']]
    return item


def recommend_rows(rows, people=None):
    count = people or 1
    if not 1 <= count <= 12: raise ValueError('人数范围 1–12')
    # Prefer current sales within each official category, then current value.
    groups = {}
    for row in rows:
        groups.setdefault(row['category'], []).append(row)
    def rank(row):
        try: price = Decimal(row.get('price_yuan') or '9999')
        except Exception: price = Decimal('9999')
        return (-int(row.get('salesVolume', row.get('sales', 0)) or 0), price, row['productCode'])
    selected=[]
    # Category roles come from this store's catalogue, never fixed product names.
    for role in (r'汉堡|主食|饭|卷|堡',r'小食|配餐|薯|鸡块',r'饮料|饮品|咖啡|茶'):
        candidates=[r for category,group in groups.items() if re.search(role,category) for r in group if not re.search(r'套餐|件套|分享|组合',r['name']) and r not in selected]
        if candidates:
            if '小食' in role:
                savory=[r for r in candidates if re.search(r'薯|鸡|玉米|咸|肉',r['name']) and not re.search(r'甜|冰|蛋糕|圆筒',r['name'])]
                selected.append(min(savory or candidates,key=rank))
            elif '饮料' in role and not any(int(r.get('salesVolume',r.get('sales',0)) or 0) for r in candidates):
                # With no sales signal, choose a normal mid-priced drink instead
                # of always reducing the meal to the cheapest bottled option.
                ordered=sorted(candidates,key=rank);selected.append(ordered[len(ordered)//2])
            else:selected.append(min(candidates,key=rank))
    if not selected and rows:selected=[min(rows,key=rank)]
    return [(row,count) for row in selected]
