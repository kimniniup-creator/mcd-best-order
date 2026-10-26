"""Local order diagnostics: an append-only redacted log and a shareable export.

Nothing here records tokens, full order ids, coupon codes or payment links.
Upstream messages are kept (they are the diagnosis) after redaction.
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


DROP_KEYS = re.compile(r'token|orderId|payId|coupon|payH5Url|url|account|owner|confirm|payment_key|before_order_ids|evidence|address|phone|mobile', re.I)


def scrub_value(value):
    if isinstance(value, dict):
        return {k: (v if k == 'trace_id' else scrub_value(v)) for k, v in value.items() if not DROP_KEYS.search(k)}
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def export(home, out_dir=None):
    """Write a redacted JSON the user can read and send. Returns its path."""
    home = Path(home)
    result = {'exported': time.strftime('%Y-%m-%d %H:%M:%S'), 'home_exists': home.exists(),
              'files': sorted(p.name for p in home.glob('*')) if home.exists() else [], 'plans': []}
    db = home / 'plans.sqlite3'
    if db.exists():
        con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
        try:
            rows = [json.loads(body) for (body,) in con.execute('SELECT body FROM plans')]
        finally:
            con.close()
        placed = [p for p in rows if p.get('state') not in (None, 'quoted')] or rows
        placed.sort(key=lambda p: p.get('placing_started') or p.get('created') or 0, reverse=True)
        for p in placed[:5]:
            selected = p.get('selected') or {}
            result['plans'].append(scrub_value({
                'state': p.get('state'), 'dry_run': p.get('dry_run'), 'failure': p.get('failure'),
                'late_found': p.get('late_found'), 'cancel_reason': p.get('cancel_reason'),
                'created': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.get('created') or 0)),
                'storeName': p.get('storeName'), 'price': selected.get('price'),
                'order_elements': {k: (p.get('order_elements') or {}).get(k) for k in ('fulfillment', 'takeWayCode', 'needTableware')},
                'requests': [o.get('request') for o in selected.get('orders', [])],
                'orders': [{k: o.get(k) for k in ('status', 'orderStatus', 'realTotalAmount', 'price', 'cancel_error')} for o in p.get('orders', [])],
                'verify_checks': p.get('verify_checks'), 'place_attempts': p.get('place_attempts'),
            }))
    for name in ('orders.log', 'bridge.stderr'):
        path = home / name
        if path.exists():
            result[name.replace('.', '_') + '_tail'] = scrub_text(path.read_text(encoding='utf-8', errors='replace')[-6000:]) if name == 'bridge.stderr' else [
                scrub_value(json.loads(line)) for line in path.read_text(encoding='utf-8', errors='replace').splitlines()[-80:] if line.startswith('{')]
    logs = []
    for base in (os.environ.get('LOCALAPPDATA', ''), str(Path.home() / 'Library/Caches'), str(Path.home() / '.cache')):
        if not base:
            continue
        found = glob.glob(os.path.join(base, 'claude-cli-nodejs', '**', 'mcp-logs-*coupon*', '*'), recursive=True)
        for f in sorted(set(found), key=os.path.getmtime)[-5:]:
            try:
                if time.time() - os.path.getmtime(f) < 7 * 86400:
                    lines = [l for l in Path(f).read_text(encoding='utf-8', errors='replace').splitlines() if re.search(r'create-order|place_orders|rror', l)]
                    logs.append({'file': Path(f).name, 'lines': [scrub_text(l) for l in lines[-40:]]})
            except OSError:
                pass
    result['claude_mcp_logs'] = logs[-5:]
    target = Path(out_dir or os.environ.get('DIAG_OUT') or Path.home()) / ('mcd-order-diagnosis-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    return target
