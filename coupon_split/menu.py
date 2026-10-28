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
                 requires_card=bool(m.get('withOrder')),
                 image=m.get('image', ''), category=categories.get(str(code), '其他餐品'),
                 **{k:m[k] for k in ('aliases', 'alias', 'sales', 'salesVolume', 'description', 'available', 'isSelling', 'onSale', 'soldOut', 'spicy', 'isSpicy', 'portion', 'servingSize', 'allergens', 'servingsByRole') if k in m})
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
    # an answer that only agrees or picks a time ("那就明天中午12点") names no food
    if re.fullmatch(r'(?:那就|那好|那|就|好的|好吧|好|行吧|行|可以|嗯+|对|是的|就这样)?[，。！!、\s]*(?:吧|了|呢|啊|哦)?[，。！!\s]*', utterance):
        return ''
    utterance = re.sub(r'(?:想点|要点)(?:个)?麦当劳', '', utterance)
    utterance = re.sub(r'(?:我)?(?:想)?吃点(?:东西|什么)|肚子(?:有点)?饿了|有点饿|不预约|无需预约|不用预约', '', utterance)
    utterance = re.sub(r'(?:我)?(?:想)?自己(?:来)?(?:点|选|看看菜单|看下菜单|看菜单)(?:餐)?', '', utterance)
    utterance = re.sub(r'好了|继续刚才的?|接着点刚才那个|接着用|重新点|新的一单|再下一单|只要|刚才说的餐品|刚才那份|按官方默认规格|官方默认规格|默认规格|保留原来的餐品', '', utterance)
    text = re.sub(r'(?<!现)(?:我在|位于|现在在|在)(?:[^，。,；]+?)(?:附近|[，。,；]|$)', '', utterance)
    text = re.sub(r'(?:帮我配|帮我推荐|帮我点个|帮我点|帮我|给我|我自己点|自己点|自己选|照上次那份|照上次|照旧)', '', text)
    text = re.sub(r'(?:不要餐具|需要餐具|要餐具|不带餐具|不用餐具|餐具|到店自取|开车去|开车|去拿|得来速|到店|自取|堂食|外带|外送|外卖|麦乐送|立即送|立即取|现在取|现在送)', '', text)
    text = re.sub(r'(?:明天|明早|今天|今晚|晚上|中午|早上)?\s*\d{1,2}\s*(?:点|:)\s*\d{0,2}', '', text)
    text = re.sub(r'(?:\d+|[一二两三四五六七八九十]+)\s*(?:个\s*人|个|份|杯|人|块|套)', '', text)
    text = re.sub(r'我要|我想|想要|帮我|给我|点个|来份|来个|点一|点|想吃|要吃|吃|麦当劳|外送|外卖|送到|送份|开车|去拿|到店|自取|得来速|附近|饿了|推荐|随便|我们|自己选|自己点|帮我配|照上次那份|照上次|照旧|立即|现在|中午|明早|今晚|晚上|今天|明天|的|吧|了|，|。|、|！|\s', '', text)
    return re.sub(r'[\W_]', '', text).removeprefix('我')


class AmbiguousIntent(ValueError):
    def __init__(self, rows, message=''):
        self.options = rows[:8]
        numbered='\n'.join(f"{n} {r['name']}　¥{r['price_yuan']}" for n,r in enumerate(self.options,1))
        super().__init__((message+'\n'+numbered).rstrip() if message else '你说的对应几款餐品，请选编号：\n'+numbered)


def resolve_intent(utterance, rows):
    if not intent_text(utterance): return []
    # Split requests without splitting product names such as "双层吉士".
    parts = re.split(r'[、，,。]|再来|加一份|加个|和', utterance)
    result = []
    for part in filter(None, parts):
        from coupon_split.slang import resolve,available
        slang=resolve(part,rows)
        if slang and not slang['selected']:raise AmbiguousIntent(slang['rows'],slang['message'])
        ranked = slang['rows'] if slang else matches(intent_text(part), [r for r in rows if available(r)]) if intent_text(part) else []
        if ranked:
            if slang:
                quantity=re.search(r'(\d+|[一两二三四五六七八九十])(?:个|份|杯|套)',part)
                q=quantity[1] if quantity else '1'
                q=int(q) if q.isdigit() else '一二三四五六七八九十'.index(q.replace('两','二'))+1
                result.append({'productCode':ranked[0]['productCode'],'quantity':min(q,20)})
                continue
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


def default_choices(round_):
    """Default picks for one combo round, filled to the round's minimum.

    isDefault alone is not enough: a round that needs 3 picks may flag only one
    choice. Fill the rest from the remaining official choices, in menu order.
    """
    minimum = int(round_.get('minQuantity', 1) or 0)
    picks = []
    total = 0
    for c in round_['choices']:
        if c.get('isDefault') and c.get('quantity', 1) > 0:
            picks.append({'code': str(c['code']), 'quantity': c.get('quantity', 1)})
            total += c.get('quantity', 1)
    for c in round_['choices']:
        if total >= minimum:
            break
        if any(p['code'] == str(c['code']) for p in picks) or c.get('quantity', 1) <= 0:
            continue
        if c.get('available') is False or c.get('isSelling') is False or c.get('soldOut'):
            continue
        take = min(c.get('quantity', 1), minimum - total)
        picks.append({'code': str(c['code']), 'quantity': take})
        total += take
    return picks


def default_item(detail, quantity=1):
    item = {'productCode': str(detail['code']), 'quantity': quantity}
    if detail.get('rounds'):
        item['roundList'] = [dict(round=str(r['id']), comboItemList=default_choices(r)) for r in detail['rounds']]
    return item


def recommend_rows(rows, people=None):
    from coupon_split.recommendation import fallback
    return fallback(rows, people)
