"""Local order diagnostics: an append-only redacted log and a shareable export.

Nothing here records tokens, full order ids, coupon codes or payment links.
The local log keeps McDonald's wording after redaction; the shareable export is
built from an allow-list of fields (see below) and never copies free text.
"""
import glob
import json
import os
from pathlib import Path
import re
import sqlite3
import time

MAX_LOG_BYTES = 256 * 1024
URL = re.compile(r'https?://\S+')
LONG_NUMBER = re.compile(r'\d{9,}')
LONG_ID = re.compile(r'[A-Za-z0-9_\-\.]{24,}')
SECRET_KEY = re.compile(r'bearer\s+\S+|(?:token|secret|api[_-]?key|authorization)\s*[:=]\s*(?:bearer\s+)?\S+', re.I)


def scrub_text(value):
    text = str(value or '')
    text = URL.sub('<url>', text)
    text = SECRET_KEY.sub('<redacted>', text)
    text = LONG_NUMBER.sub('<num>', text)
    return LONG_ID.sub('<id>', text)[:400]


def safe_trace(value):
    text = str(value or '')
    return text[:64] if re.fullmatch(r'[A-Za-z0-9\-_.]{4,80}', text) else ''


def attempt_entry(op, duration_ms, outcome, exc=None, data=None):
    """One redacted create/cancel attempt; outcome is ok / failed / unknown."""
    entry = {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'op': op, 'duration_ms': duration_ms, 'outcome': outcome}
    if exc is not None:
        entry.update(exception_type=type(exc).__name__, kind=getattr(exc, 'kind', ''),
                     http_status=getattr(exc, 'http_status', None), upstream_code=getattr(exc, 'code', None),
                     upstream_message=scrub_text(getattr(exc, 'upstream_message', '') or ''),
                     trace_id=safe_trace(getattr(exc, 'trace_id', '')))
    elif isinstance(data, dict):
        entry.update(http_status=200, upstream_code=data.get('code', 200), trace_id=safe_trace(data.get('traceId', '')))
    return entry


def write_log(directory, plan_id, entry):
    """Append one JSON line; never raises into the order flow."""
    try:
        path = Path(directory) / 'orders.log'
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            old = path.with_name('orders.log.1')
            if old.exists():
                old.unlink()
            path.replace(old)
        line = dict(entry, plan=plan_id[:8])
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------- shareable export: allow-list only
# The export is built from named fields whose values are checked against a tight shape
# (enum, number, short code). Anything not named here is not exported, whatever it looks like:
# short order ids, coupon codes, addresses, store names/addresses, phones, names, URLs and tokens
# cannot get through by being phrased in a way a pattern did not anticipate. Free text from
# McDonald's is reduced to tags from a fixed vocabulary; the raw wording stays on this machine.

STATES = {'quoted', 'placing', 'verifying', 'awaiting_payment', 'rollback_pending', 'place_failed', 'cancelled', 'awaiting_coupon_return', 'paid', 'partially_completed'}
FAILURE_KINDS = {'business', 'manual', 'not_created', 'auth', 'transport', 'busy', 'error', 'timeout', 'disconnect', 'http5xx', 'http4xx', 'parse', 'noorder'}
OPS = {'create-order', 'cancel-order'}
OUTCOMES = {'ok', 'failed', 'unknown'}
FULFILLMENT = {'pickup', 'drive', 'delivery'}
ORDER_STATUS_TEXT = {'待支付', '已支付', '已撤回', '已撤回（模拟）', '已撤回，退券待核验', '模拟 · 未下单', '已取消', '已完成', '制作中', '待取餐'}
CANCEL_ERRORS = {'取消未确认，等待重试'}
REASON_TAGS = (
    (re.compile('太火爆|限流|too many', re.I), 'rate-limited'), (re.compile('售罄|卖完'), 'sold-out'), (re.compile('暂停营业|休息|打烊|未营业'), 'store-closed'),
    (re.compile('选配'), 'combo-selection'), (re.compile('优惠券|券'), 'coupon'), (re.compile('库存'), 'stock'), (re.compile('不可售|停售|未售'), 'unavailable'),
    (re.compile('授权|登录|token|unauthorized', re.I), 'authorization'), (re.compile('超时|timeout', re.I), 'timeout'), (re.compile('金额|价格'), 'amount'),
    (re.compile('地址'), 'address-related'), (re.compile('配送|外送'), 'delivery'), (re.compile('时间|预约'), 'time-slot'),
)
CODE_KEYS = {'productCode', 'quantity', 'roundList', 'comboItemList', 'roundId', 'comboItems', 'childItems', 'items'}
SAFE_CODE = re.compile(r'[A-Za-z0-9_\-]{1,16}')
TRACE = re.compile(r'(?=.*[a-f])[0-9a-f\-]{16,40}')
EXC_NAME = re.compile(r'[A-Z][A-Za-z]{2,40}(?:Error|Exception|Timeout|Exit)')
LOG_FILES = ('plans.sqlite3', 'orders.log', 'orders.log.1', 'bridge.stderr', 'bridge.json', 'confirmations.sqlite3', 'carts.sqlite3')


