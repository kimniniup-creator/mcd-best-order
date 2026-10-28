"""Ground the host's reading of an utterance on real data, deterministically.

The host agent understands the user and passes that understanding in the ontology's
slot structure. This module only checks it against what McDonald's actually returns
(saved addresses, store windows, menu, product options) and reports resolved values,
open questions and violated constraints. It never words an answer for the host.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from coupon_split.ontology import MODEL, violation

TZ = ZoneInfo('Asia/Shanghai')
WINDOW = re.compile(r'(\d{1,2}):(\d{2})\s*至\s*(\d{1,2}):(\d{2})')
SPEC_WORDS = sorted({w for words in MODEL['entities']['Spec']['axes'].values() for w in words}, key=len, reverse=True)
ADDRESS_ALIASES = MODEL['entities']['DeliveryAddress'].get('aliases', {})
FILLER = re.compile(r'送到|送去|送至|送|外送|外卖|麦乐送|那个地址|那个|这个|地址|附近|我的|我|的|上次|之前|常用|老地方|就是|那边|那里|这里|里面|门口|地方|位置|吧|啊|呀|，|。|,|\s')


def now_cn():
    return datetime.now(TZ)


# ---------------------------------------------------------------- time windows

def _minutes(h, m):
    return int(h) * 60 + int(m)


def windows_by_date(store):
    """{'2026-10-28': [(start_min, end_min), ...]} from the store's reservation options."""
    out = {}
    for option in store.get('reservationTimeOptions') or []:
        found = [(_minutes(a, b), _minutes(c, d)) for a, b, c, d in WINDOW.findall(option.get('reservationOptionText', ''))]
        if option.get('date') and found:
            out[option['date']] = sorted(found)
    return out


