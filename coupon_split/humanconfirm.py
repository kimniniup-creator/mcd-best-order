"""Server-issued, human-completed order confirmation.

Hosts without an approval dialog (DeepSeek Harness, most IDE agents) let the model call
place_orders directly, and the model fills user_confirmation itself. So the last gate is
not text the model sends: the server asks the person, either through MCP elicitation or
through a local confirmation page, and only then creates the order.

Records live in a small SQLite file so the stdio process (which runs place_orders) and
the page bridge process (which serves the click) see the same state.
"""
import json
import secrets
import sqlite3
import time
from pathlib import Path

TTL_SECONDS = 600
# A host that blocks tools behind its own un-bypassable approval and that sends this clientInfo name.
# Claude Code marks place_orders anthropic/requiresUserInteraction; even bypassPermissions
# cannot skip it (verified in r19 by running the tool headless: "requires user interaction").
TRUSTED_CLIENTS = ('claude-code',)


class ConfirmationStore:
    def __init__(self, path):
        self.path = str(path)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS confirmations (key TEXT PRIMARY KEY, body TEXT NOT NULL)')

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def _save(self, record):
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO confirmations VALUES (?,?)', (record['key'], json.dumps(record, ensure_ascii=False)))
        return record

    def get(self, key):
        with self._db() as db:
            row = db.execute('SELECT body FROM confirmations WHERE key=?', (key,)).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        if time.time() - record['created'] > TTL_SECONDS and record['state'] in {'pending', 'approved'}:
            record['state'] = 'expired'
            self._save(record)
        return record

    def create(self, owner, plan_id, option_id, take_way, price, summary):
        record = dict(key=secrets.token_urlsafe(32), owner=owner, plan_id=plan_id, option_id=option_id,
                      take_way=take_way or '', price=price, summary=summary, state='pending', created=time.time())
        return self._save(record)

    def find(self, owner, plan_id, option_id, take_way, price):
        """Newest live record for exactly this order (same plan, option, pickup way and price)."""
        with self._db() as db:
            rows = [json.loads(body) for (body,) in db.execute('SELECT body FROM confirmations')]
        rows = [r for r in rows if (r['owner'], r['plan_id'], r['option_id'], r['take_way'], r['price']) == (owner, plan_id, option_id, take_way or '', price)]
        for record in sorted(rows, key=lambda r: r['created'], reverse=True):
            current = self.get(record['key'])
            if current['state'] in {'pending', 'approved', 'declined'}:
                return current
        return None

    def find_any(self, owner, plan_id, option_id, price):
        """A live pending/approved record for this plan, whatever the pickup way."""
        with self._db() as db:
            rows = [json.loads(body) for (body,) in db.execute('SELECT body FROM confirmations')]
        for record in sorted(rows, key=lambda r: r['created'], reverse=True):
            if (record['owner'], record['plan_id'], record['option_id'], record['price']) == (owner, plan_id, option_id, price):
                current = self.get(record['key'])
                if current['state'] in {'pending', 'approved'}:
                    return current
        return None

    def set_state(self, key, state, only_from=('pending',)):
        record = self.get(key)
        if not record or record['state'] not in only_from:
            return record
        record['state'] = state
        record[state + '_at'] = time.time()
        return self._save(record)


def client_name(ctx):
    try:
        return (ctx.request_context.session.client_params.clientInfo.name or '').lower()
    except Exception:
        return ''


def trusted_host(ctx):
    """True only when the host itself provably demands a person's approval for this tool."""
    return ctx is not None and any(client_name(ctx).startswith(name) for name in TRUSTED_CLIENTS)


def supports_elicitation(ctx):
    try:
        from mcp import types
        return bool(ctx.request_context.session.check_client_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())))
    except Exception:
        return False


def order_summary(plan, selected, take_way_label=''):
    """What the person is shown: the shop, what is bought, and the exact amount."""
    way = {'pickup': '到店自取', 'drive': '得来速', 'delivery': '麦乐送'}.get((plan.get('order_elements') or {}).get('fulfillment'), '到店自取')
    lines = []
    for order in selected['orders']:
        for row in order['productList']:
            if row.get('productCode') == '521234':
                lines.append({'name': '【卡费】' + row['productName'], 'quantity': row['quantity'], 'amount_yuan': f"{row['subtotal']/100:.2f}"})
            else:
                lines.append({'name': row['productName'], 'quantity': row['quantity'], 'amount_yuan': f"{row['subtotal']/100:.2f}"})
    count = len(selected['orders'])
    return {'store': plan.get('storeName') or '所选门店', 'way': way, 'take_way': take_way_label, 'lines': lines,
            'order_count': count, 'price_yuan': f"{selected['price']/100:.2f}", 'real': bool(plan.get('real_orders_enabled')),
            'payments_note': f'需依次支付 {count} 笔' if count > 1 else '付款 1 次'}


def elicitation_message(summary):
    items = '、'.join(f"{r['name']} ×{r['quantity']}" for r in summary['lines'])
    mode = '' if summary['real'] else '（模拟模式，不会产生真实订单）'
    return (f"确认下单吗？{summary['store']} · {summary['way']}\n{items}\n实付 ¥{summary['price_yuan']}，{summary['payments_note']}{mode}。\n"
            '接受后会在麦当劳创建待付款订单，付款仍由你本人在手机上完成。')
