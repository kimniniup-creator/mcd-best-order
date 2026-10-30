"""Strict public order elements and their provenance."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, AliasChoices, StrictBool, model_validator, field_validator


class OrderElements(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    fulfillment: Literal['pickup', 'drive', 'delivery'] | None = None
    account_name: str | None = None
    account_last4: str | None = None
    addressId: str | None = None
    delivery_address: str | None = None
    takeWayCode: str | None = Field(default=None, description='堂食或外带：传“堂食”或“外带”（服务端按本店报价对上官方代码）；到店自取不传，默认外带')
    takeWayName: str | None = None
    reservationDate: str | None = Field(default=None, validation_alias=AliasChoices('reservationDate', 'reservation_time', 'reservationTime'))
    needTableware: StrictBool | None = None
    sources: dict[str, Literal['utterance', 'last', 'default', 'account', 'nearest', 'preference']] = Field(default_factory=dict)

    @field_validator('reservationDate')
    @classmethod
    def explicit_time(cls,value):
        if value is not None and not value.strip():
            raise ValueError('预约时间不能为空；立即单省略 reservationDate 或传 null，不能丢弃预约')
        return value

    @model_validator(mode='before')
    @classmethod
    def guide_keys(cls,value):
        if isinstance(value,dict):
            unknown=value.keys()-cls.model_fields.keys()-{'reservation_time','reservationTime'}
            if unknown:raise ValueError('未知 elements 键：'+'、'.join(sorted(unknown))+'；正确键名：'+'、'.join(cls.model_fields))
            times=[value[k] for k in ('reservationDate','reservation_time','reservationTime') if k in value]
            if len(set(times))>1:raise ValueError('预约别名冲突，只能用正确键 reservationDate')
        return value


# Keys the server derives from other elements (the address text from addressId). They carry no source of their own
# and must never be carried from one turn into the next, or sources would name a key nobody asserted.
DERIVED = frozenset({'delivery_address'})


def without_derived(elements):
    """Copy of an elements dict with derived keys removed from the values and from sources."""
    e = {k: v for k, v in (elements or {}).items() if k not in DERIVED}
    if isinstance(e.get('sources'), dict):
        e['sources'] = {k: v for k, v in e['sources'].items() if k not in DERIVED}
    return e


ALIASES = {'reservation_time': 'reservationDate', 'reservationTime': 'reservationDate'}


def utterance_elements(text):
    """Keep facts from original words, independent of model arguments."""
    import re
    value = {}
    if '外带' in text:value['takeWayCode'] = 'take-in-store'
    elif '堂食' in text:value['takeWayCode'] = 'eat-in'
    if re.search(r'不要餐具|不用餐具|不带餐具', text):value['needTableware'] = False
    elif re.search(r'要餐具|需要餐具|带餐具', text):value['needTableware'] = True
    value['sources'] = {key:'utterance' for key in value}
    return value


DINE_IN_CODES = {'eat-in'}
TAKE_OUT_CODES = {'take-in-store', 'locker-in'}


def _take_way_kind(code, title=''):
    """'dine' for 堂食, 'out' for 外带 (whatever code the store uses for it), else None."""
    code, title = str(code or '').strip().lower(), str(title or '')
    if code in DINE_IN_CODES or '堂食' in title or '堂食' in code:return 'dine'
    if code in TAKE_OUT_CODES or '外带' in title or '外带' in code or code in {'take-out', 'takeout', 'take-away', 'takeaway', '取餐柜'}:return 'out'
    return None


def resolve_take_way(value, options):
    """Map what the host or the user said (堂食 / 外带 / an official code) onto the code this quote's takeWayList really offers.

    The code for 外带 differs by store (take-in-store, or locker-in whose title is still 外带), so the offered list decides.
    Returns None when the quote offers nothing matching."""
    if not value:return None
    options = [o for o in (options or []) if isinstance(o, dict)]
    code_of = lambda o: o.get('code', o.get('takeWayCode'))
    for o in options:
        if str(code_of(o)) == str(value):return code_of(o)
    kind = _take_way_kind(value, value)
    if not kind:return None
    for o in options:
        if _take_way_kind(code_of(o), o.get('title') or o.get('name') or o.get('takeWayName')) == kind:return code_of(o)
    return None


def normalize_elements(value):
    if isinstance(value, OrderElements):
        value = value.model_dump(exclude_unset=True)
    value = dict(value or {})
    for alias, correct in ALIASES.items():
        if alias in value:
            if correct in value and value[correct] != value[alias]:
                raise ValueError(f'{alias} 与正确键 {correct} 冲突，只能用 {correct}')
            value[correct] = value.pop(alias)
    unknown = value.keys() - OrderElements.model_fields.keys()
    if unknown:
        raise ValueError('未知 elements 键：' + '、'.join(sorted(unknown)) + '；正确键名：' + '、'.join(OrderElements.model_fields))
    if isinstance(value.get('sources'), dict):  # a derived key has no source; naming one is harmless, never an error
        value['sources'] = {k: v for k, v in value['sources'].items() if k not in DERIVED}
    sources = value.get('sources', {})
    bad = set(sources) - (OrderElements.model_fields.keys() - {'sources'} - DERIVED | {'store_code'})
    if bad:
        raise ValueError('sources 只认 elements 的键名或 store_code；不认识：' + '、'.join(sorted(bad)))
    return OrderElements.model_validate(value).model_dump(exclude_unset=True)


def merge_elements(previous, incoming):
    incoming = normalize_elements(incoming)
    reusable = {'fulfillment', 'addressId', 'takeWayCode'}
    result = {k: v for k, v in previous.items() if k in reusable and v}
    sources = {k: previous.get('sources', {}).get(k) if previous.get('sources', {}).get(k) in {'default','nearest','account','preference'} else 'last' for k in result}
    result.update(reservationDate=None, needTableware=False)
    sources.update(reservationDate='default', needTableware='default')
    for k, v in incoming.items():
        if k != 'sources':
            result[k] = v
            if k not in DERIVED:  # derived from addressId; it has no source of its own
                sources[k] = incoming.get('sources', {}).get(k, 'utterance')
    sources.update(incoming.get('sources', {}))
    result['sources'] = {k: v for k, v in sources.items() if k not in DERIVED}
    return result