def _fmt(window):
    return '%02d:%02d-%02d:%02d' % (window[0] // 60, window[0] % 60, window[1] // 60, window[1] % 60)


def windows_text(windows):
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return '、'.join(_fmt(w) for w in merged)


def ground_time(when, store, now=None):
    """Return (resolved, violations) for a requested time against a store's windows."""
    now = now or now_cn()
    kind = (when or {}).get('kind') or 'immediate'
    name = store.get('storeName') or store.get('name') or '这家店'
    by_date = windows_by_date(store)
    today = now.date().isoformat()
    minute = now.hour * 60 + now.minute
    if kind == 'immediate':
        if store.get('businessStatus') is False:
            hours = '%s-%s' % (store.get('businessStartTime', '?'), store.get('businessEndTime', '?'))
            return None, [violation('STORE_CLOSED', store=name, hours=hours)]
        windows = by_date.get(today)
        # Delivery lists day-parts that start and end at fixed times, so "now" must sit inside one. Pickup and drive list slots
        # that already start after the lead time, so an open store can always serve "now".
        if store.get('fulfillment', 'delivery') == 'delivery' and windows is not None and not any(s <= minute <= e for s, e in windows):
            later = [w for w in windows if w[0] > minute]
            shown = windows_text(later or windows)
            return None, [violation('IMMEDIATE_NOT_AVAILABLE', store=name, now=now.strftime('%H:%M'), windows=shown or '今天没有可预约时段')]
        return {'kind': 'immediate'}, []
    at = (when or {}).get('at') or ''
    try:
        target = datetime.strptime(at.strip(), '%Y-%m-%d %H:%M').replace(tzinfo=TZ)
    except ValueError:
        return None, [violation('TIME_OUTSIDE_WINDOW', store=name, date=at or '?', time=at or '?', windows='请给出 YYYY-MM-DD HH:MM 形式的预约时间')]
    if target <= now:
        return None, [violation('TIME_IN_PAST', time=at, now=now.strftime('%Y-%m-%d %H:%M'))]
    date = target.date().isoformat()
    windows = by_date.get(date)
    tm = target.hour * 60 + target.minute
    if by_date and (windows is None or not any(s <= tm <= e for s, e in windows)):
        shown = windows_text(windows) if windows else '这一天没有可预约时段（可预约日期：%s）' % ('、'.join(by_date) or '无')
        return None, [violation('TIME_OUTSIDE_WINDOW', store=name, date=date, time=at, windows=shown)]
    return {'kind': 'reserve', 'at': target.strftime('%Y-%m-%d %H:%M')}, []


# ------------------------------------------------------------------- addresses

def _cleaned(text):
    return FILLER.sub('', text or '')


def _grams(text, n=2):
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def match_addresses(hint, addresses):
    """Rank saved addresses by how much distinctive text the user's wording shares with them.

    Returns [(score, address)] best first. A synonym class (学校 -> 大学/校区 ...) counts
    for a match when the literal wording shares nothing with the saved text.
    """
    core = re.sub(r'[\s，。,;；、]', '', hint or '')
    scored = []
    for address in addresses:
        text = re.sub(r'\s', '', address.get('fullAddress', '') + address.get('address', '') + address.get('addressDetail', ''))
        score = 0
        # longest common substring (the user's own wording is kept whole) that covers at least half of what they said
        need = max(2, (len(_cleaned(hint)) + 1) // 2)
        for size in range(min(len(core), 16), need - 1, -1):
            if any(core[i:i + size] in text for i in range(len(core) - size + 1)):
                score = size
                break
        if not score:
            for word, synonyms in ADDRESS_ALIASES.items():
                if word in core and any(s in text for s in synonyms):
                    score = 1
                    break
        if score:
            scored.append((score, address))
    scored.sort(key=lambda pair: -pair[0])
    return scored


def ground_address(hint, addresses):
    """(resolved_address | None, candidates, violations) for an address description."""
    if not addresses:
        return None, [], [violation('ADDRESS_NOT_SAVED', hint=hint or '', count=0)]
    if not _cleaned(hint):
        return None, list(addresses), []  # nothing said about which one: the user picks
    ranked = match_addresses(hint, addresses)
    if not ranked:
        return None, [], [violation('ADDRESS_NOT_SAVED', hint=hint, count=len(addresses))]
    best = ranked[0][0]
    top = [a for s, a in ranked if s == best]
    if len(top) == 1:
        return top[0], [], []
    return None, top, [violation('ADDRESS_AMBIGUOUS', hint=hint, count=len(top))]


def address_view(address, with_text=True):
    view = {'addressId': str(address.get('addressId'))}
    if with_text:
        view['fullAddress'] = address.get('fullAddress', address.get('address', ''))
    return view


# ----------------------------------------------------------------------- specs

def _modification_options(node):
    """(name, code, key, group_index) for every selectable modification under a node."""
    found = []
    items = (node or {}).get('modification', {}).get('items', []) if isinstance(node, dict) else []
    for group, item in enumerate(items):
        for value in item.get('values', []):
            key = value.get('selectedKey') or value.get('unselectedKey')
            if key is not None and value.get('name'):
                found.append((value['name'], str(value['code']), str(key), group, value.get('selectedQuantity', 0)))
    return found


def spec_options(detail):
    names = []
    for r in detail.get('rounds', []):
        names.extend(c.get('name', '') for c in r['choices'])
        for c in r['choices'][:1]:
            names.extend(n for n, *_ in _modification_options(c))
    names.extend(n for n, *_ in _modification_options(detail))
    return list(dict.fromkeys(n for n in names if n and n != '标准'))


SIZE = re.compile(r'(超大|大|中|小)(?:杯|份|号)?$')
SIZE_MARK = re.compile(r'(超大|大|中|小)(?=薯|杯|份|号|块|可乐|饮)')


def size_markers(phrase):
    """Size qualifiers the user attached to a food ("大薯条", "中杯可乐"). They must be met, not dropped."""
    return list(dict.fromkeys(SIZE_MARK.findall(phrase)))


def in_official_name(spec, *names):
    """A size already built into the product's own name ("薯条中", "可口可乐(中杯)") is met by that product."""
    found = SIZE.match(spec)
    return bool(found) and any(found[1] in name for name in names if name)


SPEC_TOKEN = '|'.join(re.escape(w) for w in SPEC_WORDS if len(w) >= 2)
BEFORE = re.compile(r'(?:' + SPEC_TOKEN + r'|[的、，, ])+$')
AFTER = re.compile(r'^[（(]?(?:' + SPEC_TOKEN + r'|[的、 ])+')


def neighbour_specs(utterance, phrase):
    """Spec words the user attached directly to this food in their own sentence ("大杯去冰的可口可乐", "可乐去冰").

    The host may drop them while reading the sentence; they are looked up again on the user's words, never guessed.
    """
    at = utterance.find(phrase) if phrase else -1
    if at < 0:
        return []
    before = BEFORE.search(utterance[:at])
    after = AFTER.match(utterance[at + len(phrase):])
    found = []
    for text in ((before.group(0) if before else ''), (after.group(0) if after else '')):
        found += [w for w in re.findall(SPEC_TOKEN, text) if w not in found]
    return found


def split_spec(phrase):
    """Peel official-spec words (大杯, 去冰 ...) off a food phrase. Returns (core, [specs])."""
    specs = []
    core = phrase
    for word in SPEC_WORDS:
        if len(word) >= 2 and word in core and len(core.replace(word, '')) >= 1:
            specs.append(word)
            core = core.replace(word, '')
    core = re.sub(r'[()（）\s]', '', core)
    return core.strip(), specs


def apply_specs(item, detail, specs, phrase='', name=''):
    """Set the requested specs on an item using only the product's official options.

    Returns (item, applied_names, violations). A spec with no official option is
    reported, never replaced by a near one.
    """
    applied, problems = [], []
    rounds = detail.get('rounds', [])
    for spec in specs:
        hit = False
        if in_official_name(spec, detail.get('name', ''), name):
            applied.append(spec)
            continue
        # 1. a combo/drink round choice named for the spec (可乐大杯)
        for round_ in rounds:
            default = next((c for c in round_['choices'] if c.get('isDefault')), round_['choices'][0] if round_['choices'] else None)
            matches = [c for c in round_['choices'] if spec in c.get('name', '') and c.get('available') is not False]
            if matches and default:
                stem = default.get('name', '')
                for size in SPEC_WORDS:
                    stem = stem.replace(size, '')
                exact = [c for c in matches if c.get('name', '').replace(spec, '') == stem]
                pick = (exact or matches)
                if len(pick) > 1 and not exact:
                    problems.append(violation('ITEM_AMBIGUOUS', phrase=phrase + spec, store='这家店', options='、'.join(c['name'] for c in pick[:6])))
                    hit = True
                    break
                chosen = pick[0]
                entry = next((r for r in item.setdefault('roundList', [{'round': str(x['id']), 'comboItemList': []} for x in rounds]) if r['round'] == str(round_['id'])), None)
                if entry is not None:
                    entry['comboItemList'] = [{'code': str(chosen['code']), 'quantity': max(1, int(round_.get('minQuantity', 1) or 1))}]
                    applied.append(chosen['name'])
                    hit = True
                    break
        if hit:
            continue
        # 2. a modification option with that exact name (去冰), at item level or on the chosen choice
        targets = [(item, detail)]
        for entry in item.get('roundList', []):
            round_ = next((r for r in rounds if str(r['id']) == entry['round']), None)
            for child in entry['comboItemList']:
                official = next((c for c in (round_ or {}).get('choices', []) if str(c['code']) == str(child['code'])), None)
                if official:
                    targets.append((child, official))
        for holder, node in targets:
            options = _modification_options(node)
            found = next((o for o in options if o[0] == spec), None) or next((o for o in options if spec in o[0] and o[0] != '标准'), None)
            if found:
                name, code, key, group, selected = found
                values = holder.setdefault('modification', {}).setdefault('values', [])
                values[:] = [v for v in values if not (str(v.get('code')) == code and v.get('key') == key)]
                if not selected:
                    values.append({'code': code, 'key': key, 'quantity': 1})
                applied.append(name)
                hit = True
                break
        if not hit:
            options = spec_options(detail)
            problems.append(violation('SPEC_NOT_OFFERED', phrase=phrase or detail.get('name', ''), spec=spec, options='、'.join(options) if options else '这个商品没有可选规格，只有官方默认'))
    return item, applied, problems
