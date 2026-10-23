"""Menu-bound host context and deterministic web fallback; no model or credentials."""
import re
from collections import OrderedDict
from decimal import Decimal


def role(row):
    name = row.get('name', '')
    category = row.get('category', '')
    if re.search(r'分享|多人|家庭|全家', name + category): return '分享餐'
    if re.search(r'套餐|件套|组合', name): return '套餐'
    if re.search(r'冰淇淋|麦旋风|麦炫酷|蛋挞|圆筒|派', name): return '甜品'
    if re.search(r'汉堡|饭|卷|堡|麦满分|粥', name): return '主食'
    if re.search(r'薯|鸡块|麦乐鸡|鸡翅|玉米', name): return '小食'
    if re.search(r'咖啡|茶|可乐|雪碧|牛奶|果汁|豆浆|水|柠|巧克力|黑巧|奶绿', name): return '饮料'
    if re.search(r'主食|汉堡', category): return '主食'
    if re.search(r'小食|配餐', category): return '小食'
    if re.search(r'饮料|饮品|咖啡', category): return '饮料'
    if re.search(r'甜品|甜点', category): return '甜品'
    return '未知'


def available(row):
    return not any(row.get(k) in (False, 0, '0', 'false') for k in ('available', 'isSelling', 'onSale'))


def spicy(row):
    value = row.get('spicy', row.get('isSpicy'))
    if value is not None: return '辣' if value in (True, 1, '1', 'true', '辣') else '不辣' if value in (False, 0, '0', 'false', '不辣') else '未知'
    text = row.get('name', '') + ' ' + str(row.get('description', ''))
    if re.search(r'不辣|无辣', text): return '不辣'
    if '辣' in text: return '辣'
    return '未知'


def conflicts(row, utterances):
    """Known ingredient conflicts, including selected children, never infer safety."""
    text = row.get('name', '') + ' ' + str(row.get('description', ''))
    reasons = []
    if re.search(r'不(?:吃|要|能吃|爱吃)辣|忌辣|不要辣', utterances) and spicy(row) == '辣': reasons.append('与不吃辣冲突')
    terms=re.findall(r'(?:不吃|不要|不能吃|忌口[：:]?)(牛肉|猪肉|鸡肉|鱼|虾|蛋|奶|花生|芝士|乳制品)', utterances)
    terms+=re.findall(r'(?:不吃|不要|不能吃|忌口[：:]?)([\u4e00-\u9fff]{1,8})',utterances)
    for term in terms:
        term=re.split(r'帮|但|其余|其他|然后',term)[0].rstrip('的')
        if not term or term in {'餐具','辣','太辣','外带','堂食','配送','塑料袋'}:continue
        synonyms = {'牛肉': '牛|吉士', '猪肉': '猪|培根|火腿', '鸡肉': '鸡', '乳制品': '乳|奶|芝士|吉士'}
        if re.search(synonyms.get(term, re.escape(term)), text): reasons.append('与忌口' + term + '冲突')
    for term in re.findall(r'(?:对)?(花生|牛奶|鸡蛋|坚果|海鲜|乳制品)(?:严重)?过敏', utterances):
        # Unknown allergen information cannot establish safety.
        allergens = row.get('allergens')
        if allergens is None or term in str(allergens): reasons.append(term + '过敏：官方资料未证实可安全食用')
    return reasons


def budget(utterances):
    found = re.findall(r'(?:预算|不超过|最多|限额)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)', utterances)
    return int(Decimal(found[-1]) * 100) if found else None


def fallback(rows, people=None):
    count = people or 1
    if type(count) is not int or not 1 <= count <= 12: raise ValueError('人数范围 1–12')
    rows = [r for r in rows if available(r)]
    def rank(r):
        try: price = Decimal(r.get('price_yuan') or '9999')
        except Exception: price = Decimal('9999')
        return (price, r['productCode'])
    groups = {key: sorted([r for r in rows if role(r) == key], key=rank) for key in ('主食','小食','饮料','分享餐')}
    picked = OrderedDict()
    def add(row, quantity=1):
        code = row['productCode']
        if code not in picked: picked[code] = [row, 0]
        picked[code][1] += quantity
    # Only count a sharing meal when the official catalogue states coverage.
    remaining = {'主食': count, '小食': (count + 1)//2, '饮料': count}
    if count >= 3:
        for row in groups['分享餐']:
            coverage = row.get('servingsByRole')
            if isinstance(coverage, dict) and all(type(v) is int and v >= 0 for v in coverage.values()) and coverage.get('主食', 0) > 0 and all(coverage.get(k,0) <= v for k,v in remaining.items()):
                add(row)
                remaining = {k: v - coverage.get(k, 0) for k,v in remaining.items()}
                break
    for key, quantity in remaining.items():
        candidates = groups[key]
        for i in range(quantity):
            if candidates: add(candidates[i % len(candidates)])
    return [(row, quantity) for row, quantity in picked.values()]
