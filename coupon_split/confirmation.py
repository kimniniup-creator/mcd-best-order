"""Server-side confirmation bound to the selected quote, before any order write."""
from decimal import Decimal
import re


def validate_confirmation(original,price,extra=''):
    expected=f'确认下单 ¥{price/100:.2f}'
    if price>20000:
        # Never allow a host-invented auxiliary string to upgrade a bare 确认.
        if original!=expected or (extra and extra!=original):
            raise ValueError('金额超过 ¥200，请用户完整回复：'+expected)
        return
    match=re.fullmatch(r'(?:确认下单|确认|同意下单|就这样下单)(?:\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?)?[。！!\s]*',original)
    if not match:raise ValueError('请先在对话中明确确认下单；逐字传用户最后一句原文，例如：'+expected)
    if match[1] is not None and Decimal(match[1])*100!=price:
        raise ValueError('确认金额与本次报价不一致，请用户核对后回复：'+expected)
    if extra and extra!=original:raise ValueError('附加确认必须与用户最后一句原文相同')
