"""resolve_order: the host agent's reading of the user's words, grounded on real data.

The host understands the user and passes that reading in the ontology's slot structure.
This module checks every slot against what McDonald's actually returns and reports what is
resolved (and from which source), what is ambiguous (candidates plus the one fact that
tells them apart), what must still be asked, and which constraints are violated. It
never decides what the host says next.
"""
import copy
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coupon_split import grounding, ontology as onto
from coupon_split.menu import AmbiguousIntent, default_item, menu_rows, normalized_name, resolve_intent
from coupon_split.timeparse import TimeClarification, parse_time

FULFILLMENT = onto.MODEL['entities']['Fulfillment']['values']
ADDRESS_HINT = re.compile(r'(?:送到|送去|送至|送往|外送到|外卖到|配送到|地址是|地址在|地址(?:改成|改为|换成|换到|改到))([^，。,；！!？?]+)')


class Food(BaseModel):
    model_config = ConfigDict(extra='ignore')
    phrase: str = Field(description='用户说的餐品，保持用户的词（黑话、简称、错别字都行）；规格词可以留在里面')
    quantity: int | None = Field(default=None, ge=1, le=20, description='数量；用户没说就不填')
    spec: list[str] = Field(default_factory=list, description='规格，如 大杯、去冰')


class When(BaseModel):
    model_config = ConfigDict(extra='ignore')
    kind: Literal['immediate', 'reserve'] = 'immediate'
    text: str | None = Field(default=None, description='用户说的预约时间原话，如“明天中午12点半”；由服务端解析')
    at: str | None = Field(default=None, description='你已经算出的预约时间，上海时间 YYYY-MM-DD HH:MM；与 text 二选一')


class Interpretation(BaseModel):
    """Your reading of what the user said, slot by slot. Leave out whatever the user did not say."""
    model_config = ConfigDict(extra='ignore')
    fulfillment: Literal['pickup', 'drive', 'delivery'] | None = Field(default=None, description='取餐方式：pickup 到店自取，drive 得来速，delivery 外送')
    city: str | None = Field(default=None, description='城市（自取、得来速查门店用）')
    landmark: str | None = Field(default=None, description='地标、小区或街道（自取、得来速）')
    address: str | None = Field(default=None, description='外送时用户对地址的说法，原样转述，如“北二门那个地址”“公司”；服务端会对上账户已保存的地址')
    when: When | None = Field(default=None, description='取餐或送达时间；用户没提时间就不填（默认立即）')
    foods: list[Food] | None = Field(default=None, description='这一单完整的餐品清单（会替换之前的）；本轮没提餐品就不填')
    people: int | None = Field(default=None, description='几个人吃')
    avoid: list[str] | None = Field(default=None, description='忌口')
    plan: Literal['帮我配', '我自己点', '照上次那份'] | None = Field(default=None, description='用户要你配餐、自己在点餐页点，还是照上次')
    tableware: bool | None = Field(default=None, description='是否要餐具')
    take_way: Literal['eat-in', 'take-in-store'] | None = Field(default=None, description='堂食 eat-in 或外带 take-in-store')


def fulfillment_said(text):
    for value in ('delivery', 'drive', 'pickup'):
        if any(word in text for word in FULFILLMENT[value]['aliases']):
            return value
    return None


def address_hint(text):
    found = ADDRESS_HINT.search(text or '')
    return found.group(1).strip() if found else ''


def time_text(text):
    hint = address_hint(text)
    return text.replace(hint, '') if hint else text


def saved_list(data):
    return data if isinstance(data, list) else data.get('addresses', [])


async def saved_addresses(s, t):
    return saved_list((await s.upstream.call(t, 'delivery-query-addresses', {}))['data'])


def source_of(element, key):
    return {'last': 'saved_data', 'account': 'saved_data', 'nearest': 'lookup', 'utterance': 'user_utterance'}.get(element.get('sources', {}).get(key), 'default')


def context_for(s, t, e, store_code):
    return s.order_context(s.get_store_context(t, store_code), e, require_take=False)


