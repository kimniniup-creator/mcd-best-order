"""Server-side confirmation bound to the selected quote, before any order write.

Decision order of place_orders (first match wins; each step is tested):

1. Withdrawal first. If the user's latest words carry a refusal or a retraction
   (别下 / 不要了 / 取消 / 等等 ...) nothing else is looked at: not an earlier
   approval, not a trusted host, not a click.
   a. plan still quoted    -> revoke every live confirmation of this plan, create nothing.
   b. order already created -> cancel it right away and report the outcome
      (cancelled / still verifying / already paid).
2. Plan is no longer quoted (the click on the confirmation page already placed
   the order) -> idempotent: return the existing result, wording is not re-checked.
3. Plan is quoted and a live confirmation exists (link shown, or click being
   processed) -> the person's click is the authorisation. The wording only has to
   be neutral: a question, or a different amount, is refused and the page stays valid.
4. Plan is quoted, no live confirmation -> the wording must be an explicit
   confirmation of this exact amount, then the person confirms on the host dialog or page.

After the person clicks "确认下单" the order is created at once, by the process that
serves the page; there is no waiting window in which a later "别下单" could be skipped.
A "别下单" arriving after that is rule 1b: the order is withdrawn.
"""
from decimal import Decimal
import re

AFFIRM = re.compile(r'(?:确认下单|确认|同意下单|就这样下单)(?:\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?)?[。！!\s]*')

NEGATIVE = re.compile(
    r'先别|别(?:下|买|点|订|了|急|等|动)|不(?:要了|下|买|订|点了|用了|必了|想(?:要|下|买)?了|确认|同意|行|对)|不要[。！!\s]*$|先不|暂时不|暂不'
    r'|取消|算了|撤回|撤销|反悔|改主意|等等|等一下|等会|稍等|先等|停一下|停下|想想|考虑一下|再看看'
    r'|\b(?:no|nope|cancel|stop|wait|never\s*mind|hold\s*on|don\'?t|do\s*not)\b', re.I)

QUESTION = re.compile(r'[?？]\s*$|(?:吗|呢|么)[。！!\s]*$|是不是|多少钱|为什么|怎么|能不能|可不可以')


def is_negative(text):
    """True when the words refuse or retract the order. Deliberately broad: when in doubt, do not order."""
    return bool(NEGATIVE.search(str(text or '')))


def is_question(text):
    return bool(QUESTION.search(str(text or '').strip()))


def validate_confirmation(original,price,extra=''):
    expected=f'确认下单 ¥{price/100:.2f}'
    if price>20000:
        # Never allow a host-invented auxiliary string to upgrade a bare 确认.
        if original!=expected or (extra and extra!=original):
            raise ValueError('金额超过 ¥200，请用户完整回复：'+expected)
        return
    match=AFFIRM.fullmatch(original)
    if not match:raise ValueError('请先在对话中明确确认下单；逐字传用户最后一句原文，例如：'+expected)
    if match[1] is not None and Decimal(match[1])*100!=price:
        raise ValueError('确认金额与本次报价不一致，请用户核对后回复：'+expected)
    if extra and extra!=original:raise ValueError('附加确认必须与用户最后一句原文相同')


def validate_follow_up(original,price):
    """Words said while a confirmation page is live: the click decides, the words must not contradict it."""
    if is_question(original):
        raise ValueError('用户在提问，还不是确认。先回答问题；确认页仍然有效，用户点“确认下单”才会下单。')
    match=AFFIRM.fullmatch(str(original or ''))
    if match and match[1] is not None and Decimal(match[1])*100!=price:
        raise ValueError('确认金额与本次报价不一致，请用户核对后回复：'+f'确认下单 ¥{price/100:.2f}')
