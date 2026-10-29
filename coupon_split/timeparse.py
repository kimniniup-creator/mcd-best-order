"""Chinese reservation expressions; incomplete or conflicting facts fail closed."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NUM = r'[零〇一二两三四五六七八九十\d]+'
DATE = rf'今天|明天|后天|(?:本|这|下)?(?:周|星期)[一二三四五六日天]|(?:{NUM}月)?{NUM}(?:日|号(?![楼院室门栋幢座层单弄路巷]))'
PERIOD = r'早上|上午|中午|下午|晚上|傍晚|夜里|凌晨|明早|今晚'
CLOCK = rf'(?P<h>{NUM})(?:点|时)(?:(?P<part>半|一刻|三刻)|(?P<m>{NUM})分?)?|(?P<colon>\d{{1,2}}[:：]\d{{2}})'
TEMPORAL = re.compile(rf'(?:{DATE})|(?:{PERIOD})|(?:{CLOCK})|预约|现在取|现在送|立即取|立即送')
UNSUPPORTED = r'大后天|下下(?:周|星期)|[上下]个?月|明年|明日|明晚|后晚|周末|月底|月末|小时|分钟后|秒|晚点|过会儿?|等下|等会儿?|稍后|一会儿?'


NO_RESERVATION = re.compile(r'不(?:用|需要|必)?预约|无需预约|免预约|不要预约')


class TimeClarification(ValueError):
    pass


def number(value):
    if value.isdigit(): return int(value)
    value = value.replace('两', '二').replace('〇', '零')
    digits = '零一二三四五六七八九'
    if '十' in value:
        a, b = value.split('十')
        return (digits.index(a) if a else 1) * 10 + (digits.index(b) if b else 0)
    return int(''.join(str(digits.index(c)) for c in value))


def strip_time(text):
    text=re.sub(rf'(?:{CLOCK})(?:取|送达)?', '', text)
    return TEMPORAL.sub('', text)


def parse_time(text, now=None):
    """Return (mentioned, timestamp/null); never silently downgrade a reservation."""
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    text = re.sub(r'\s+', '', text)
    clocks = list(re.finditer(CLOCK, text))
    dates = list(re.finditer(DATE, text))
    periods = re.findall(PERIOD, text)
    immediate = bool(re.search(r'现在(?:取|送)|立即(?:取|送)|马上(?:取|送)', text)) or bool(NO_RESERVATION.search(text) and not clocks and not dates)
    unsupported=bool(re.search(UNSUPPORTED,text))
    mentioned = bool(clocks or dates or periods or '预约' in text or immediate or unsupported)
    if not mentioned: return False, None
    def ask():
        raise TimeClarification('预约日期和时间不完整或有歧义（完整说法如“明天中午12点半”）；上午还是下午、哪一天需要明确。')
    if unsupported:ask()
    if immediate:
        if clocks or dates or periods or ('预约' in text and not re.search(NO_RESERVATION.pattern,text)): ask()
        return True, None
    if periods and not clocks and not dates and '预约' not in text:
        return False, None
    if len(clocks) != 1 or len(dates) > 1 or len(periods) > 1: ask()
    if re.search(r'左右|大概|或者|或|到\d|至\d|多点|点多|点钟后|点以后|前', text): ask()
    period = periods[0] if periods else ''
    day = now.date()
    if period == '明早': day += timedelta(days=1)
    if dates:
        raw = dates[0][0]
        if raw in ('今天', '明天', '后天'):
            day += timedelta(days={'今天':0, '明天':1, '后天':2}[raw])
            if period == '明早': ask()
        elif '周' in raw or '星期' in raw:
            weekday = '一二三四五六日'.index(raw[-1].replace('天', '日'))
            if raw.startswith('下'): delta = 7 - day.weekday() + weekday
            elif raw.startswith(('本', '这')): delta = weekday - day.weekday()
            else: delta = (weekday - day.weekday()) % 7
            day += timedelta(days=delta)
        else:
            parts = re.fullmatch(rf'(?:(?P<month>{NUM})月)?(?P<day>{NUM})(?:日|号)', raw)
            try: day = day.replace(month=number(parts['month']) if parts['month'] else day.month, day=number(parts['day']))
            except ValueError: ask()
    clock = clocks[0]
    if clock['colon']:
        hour, minute = map(int, re.split('[:：]', clock['colon']))
    else:
        hour = number(clock['h'])
        minute = {'半':30, '一刻':15, '三刻':45}.get(clock['part'], number(clock['m']) if clock['m'] else 0)
    if hour > 23 or minute > 59: ask()
    if period and not clock['colon'] and hour > 12: ask()
    if period in ('下午', '晚上', '傍晚', '夜里', '今晚'):
        if 1 <= hour < 12: hour += 12
        elif hour == 12 and period != '下午': ask()
        if hour < 12: ask()
        if period in ('晚上','夜里','今晚') and hour<18: ask()
        if period=='傍晚' and not 16<=hour<=20: ask()
        if period=='下午' and hour>18: ask()
    elif period == '中午':
        if hour in (1, 2): hour += 12
        if not 11 <= hour <= 14: ask()
    elif period in ('早上', '上午', '明早', '凌晨'):
        if period == '凌晨' and hour == 12: hour = 0
        if hour >= 12: ask()
    elif not clock['colon'] and hour <= 12:
        ask()
    scheduled=datetime.combine(day,datetime.min.time()).replace(hour=hour,minute=minute,tzinfo=now.tzinfo)
    if scheduled <= now:
        raise TimeClarification('这个时间已经过去了；指明天时需要明确说“明天”。')
    return True, day.isoformat() + f' {hour:02d}:{minute:02d}'