def ground_phrase(phrase, rows):
    """('ok', row) | ('ambiguous', rows, note) | ('violation', code, facts)."""
    from coupon_split.slang import resolve, available, NOT_FOOD
    # the user's phrase is, word for word, the official name of exactly one selling menu item: that item
    same = [r for r in rows if available(r) and normalized_name(r['name']) == normalized_name(phrase)]
    if len(same) == 1:
        return 'ok', same[0], {}
    slang = resolve(phrase, rows)
    if slang:
        if slang['selected']:
            return 'ok', slang['rows'][0], {}
        if slang.get('unavailable'):  # the named item is not selling here; the rows are replacements, not what the user asked for
            return 'violation', 'ITEM_SOLD_OUT', {'phrase': phrase, 'alternatives': '、'.join(r['name'] for r in slang['rows']) or '无'}
        if slang['entry']['kind'] in NOT_FOOD:
            return 'violation', 'ITEM_NOT_FOOD', {'phrase': phrase, 'note': slang['message']}
        selling = [r for r in slang['rows'] if available(r)]
        if not selling:
            return 'violation', 'ITEM_SOLD_OUT', {'phrase': phrase, 'alternatives': '、'.join(r['name'] for r in slang['rows']) or '无'}
        return 'ambiguous', selling, slang['entry'].get('note', '')
    try:
        picked = resolve_intent(phrase, rows)
    except AmbiguousIntent as exc:
        return 'ambiguous', exc.options, ''
    if not picked:
        return 'violation', 'ITEM_NOT_FOUND', {'phrase': phrase}
    row = next(r for r in rows if r['productCode'] == picked[0]['productCode'])
    return 'ok', row, {}


def candidate_view(rows):
    return [{'name': r['name'], 'productCode': r['productCode'], 'price_yuan': r.get('price_yuan', '')} for r in rows[:8]]


async def ground_foods(s, t, e, store_code, store_name, foods, utterance=''):
    """Foods -> items on the live menu. Returns (items, names, ambiguous, violations)."""
    context = context_for(s, t, e, store_code)
    menu = (await s.upstream.call(t, 'query-meals', dict(context, storeCode=store_code)))['data']
    rows = menu_rows(menu)
    items, names, ambiguous, violations = [], [], [], []
    for food in foods:
        phrase = food['phrase'].strip()
        specs = list(food.get('spec') or [])
        counted = re.search(r'(\d+|[一两二三四五六七八九十]+)\s*(?:个|份|杯|套)', phrase)
        if counted and food.get('quantity') is None:
            raw = counted[1]
            food = dict(food, quantity=int(raw) if raw.isdigit() else '一二三四五六七八九十'.index(raw.replace('两', '二')[-1]) + 1)
            phrase = (phrase[:counted.start()] + phrase[counted.end():]).strip() or phrase
        official = [r for r in rows if normalized_name(phrase) in normalized_name(r['name'])]
        core, extra = grounding.split_spec(phrase)
        for marker in grounding.size_markers(core):  # "中薯条" is the item 薯条 asked in size 中
            core = core.replace(marker, '', 1)
            extra = extra + [marker]
        core = {'薯': '薯条'}.get(core, core)  # "中薯" is how people say 中薯条
        if extra and core and not official:
            outcome = ground_phrase(core, rows)
            if outcome[0] == 'violation' and outcome[1] == 'ITEM_NOT_FOUND':
                outcome = ground_phrase(phrase, rows)
            else:
                specs = specs + [w for w in extra if w not in specs]
        else:
            outcome = ground_phrase(phrase, rows)
        kind = outcome[0]
        if kind == 'violation':
            facts = dict(outcome[2], store=store_name)
            violations.append(onto.violation(outcome[1], **facts))
            continue
        if kind == 'ambiguous':
            ambiguous.append({'slot': 'items', 'phrase': phrase, 'candidates': candidate_view(outcome[1]),
                              'ask': onto.violation('ITEM_AMBIGUOUS', phrase=phrase, store=store_name, options='、'.join(r['name'] for r in outcome[1][:8]))['fact'] + (outcome[2] or '')})
            continue
        row = outcome[1]
        quantity = food.get('quantity')
        if quantity is None:
            parsed = re.search(r'(\d+|[一两二三四五六七八九十])\s*(?:个|份|杯|套)', phrase)
            if parsed:
                raw = parsed[1]
                quantity = int(raw) if raw.isdigit() else '一二三四五六七八九十'.index(raw.replace('两', '二')) + 1
            else:
                quantity = 1
        quantity = max(1, min(int(quantity), 20))
        detail = (await s.upstream.call(t, 'query-meal-detail', dict(context, storeCode=store_code, code=row['productCode'])))['data']
        item = default_item(detail, quantity)
        applied = []
        specs = specs + [m for m in grounding.size_markers(phrase) if m not in specs and not any(m in s for s in specs)]
        specs = specs + [w for w in grounding.neighbour_specs(utterance, phrase) if w not in specs and w not in phrase]
        if specs:
            item, applied, problems = grounding.apply_specs(item, detail, specs, phrase=row['name'], name=row['name'])
            if problems:
                violations.extend(problems)
                continue
        items.append(item)
        names.append(row['name'] + (' ×%d' % quantity if quantity > 1 else '') + ('（%s）' % '、'.join(applied) if applied else ''))
    if items:
        try:
            await s.validate_selected(t, store_code, items, context, fill_defaults=True)
        except ValueError as exc:
            violations.append(onto.violation('ITEM_NOT_OFFERED_FOR_WAY', detail=str(exc)))
            items, names = [], []
    return items, names, ambiguous, violations