def reason_tags(text):
    value = str(text or '')
    return sorted({tag for pattern, tag in REASON_TAGS if pattern.search(value)})


def _enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _number(value, low=0, high=10**9):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and low <= value <= high:
        return value
    if isinstance(value, str) and re.fullmatch(r'\d{1,9}', value) and low <= int(value) <= high:
        return int(value)
    return None


def _money(value):
    if isinstance(value, str) and re.fullmatch(r'\d{1,6}(\.\d{1,2})?', value):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10**7:
        return value
    return None


def _stamp(value):
    return value if isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', value) else None


def _bool(value):
    return value if isinstance(value, bool) else None


def _code(value):
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and SAFE_CODE.fullmatch(str(value)) else None


def _code_tree(value, depth=0):
    """Only product codes and quantities, in the original nesting; nothing else survives."""
    if depth > 6:
        return None
    if isinstance(value, list):
        return [x for x in (_code_tree(v, depth + 1) for v in value[:60]) if x not in (None, {}, [])]
    if isinstance(value, dict):
        out = {}
        for key in CODE_KEYS & value.keys():
            item = value[key]
            if key == 'quantity':
                out[key] = _number(item, 0, 999)
            elif key in {'productCode', 'roundId'}:
                out[key] = _code(item)
            else:
                out[key] = _code_tree(item, depth + 1)
        return {k: v for k, v in out.items() if v not in (None, [], {})}
    return None


def clean_request(request):
    request = request if isinstance(request, dict) else {}
    return {'beType': _number(request.get('beType'), 0, 9), 'needTableware': _bool(request.get('needTableware')), 'withOrder': _bool(request.get('withOrder')),
            'takeWayCode': _code(request.get('takeWayCode')), 'items': _code_tree(request.get('items')) or []}


def clean_failure(failure):
    failure = failure if isinstance(failure, dict) else {}
    return {'kind': _enum(failure.get('kind'), FAILURE_KINDS), 'code': _number(failure.get('code'), 0, 10**7), 'busy': _bool(failure.get('busy')),
            'reason_tags': reason_tags(str(failure.get('message', '')) + ' ' + str(failure.get('upstream_message', '')))}


def clean_attempt(entry):
    entry = entry if isinstance(entry, dict) else {}
    trace = str(entry.get('trace_id') or '')
    exc = str(entry.get('exception_type') or '')
    plan = str(entry.get('plan') or '')
    return {'at': _stamp(entry.get('at')), 'op': _enum(entry.get('op'), OPS), 'duration_ms': _number(entry.get('duration_ms'), 0, 10**7), 'outcome': _enum(entry.get('outcome'), OUTCOMES),
            'exception_type': exc if EXC_NAME.fullmatch(exc) else None, 'kind': _enum(entry.get('kind'), FAILURE_KINDS), 'http_status': _number(entry.get('http_status'), 100, 599),
            'upstream_code': _number(entry.get('upstream_code'), 0, 10**7), 'reason_tags': reason_tags(entry.get('upstream_message')),
            'trace_id': trace if TRACE.fullmatch(trace) else None, 'plan': plan if re.fullmatch(r'[0-9a-f]{8}', plan) else None}


