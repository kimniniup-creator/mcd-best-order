"""Per-account preference library: dietary limits, default order elements, saved meals, personal aliases.

Stored beside the conversation memory, one file per account, never in the
plugin directory, so upgrades do not overwrite it. Every write goes through
`validate`; unknown keys and malformed values are rejected, not dropped.
"""
import copy

from coupon_split.local import LocalState

FULFILLMENT = {'pickup', 'drive', 'delivery'}
TAKE_WAY = {'eat-in', 'take-in-store'}
LIMIT = {'avoid': 30, 'taste': 30, 'meals': 20, 'aliases': 50, 'items': 20, 'text': 40}
EMPTY = {'avoid': [], 'taste': [], 'defaults': {}, 'meals': [], 'aliases': []}
DEFAULT_KEYS = {'fulfillment', 'store_name', 'city', 'landmark', 'take_way', 'tableware'}


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} 不能为空')
    value = value.strip()
    if len(value) > LIMIT['text']:
        raise ValueError(f'{field} 最长 {LIMIT["text"]} 个字')
    return value


def _words(values, field):
    if not isinstance(values, list) or len(values) > LIMIT[field]:
        raise ValueError(f'{field} 最多 {LIMIT[field]} 项')
    return list(dict.fromkeys(_text(v, field) for v in values))


def _foods(values, field):
    if not isinstance(values, list) or not values or len(values) > LIMIT['items']:
        raise ValueError(f'{field} 要列 1 到 {LIMIT["items"]} 样餐品')
    return [_text(v, field) for v in values]


def validate(data):
    """Return a normalized copy, or raise ValueError naming the bad field."""
    if not isinstance(data, dict):
        raise ValueError('偏好格式不对')
    unknown = set(data) - set(EMPTY)
    if unknown:
        raise ValueError('不认识的偏好项：' + '、'.join(sorted(unknown)))
    out = copy.deepcopy(EMPTY)
    out['avoid'] = _words(data.get('avoid', []), 'avoid')
    out['taste'] = _words(data.get('taste', []), 'taste')
    defaults = data.get('defaults', {})
    if not isinstance(defaults, dict) or set(defaults) - DEFAULT_KEYS:
        raise ValueError('默认要素只能是：' + '、'.join(sorted(DEFAULT_KEYS)))
    for key, value in defaults.items():
        if value in (None, ''):
            continue
        if key == 'fulfillment' and value not in FULFILLMENT:
            raise ValueError('取餐方式只能是 pickup / drive / delivery')
        if key == 'take_way' and value not in TAKE_WAY:
            raise ValueError('堂食外带只能是 eat-in / take-in-store')
        if key == 'tableware':
            if not isinstance(value, bool):
                raise ValueError('餐具只能是 是 / 否')
            out['defaults'][key] = value
            continue
        out['defaults'][key] = value if key in {'fulfillment', 'take_way'} else _text(value, key)
    meals = data.get('meals', [])
    if not isinstance(meals, list) or len(meals) > LIMIT['meals']:
        raise ValueError(f'常点的餐最多 {LIMIT["meals"]} 份')
    names = set()
    for meal in meals:
        name = _text((meal or {}).get('name'), '常点名称')
        if name in names:
            raise ValueError('常点名称重复：' + name)
        names.add(name)
        out['meals'].append({'name': name, 'items': _foods(meal.get('items'), '常点内容')})
    aliases = data.get('aliases', [])
    if not isinstance(aliases, list) or len(aliases) > LIMIT['aliases']:
        raise ValueError(f'个人说法最多 {LIMIT["aliases"]} 条')
    labels = set()
    for alias in aliases:
        label = _text((alias or {}).get('label'), '说法')
        if label in labels:
            raise ValueError('说法重复：' + label)
        labels.add(label)
        out['aliases'].append({'label': label, 'foods': _foods(alias.get('foods'), '说法对应的餐品')})
    return out


class Preferences:
    def __init__(self, root):
        self.store = LocalState(root)

    def read(self, account):
        try:
            return validate(self.store.read(account))
        except ValueError:
            # A hand-edited file that no longer validates is shown empty, never half-applied.
            return copy.deepcopy(EMPTY)

    def write(self, account, data):
        clean = validate(data)
        self.store.write(account, clean)
        return clean

    def merge(self, account, change):
        """Add to the saved library without dropping what is already there; the same label or name is replaced."""
        current = self.read(account)
        for key in ('avoid', 'taste'):
            current[key] = current[key] + list(change.get(key, []))
        current['defaults'] = {**current['defaults'], **change.get('defaults', {})}
        for key, name in (('meals', 'name'), ('aliases', 'label')):
            new = {x[name] for x in change.get(key, [])}
            current[key] = [x for x in current[key] if x[name] not in new] + list(change.get(key, []))
        return self.write(account, current)


def expand(foods, prefs):
    """Replace a saved meal name or personal alias with its foods. The user's own words win over shared slang."""
    table = {m['name']: m['items'] for m in prefs['meals']}
    table.update({a['label']: a['foods'] for a in prefs['aliases']})
    out, used = [], []
    for food in foods:
        phrase = food.get('phrase', '').strip()
        if phrase in table:
            used.append(phrase)
            out.extend({'phrase': item, 'quantity': None, 'spec': []} for item in table[phrase])
        else:
            out.append(food)
    return out, used


def fill(interp, prefs, said):
    """Slots the user did not mention, taken from saved preferences. Returns the interpretation and what was filled.

    Only empty slots are filled; anything the host passed from the user's words stays. Fulfillment is filled
    only when the user's words did not express one (`said`).
    """
    d, filled = prefs['defaults'], {}
    interp = dict(interp)
    if not interp.get('fulfillment') and not said and d.get('fulfillment'):
        interp['fulfillment'] = d['fulfillment']
        interp['trust_fulfillment'] = True
        filled['fulfillment'] = d['fulfillment']
    if (interp.get('fulfillment') or said) in ('pickup', 'drive') and not interp.get('landmark') and d.get('landmark'):
        interp['landmark'] = d['landmark']
        filled['landmark'] = d['landmark']
        if not interp.get('city') and d.get('city'):
            interp['city'] = d['city']
            filled['city'] = d['city']
    if interp.get('take_way') is None and d.get('take_way'):
        interp['take_way'] = d['take_way']
        filled['take_way'] = d['take_way']
    if interp.get('tableware') is None and 'tableware' in d:
        interp['tableware'] = d['tableware']
        filled['tableware'] = d['tableware']
    if prefs['avoid']:
        interp['avoid'] = list(dict.fromkeys(list(interp.get('avoid') or []) + prefs['avoid']))
        filled['avoid'] = prefs['avoid']
    if interp.get('foods'):
        foods, used = expand([f if isinstance(f, dict) else f.model_dump() for f in interp['foods']], prefs)
        if used:
            interp['foods'] = foods
            filled['foods'] = used
    return interp, filled