def time_problem(note, now):
    if '已经过去' in note:
        return onto.violation('TIME_IN_PAST', time=note, now=now.strftime('%Y-%m-%d %H:%M'))
    return onto.violation('TIME_UNCLEAR', detail=note)


def when_from(interp_when, utterance, now, prior=None):
    """(when dict | None, source, ambiguity | None, note) from the host's reading and the user's own words."""
    parsed_error = ''
    mentioned, reservation = False, None
    try:
        mentioned, reservation = parse_time(time_text(utterance), now)
    except TimeClarification as exc:
        parsed_error = str(exc)
    said = {'kind': 'reserve', 'at': reservation} if reservation else {'kind': 'immediate'} if mentioned else None
    if interp_when is None:
        if said:
            return said, 'user_utterance', None, ''
        if prior and not parsed_error:
            return {'kind': 'reserve', 'at': prior}, 'saved_data', None, ''
        if parsed_error:
            return None, 'user_utterance', None, parsed_error
        return {'kind': 'immediate'}, 'default', None, ''
    mine = {'kind': interp_when.get('kind', 'immediate')}
    if mine['kind'] == 'reserve':
        at = interp_when.get('at')
        if not at and interp_when.get('text'):
            try:
                _, at = parse_time(interp_when['text'], now)
            except TimeClarification as exc:
                return None, 'host_interpretation', None, str(exc)
        if not at:
            return None, 'host_interpretation', None, '预约时间没有给出具体的日期和时刻'
        mine['at'] = at
    if said and said != mine:
        return None, 'conflict', {'slot': 'time', 'candidates': [said, mine],
                                  'ask': '用户原话解析出的时间与你传入的时间不一致，需要问用户到底是哪个时间'}, ''
    if said:
        return mine, 'user_utterance', None, ''
    if mine['kind'] == 'reserve' and not parsed_error and not mentioned:
        return None, 'host_unverified', None, '用户原话里没有出现预约时间，不能当作用户说过'
    return mine, 'host_interpretation' if mine['kind'] == 'reserve' else 'default', None, ''


