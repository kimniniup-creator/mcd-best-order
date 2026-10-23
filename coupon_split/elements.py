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
    takeWayCode: str | None = None
    takeWayName: str | None = None
    reservationDate: str | None = Field(default=None, validation_alias=AliasChoices('reservationDate', 'reservation_time', 'reservationTime'))
    needTableware: StrictBool | None = None
    sources: dict[str, Literal['utterance', 'last', 'default', 'account', 'nearest']] = Field(default_factory=dict)

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
            if len(set(times))>1:raise ValueError('预约别名冲突，请只传正确键 reservationDate')
        return value


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


def normalize_elements(value):
    if isinstance(value, OrderElements):
        value = value.model_dump(exclude_unset=True)
    value = dict(value or {})
    for alias, correct in ALIASES.items():
        if alias in value:
            if correct in value and value[correct] != value[alias]:
                raise ValueError(f'{alias} 与正确键 {correct} 冲突，请只传 {correct}')
            value[correct] = value.pop(alias)
    unknown = value.keys() - OrderElements.model_fields.keys()
    if unknown:
        raise ValueError('未知 elements 键：' + '、'.join(sorted(unknown)) + '；正确键名：' + '、'.join(OrderElements.model_fields))
    sources = value.get('sources', {})
    if set(sources) - (OrderElements.model_fields.keys() - {'sources', 'delivery_address'} | {'store_code'}):
        raise ValueError('sources 请使用正确的 elements 键名或 store_code')
    return OrderElements.model_validate(value).model_dump(exclude_unset=True)


def merge_elements(previous, incoming):
    incoming = normalize_elements(incoming)
    reusable = {'fulfillment', 'addressId', 'takeWayCode'}
    result = {k: v for k, v in previous.items() if k in reusable and v}
    sources = {k: previous.get('sources', {}).get(k) if previous.get('sources', {}).get(k) in {'default','nearest','account'} else 'last' for k in result}
    result.update(reservationDate=None, needTableware=False)
    sources.update(reservationDate='default', needTableware='default')
    for k, v in incoming.items():
        if k != 'sources':
            result[k] = v
            sources[k] = incoming.get('sources', {}).get(k, 'utterance')
    sources.update(incoming.get('sources', {}))
    result['sources'] = sources
    return result
