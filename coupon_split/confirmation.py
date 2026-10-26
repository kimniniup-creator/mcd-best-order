"""Server-side confirmation bound to the selected quote, before any order write.

Decision order of place_orders (first match wins; each step is tested):

1. Withdrawal first. If the user's latest words contain a word of the withdraw / pause list
   (取消 撤回 撤单 不要 别下 别买 算了 先不 不下了 不吃了 退掉 退单 等等 等一下 再想想 改一下 换一下 no cancel stop) anywhere,
   nothing else is looked at (a sentence with both such a word and "确认" is a withdrawal): not an earlier
   approval, not a trusted host, not a click.
   a. plan still quoted    -> revoke every live confirmation of this plan, create nothing. This includes a
      click that is being processed (goods being re-checked): its record is revoked atomically, and the last
      gate right before create-order (ConfirmationStore.commit) then refuses.
   b. create-order may be in flight (record "creating") -> the record is flagged; the moment the order exists it
      is withdrawn, before the attempt counts as done.
   c. order already created -> cancel it right away and report the outcome
      (cancelled / still verifying / already paid).
2. Plan is no longer quoted (the click on the confirmation page already placed
   the order) -> idempotent: return the existing result, wording is not re-checked.
3. Plan is quoted and a live confirmation exists (link shown, or click being
   processed) -> the person's click is the authorisation, the words are not. They only must not
   contradict it: a question, or a standard confirmation of another amount, is refused and the page stays valid.
4. Plan is quoted, no live confirmation -> the words must equal a standard confirmation of this exact amount
   (whitelist; anything else is only a hint, nothing happens), then the person confirms on the host dialog or page.
Whitelist: after normalisation exactly 确认 / 确认下单 / 同意下单 / 就这样下单, optionally followed by the amount, with at
most a short polite prefix (好的 嗯 对 ...) and suffix (谢谢 吧 ...). Above ¥200 the amount is mandatory.

After the person clicks "确认下单" the order is created at once, by the process that
serves the page; there is no waiting window in which a later "别下单" could be skipped.
A "别下单" arriving after that is rule 1b/1c: the order is withdrawn.
"""
from decimal import Decimal
import re

# --- negation:begin (hooks/order_context.py carries a byte-identical copy; a test keeps them equal)
# Whitelist, not guesswork. Decision order, each step final:
#  1. WITHDRAW: any withdraw / pause word anywhere in the message -> withdraw (even when "确认" is also there).
#  2. CONFIRM: after normalisation (NFKC, lower case, punctuation and spaces dropped, amount canonical) the message equals
#     a standard confirmation, with at most a short polite prefix / suffix. Nothing else counts as a confirmation.
#  3. NONE: anything else -> only a hint, no action.
import re
import unicodedata
from decimal import Decimal

WITHDRAW_ZH = ('取消', '撤回', '撤单', '不要', '别下', '别买', '算了', '先不', '不下了', '不吃了', '退掉', '退单', '等等', '等一下', '再想想', '改一下', '换一下',
               # supplement to the owner's list (QA 0.7.4 retraction samples that the list alone left unjudged)
               '撤销', '反悔', '改主意', '停止', '停一下', '稍等', '别急', '先等', '先别')
WITHDRAW_EN = re.compile(r'(?<![a-z])(?:no|cancel|stop)(?![a-z])')
CONFIRM_FULL = re.compile(
    r'(?:好的|好|嗯嗯|嗯|对|是的|ok|okay)*(?:确认下单|确认|同意下单|就这样下单)(?:¥?(\d+(?:\.\d{1,2})?)(?:元|块)?)?(?:谢谢你|谢谢|多谢|谢了|麻烦了|辛苦了|吧)*')
CONFIRM_HINT = "要下单请直接回复'确认下单'（金额超过 ¥200 时回复'确认下单 ¥X'）。"


def normalize(text):
    """NFKC (full width -> half width), lower case, drop spaces and punctuation but keep the amount's ¥ and decimal point."""
    text = unicodedata.normalize('NFKC', str(text or '')).lower()
    text = re.sub(r'[^\w.¥]+|_', '', text)
    return re.sub(r'(?<!\d)\.|\.(?!\d)', '', text)


def classify(text):
    """('withdraw', None) | ('confirm', cents or None) | ('none', None). When in doubt it is not a confirmation."""
    raw = unicodedata.normalize('NFKC', str(text or '')).lower()
    plain = normalize(text)
    if any(w in plain for w in WITHDRAW_ZH) or WITHDRAW_EN.search(raw):
        return 'withdraw', None
    if re.search('[?？]', raw):  # a question is never a confirmation
        return 'none', None
    match = CONFIRM_FULL.fullmatch(plain)
    if not match:
        return 'none', None
    return 'confirm', (None if match[1] is None else int((Decimal(match[1]) * 100).to_integral_value()))


def is_negative(text):
    """True when the words withdraw or pause the order."""
    return classify(text)[0] == 'withdraw'
# --- negation:end


def is_question(text):
    return bool(re.search(r'[?？]\s*$|(?:吗|呢|么)[。！!\s]*$|是不是|多少钱|为什么|怎么|能不能|可不可以', str(text or '').strip()))


def validate_confirmation(original,price,extra=''):
    expected=f'确认下单 ¥{price/100:.2f}'
    kind,cents=classify(original)
    if kind!='confirm':raise ValueError(CONFIRM_HINT)
    if price>20000:
        # Never allow a host-invented auxiliary string to upgrade a bare 确认.
        if cents is None:raise ValueError('金额超过 ¥200，请用户完整回复：'+expected)
        if cents!=price:raise ValueError('确认金额与本次报价不一致，请用户核对后回复：'+expected)
    elif cents is not None and cents!=price:
        raise ValueError('确认金额与本次报价不一致，请用户核对后回复：'+expected)
    if extra and extra!=original:raise ValueError('附加确认必须与用户最后一句原文相同')


def validate_follow_up(original,price):
    """Words said while a confirmation page is live: the click is the authorisation, so the words only must not
    contradict it. A question is refused, a standard confirmation of another amount is refused (the page stays valid);
    a withdraw word never gets here (it is handled first). Words alone never create an order."""
    kind,cents=classify(original)
    if kind=='none' and is_question(original):
        raise ValueError('用户在提问，还不是确认。先回答问题；确认页仍然有效，用户点“确认下单”才会下单。'+CONFIRM_HINT)
    if kind=='confirm' and cents is not None and cents!=price:
        raise ValueError('确认金额与本次报价不一致，请用户核对后回复：'+f'确认下单 ¥{price/100:.2f}')