async def apply(s, t, sid, interp, utterance, check_items=True, stores=None):
    """Ground an interpretation on the session's cart. Returns the report dict (no wording for the host)."""
    now = grounding.now_cn()
    cart = s.carts[sid]
    e = copy.deepcopy(cart['elements'])
    e.setdefault('sources', {})
    resolved, ambiguous, missing, violations = {}, [], [], []

    # ---- fulfillment
    said = fulfillment_said(utterance)
    way = interp.get('fulfillment')
    if way and said and way != said:
        ambiguous.append({'slot': 'fulfillment', 'candidates': [said, way], 'ask': '用户原话里的取餐方式与你传入的不一致，需要问用户是自取、得来速还是外送'})
        way = None
    elif way:
        if way == said or interp.get('trust_fulfillment'):
            source = 'user_utterance'
        elif way == e.get('fulfillment'):
            source = source_of(e, 'fulfillment')
        else:
            source = 'host_unverified'
            missing.append({'slot': 'fulfillment', 'reason': '用户原话里没有表达取餐方式（自取、得来速或外送），不能替用户定'})
            way = None
        if way:
            resolved['fulfillment'] = {'value': way, 'source': source}
    elif said:
        way = said
        resolved['fulfillment'] = {'value': way, 'source': 'user_utterance'}
    elif e.get('fulfillment'):
        way = e['fulfillment']
        resolved['fulfillment'] = {'value': way, 'source': source_of(e, 'fulfillment')}
    else:
        missing.append({'slot': 'fulfillment', 'reason': onto.SLOTS['fulfillment']['ask']})
    if way and way != e.get('fulfillment'):
        e['fulfillment'] = way
        e['sources']['fulfillment'] = 'utterance' if resolved['fulfillment']['source'] == 'user_utterance' else 'default'
        if e.get('addressId') and way != 'delivery':
            e.pop('addressId', None)

    # ---- place -> store
    city = interp.get('city') or cart.get('city') or ''
    landmark = interp.get('landmark') or cart.get('location') or ''
    stores, store, address_id = (stores if stores is not None else None), None, e.get('addressId', '')
    given_stores = stores is not None
    if way == 'delivery':
        addresses = await saved_addresses(s, t)
        by_id = interp.get('address_id') and any(str(a.get('addressId')) == str(interp['address_id']) for a in addresses)
        hint = interp.get('address') or ('' if by_id else address_hint(utterance))
        if by_id and not interp.get('address'):
            address_id = str(interp['address_id'])  # chosen by id (find_store): there is no wording to match
            e['addressId'] = address_id
            e['sources']['addressId'] = 'utterance'
            resolved['address'] = {'value': {'addressId': address_id}, 'source': 'user_utterance'}
        elif hint or not address_id or not any(str(a.get('addressId')) == str(address_id) for a in addresses):
            found, candidates, problems = grounding.ground_address(hint, addresses)
            if found:
                address_id = str(found['addressId'])
                e['addressId'] = address_id
                e['sources']['addressId'] = 'utterance' if hint else 'default'
                resolved['address'] = {'value': grounding.address_view(found), 'source': 'user_utterance' if hint else 'default'}
            elif candidates and not problems:
                address_id = ''
                e.pop('addressId', None)
                ambiguous.append({'slot': 'address', 'candidates': [grounding.address_view(a) for a in candidates],
                                  'ask': '用户没说送到哪一个已保存地址，需要问用户选哪一个'})
            else:
                address_id = ''
                e.pop('addressId', None)
                if problems and problems[0]['reason_code'] == 'ADDRESS_AMBIGUOUS':
                    ambiguous.append({'slot': 'address', 'candidates': [grounding.address_view(a) for a in candidates], 'ask': problems[0]['fact']})
                else:
                    violations.extend(problems)
        else:
            resolved['address'] = {'value': {'addressId': str(address_id)}, 'source': source_of(e, 'addressId')}
        if address_id and stores is None:
            stores = await s.lookup_stores(t, 'delivery', address_id=address_id)
    elif way in ('pickup', 'drive'):
        if not landmark:
            missing.append({'slot': 'landmark', 'reason': onto.SLOTS['landmark']['ask']})
        elif not city:
            missing.append({'slot': 'city', 'reason': onto.SLOTS['city']['ask']})
        else:
            if stores is None:
                stores = await s.lookup_stores(t, way, city, landmark)
            resolved['landmark'] = {'value': landmark, 'source': 'user_utterance' if landmark in utterance else 'host_inferred'}
            resolved['city'] = {'value': city, 'source': 'host_inferred' if interp.get('city') else 'saved_data'}
            if not stores:
                violations.append(onto.violation('STORE_NOT_FOUND', landmark=landmark, city=city))
    if stores:
        store = stores[0]
        resolved['store'] = {'value': s.public_store(store), 'source': 'lookup', 'candidates_considered': len(stores)}
        e['sources']['store_code'] = 'nearest'

    # ---- time
    mentioned_when = interp.get('when')
    when, time_source, conflict, note = when_from(mentioned_when, utterance, now, prior=e.get('reservationDate'))
    e['reservationDate'] = None
    if conflict:
        ambiguous.append(conflict)
    elif when is None and time_source == 'host_unverified':
        missing.append({'slot': 'time', 'reason': note})
    elif when is None:
        violations.append(time_problem(note or onto.SLOTS['time']['ask'], now))
    elif store:
        good, problems = grounding.ground_time(when, store, now)
        if good:
            e['reservationDate'] = good.get('at')
            e['sources']['reservationDate'] = 'utterance' if time_source == 'user_utterance' else 'default'
            resolved['time'] = {'value': good, 'source': time_source}
        violations.extend(problems)
    else:
        resolved['time'] = {'value': when, 'source': time_source, 'verified_against_store': False}
        if when.get('kind') == 'reserve':
            e['reservationDate'] = when['at']

    # ---- other slots
    if interp.get('people') is not None:
        if 1 <= interp['people'] <= 12:
            resolved['people'] = {'value': interp['people'], 'source': 'user_utterance'}
        else:
            violations.append(onto.violation('PEOPLE_RANGE', people=interp['people']))
    if interp.get('avoid'):
        resolved['avoid'] = {'value': interp['avoid'], 'source': 'user_utterance'}
    if interp.get('tableware') is not None:
        e['needTableware'] = bool(interp['tableware']); e['sources']['needTableware'] = 'utterance'
        resolved['tableware'] = {'value': e['needTableware'], 'source': 'user_utterance'}
    if interp.get('take_way'):
        e['takeWayCode'] = interp['take_way']; e['sources']['takeWayCode'] = 'utterance'
        resolved['takeWay'] = {'value': interp['take_way'], 'source': 'user_utterance'}

    # ---- foods
    foods = interp.get('foods')
    pending = cart.get('pending_foods') or []
    if foods is not None:
        pending = [f if isinstance(f, dict) else f.model_dump() for f in foods]
    if foods is None and not pending and store and cart.get('foods_raw') and cart.get('store_code') != store['storeCode']:
        pending = cart['foods_raw']  # the store changed: the same foods are grounded again on the new menu
    foods_raw = cart.get('foods_raw') or []
    items = copy.deepcopy(cart.get('items', [])) if foods is None and not pending else []
    item_names = cart.get('item_summary', '') if items else ''
    if pending and store and not violations_block_store(violations):
        a = (await s.account_info()).structuredContent
        e.update(account_name=a['account_name'], account_last4=a['account_last4'])
        items, names, amb, probs = await ground_foods(s, t, e, store['storeCode'], store['storeName'], pending, utterance)
        ambiguous.extend(amb)
        violations.extend(probs)
        item_names = '、'.join(names)
        foods_raw, pending = pending, []
        if items:
            resolved['items'] = {'value': [{'name': n} for n in names], 'source': 'user_utterance', 'selected_items': items}
    elif pending:
        foods_raw = pending
        resolved['items'] = {'value': [{'phrase': f['phrase']} for f in pending], 'source': 'user_utterance', 'grounded': False,
                             'note': '门店还没确定，餐品先记下，门店确定后再落到菜单'}
    elif items:
        resolved['items'] = {'value': [{'name': item_names}], 'source': 'saved_data', 'selected_items': items}
    plan = interp.get('plan') or cart.get('plan')
    if plan:
        resolved['plan'] = {'value': plan, 'source': 'user_utterance' if interp.get('plan') else 'saved_data'}
    if check_items and not items and not pending and not ambiguous and not any(v['slot'] == 'items' for v in violations) and not plan:
        missing.append({'slot': 'items', 'reason': onto.SLOTS['items']['ask']})

    open_questions = [{'slot': a['slot'], 'fact': a['ask']} for a in ambiguous] + [{'slot': m['slot'], 'fact': m['reason']} for m in missing]
    changed_items = foods is not None
    cart.update(elements=e, store_code=store['storeCode'] if store else None, location=landmark if way in ('pickup', 'drive') else cart.get('location', ''),
                city=city, items=items, item_summary=item_names, intent_resolved=bool(items), pending_foods=pending, foods_raw=foods_raw,
                violations=violations, open_questions=open_questions, plan=plan,
                people=interp['people'] if interp.get('people') else cart.get('people'), avoid=interp.get('avoid') or cart.get('avoid'),
                last_utterance=utterance, **({'submitted': False, 'pending_plan_id': None} if changed_items or store is None else {}))
    if items and store:
        cart.update(submitted=True)
    return {'resolved': resolved, 'ambiguous': ambiguous, 'missing': missing, 'violations': violations,
            'now': now.strftime('%Y-%m-%d %H:%M'), 'store': store}


def violations_block_store(violations):
    return any(v['slot'] in ('store', 'address', 'fulfillment') for v in violations)


async def check_cart_time(s, cart):
    """After a store is chosen elsewhere (find_store, mcd_start), re-check the cart's reservation time against it."""
    e = cart['elements']
    store = s.get_store_context(s.token(), cart['store_code']) if cart.get('store_code') else None
    cart_violations = [v for v in cart.get('violations', []) if v['slot'] != 'time']
    if store:
        when = {'kind': 'reserve', 'at': e['reservationDate']} if e.get('reservationDate') else {'kind': 'immediate'}
        good, problems = grounding.ground_time(when, store)
        if not good:
            e['reservationDate'] = None
        cart_violations.extend(problems)
    cart.update(elements=e, violations=cart_violations)
    return cart_violations
