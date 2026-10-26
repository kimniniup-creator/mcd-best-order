"""Server-side confirmation bound to the selected quote, before any order write.

Decision order of place_orders (first match wins; each step is tested):

1. Withdrawal first. If the user's latest words carry a refusal or a retraction
   (别下 / 不要了 / 取消 / 等等 ...) nothing else is looked at: not an earlier
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
   processed) -> the person's click is the authorisation. The wording only has to
   be neutral: a question, or a different amount, is refused and the page stays valid.
4. Plan is quoted, no live confirmation -> the wording must be an explicit
   confirmation of this exact amount, then the person confirms on the host dialog or page.

After the person clicks "确认下单" the order is created at once, by the process that
serves the page; there is no waiting window in which a later "别下单" could be skipped.
A "别下单" arriving after that is rule 1b/1c: the order is withdrawn.
"""
from decimal import Decimal
import re

AFFIRM = re.compile(r'(?:确认下单|确认|同意下单|就这样下单)(?:\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?)?[。！!\s]*')

# --- negation:begin (hooks/order_context.py carries a byte-identical copy; a test keeps them equal)
# Judged by meaning, not by the mere presence of a word:
#  * STRONG phrases are a refusal wherever they stand (别下单 / 不要了 / 先不 / 不确认 ...).
#  * WEAK words (取消 / 等等 / 想想 / 算了 ...) are a refusal when the message does not open with a confirmation, or when
#    one of its clauses is nothing but that word ("确认下单，等等" is still "wait"). Negated ("不取消这单"), quoted
#    ("“取消”按钮我不点") or embedded ("想想没问题") uses inside a confirmation sentence are not.
STRONG = re.compile(
    r'先别|别(?:下|买|点|订|了)|不(?:要了|下(?![去来班雨载])|买|订|点了|用了(?!解)|必了|想(?:要|下|买)?了|确认|同意|行|对)|不要[。！!\s]*$|先不|暂时不|暂不'
    r'|\b(?:no(?!\s*(?:problem|worries))|nope|don\'?t|do\s*not)\b', re.I)
WEAK_WORDS = ('取消', '撤回', '撤销', '反悔', '改主意', '算了', '等一下', '等一会', '等会', '等等', '稍等', '先等', '别急', '别等', '别动',
              '停一下', '停下', '再看看', '考虑一下', '想想', 'cancel', 'stop', 'wait', 'never mind', 'nevermind', 'hold on')
WEAK = re.compile('|'.join(re.escape(w) for w in sorted(WEAK_WORDS, key=len, reverse=True)), re.I)
NEGATED_WEAK = re.compile(r'(?:不|没|别|无需|不用|不必|不会|不想|不打算|不要|没有|don.?t\s+|do\s+not\s+|not\s+)(?:去|要|想|再|用)?(?:' + WEAK.pattern + ')|(?:' + WEAK.pattern + r')(?:按钮|键|页|选项|功能)', re.I)
QUOTED = re.compile(r'[“"‘「『《【\[（(][^”"’」』》】\]）)]{1,12}[”"’」』》】\]）)]')
CONFIRM_LEAD = re.compile(r'^(?:[嗯好对是的那我就请麻烦，, ]*)(?:确认|同意|可以|行|没问题|下单|就这样|ok|yes|confirm|go ahead|proceed)', re.I)
FILLER = re.compile(r'(?:好吧|我们|让我|帮我|给我|能不能|可不可以|不如|一会儿|会儿|一会|一下|再说|看看|订单|这单|这个|下单|还是|我|先|那|就|请|麻烦|你|嗯|唉|哎|啊|哦|呀|吧|了|呢|吗|单|再|要|想|得|可以)')
CLAUSES = re.compile(r'(?<!\d)[.](?!\d)|[，,。！!？?；;、\s]+')


def is_negative(text):
    """True when the words refuse, retract or postpone the order. When in doubt, do not order."""
    text = str(text or '').strip()
    if not text:
        return False
    plain = NEGATED_WEAK.sub(' ', QUOTED.sub(' ', text))
    if STRONG.search(plain):
        return True
    if not WEAK.search(plain):
        return False
    if not CONFIRM_LEAD.match(text):
        return True
    for clause in CLAUSES.split(plain):
        if clause and WEAK.search(clause) and not FILLER.sub('', WEAK.sub('#', clause)).strip('#'):
            return True
    return False
# --- negation:end

QUESTION = re.compile(r'[?？]\s*$|(?:吗|呢|么)[。！!\s]*$|是不是|多少钱|为什么|怎么|能不能|可不可以')


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