def clean_plan(plan):
    selected = plan.get('selected') or {}
    elements = plan.get('order_elements') or {}
    created = plan.get('created')
    return {'state': _enum(plan.get('state'), STATES), 'dry_run': _bool(plan.get('dry_run')),
            'failure': clean_failure(plan.get('failure')) if plan.get('failure') else None,
            'late_found': _bool(plan.get('late_found')),
            'cancel_reason_tags': reason_tags(plan.get('cancel_reason')),
            'created': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created)) if isinstance(created, (int, float)) and not isinstance(created, bool) else None,
            'price': _number(selected.get('price'), 0, 10**7),
            'order_elements': {'fulfillment': _enum(elements.get('fulfillment'), FULFILLMENT), 'needTableware': _bool(elements.get('needTableware')),
                               'takeWayCode': _code(elements.get('takeWayCode'))},
            'requests': [clean_request(o.get('request')) for o in selected.get('orders', [])[:10] if isinstance(o, dict)],
            'orders': [{'status': _number(o.get('status'), 0, 999), 'orderStatus': _enum(o.get('orderStatus'), ORDER_STATUS_TEXT), 'realTotalAmount': _money(o.get('realTotalAmount')),
                        'price': _number(o.get('price'), 0, 10**7), 'cancel_error': _enum(o.get('cancel_error'), CANCEL_ERRORS)} for o in plan.get('orders', [])[:10] if isinstance(o, dict)],
            'verify_checks': [n for n in (_number(v, 0, 600) for v in (plan.get('verify_checks') or [])[:20]) if n is not None],
            'place_attempts': [clean_attempt(e) for e in (plan.get('place_attempts') or [])[-20:]]}


def log_summary(text):
    """Counts and exception class names only; the raw lines are never copied."""
    names = {}
    for name in EXC_NAME.findall(text):
        names[name] = names.get(name, 0) + 1
    return {'lines': len(text.splitlines()), 'create_order_mentions': text.count('create-order'), 'place_orders_mentions': text.count('place_orders'),
            'error_mentions': len(re.findall('rror', text)), 'exception_types': dict(sorted(names.items())[:20])}


def export(home, out_dir=None):
    """Write an allow-listed JSON the user can read and send. Returns its path."""
    home = Path(home)
    result = {'exported': time.strftime('%Y-%m-%d %H:%M:%S'), 'home_exists': home.exists(),
              'files': sorted(p.name for p in home.glob('*') if p.name in LOG_FILES) if home.exists() else [], 'plans': []}
    db = home / 'plans.sqlite3'
    if db.exists():
        con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
        try:
            rows = [json.loads(body) for (body,) in con.execute('SELECT body FROM plans')]
        finally:
            con.close()
        placed = [p for p in rows if p.get('state') not in (None, 'quoted')] or rows
        placed.sort(key=lambda p: p.get('placing_started') or p.get('created') or 0, reverse=True)
        result['plans'] = [clean_plan(p) for p in placed[:5]]
    log = home / 'orders.log'
    if log.exists():
        tail = []
        for line in log.read_text(encoding='utf-8', errors='replace').splitlines()[-80:]:
            if line.startswith('{'):
                try:
                    tail.append(clean_attempt(json.loads(line)))
                except ValueError:
                    pass
        result['orders_log_tail'] = tail
    stderr = home / 'bridge.stderr'
    if stderr.exists():
        result['bridge_stderr_summary'] = log_summary(stderr.read_text(encoding='utf-8', errors='replace')[-6000:])
    logs = []
    for base in (os.environ.get('LOCALAPPDATA', ''), str(Path.home() / 'Library/Caches'), str(Path.home() / '.cache')):
        if not base:
            continue
        found = glob.glob(os.path.join(base, 'claude-cli-nodejs', '**', 'mcp-logs-*coupon*', '*'), recursive=True)
        for f in sorted(set(found), key=os.path.getmtime)[-5:]:
            try:
                if time.time() - os.path.getmtime(f) < 7 * 86400:
                    lines = [l for l in Path(f).read_text(encoding='utf-8', errors='replace').splitlines() if re.search(r'create-order|place_orders|rror', l)]
                    logs.append(log_summary('\n'.join(lines[-40:])))
            except OSError:
                pass
    result['claude_mcp_log_summaries'] = logs[-5:]
    target = Path(out_dir or os.environ.get('DIAG_OUT') or Path.home()) / ('mcd-order-diagnosis-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    return target
