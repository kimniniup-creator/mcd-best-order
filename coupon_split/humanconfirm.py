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

    def _rows(self, owner, plan_id):
        with self._db() as db:
            rows = [json.loads(body) for (body,) in db.execute('SELECT body FROM confirmations')]
        return sorted((r for r in rows if r['owner'] == owner and r['plan_id'] == plan_id), key=lambda r: r['created'], reverse=True)

    def find(self, owner, plan_id, option_id, take_way, price):
        """Newest live record for exactly this order (same plan, option, pickup way and price)."""
        for record in self._rows(owner, plan_id):
            if (record['option_id'], record['take_way'], record['price']) == (option_id, take_way or '', price):
                current = self.get(record['key'])
                if current['state'] in {'pending', 'approved', 'placing', 'creating', 'declined'}:
                    return current
        return None

    def find_any(self, owner, plan_id, option_id, price):
        """A live pending/approved/placing record for this plan, whatever the pickup way."""
        for record in self._rows(owner, plan_id):
            if (record['option_id'], record['price']) == (option_id, price):
                current = self.get(record['key'])
                if current['state'] in {'pending', 'approved', 'placing', 'creating'}:
                    return current
        return None

    def transition(self, key, state, only_from=('pending',), **extra):
        """Atomic compare-and-set across processes. Returns the new record, or None if the state did not match.

        Two processes (the page service and the stdio server) may both try to act on a click;
        exactly one wins, so one approval can only ever place one order.
        """
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM confirmations WHERE key=?', (key,)).fetchone()
            if not row:
                return None
            record = json.loads(row[0])
            if time.time() - record['created'] > TTL_SECONDS and record['state'] in {'pending', 'approved'}:
                record['state'] = 'expired'
                db.execute('INSERT OR REPLACE INTO confirmations VALUES (?,?)', (key, json.dumps(record, ensure_ascii=False)))
                return None
            if record['state'] not in only_from:
                return None
            record['state'] = state
            record[state + '_at'] = time.time()
            record.update(extra)
            db.execute('INSERT OR REPLACE INTO confirmations VALUES (?,?)', (key, json.dumps(record, ensure_ascii=False)))
            return record

    def set_state(self, key, state, only_from=('pending',)):
        return self.transition(key, state, only_from) or self.get(key)

    def revoke(self, owner, plan_id):
        """The person withdrew in conversation. Returns how many confirmations this stopped (see withdraw)."""
        revoked, flagged = self.withdraw(owner, plan_id)
        return revoked + flagged

    def withdraw(self, owner, plan_id):
        """Atomic withdrawal for every live confirmation of this plan -> (revoked, flagged).

        State machine of one confirmation (every move is one compare-and-set in SQLite):
        pending -> approved|declined|revoked; pending|approved -> placing (a click is being
        processed: re-checking the goods); placing -> creating (committed: create-order is about
        to be, or has been, sent; see commit); creating -> used|failed.
        A withdrawal arriving while the record is pending / approved / declined / placing
        revokes it for good, so commit() later refuses and nothing is created. Arriving while
        it is creating, create-order may already be out: the record only gets the
        withdraw_requested flag, and whoever finishes the creation withdraws the new order.
        """
        revoked = flagged = 0
        for record in self._rows(owner, plan_id):
            if self.transition(record['key'], 'revoked', ('pending', 'approved', 'declined', 'placing')):
                revoked += 1
            elif self.transition(record['key'], 'creating', ('creating',), withdraw_requested=True):
                flagged += 1
        return revoked, flagged

    def commit(self, key):
        """Last gate before create-order is sent. False when the person withdrew meanwhile."""
        if self.transition(key, 'creating', ('placing',)):
            return True
        record = self.get(key)
        return bool(record and record['state'] == 'creating')

    def finish(self, key, state='used', force=False):
        """creating|placing -> used|failed unless a withdrawal arrived; returns False then (the caller withdraws first)."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM confirmations WHERE key=?', (key,)).fetchone()
            if not row:
                return True
            record = json.loads(row[0])
            if record['state'] not in ('creating', 'placing'):
                return True
            if record.get('withdraw_requested') and state == 'used' and not force:
                return False
            record['state'] = state
            record[state + '_at'] = time.time()
            db.execute('INSERT OR REPLACE INTO confirmations VALUES (?,?)', (key, json.dumps(record, ensure_ascii=False)))
            return True

    def creating(self, owner, plan_id):
        return [r for r in self._rows(owner, plan_id) if r['state'] == 'creating']

    def take_failure(self, owner, plan_id):
        """The click happened but the order could not be created: tell the agent once."""
        for record in self._rows(owner, plan_id):
            if record['state'] == 'failed':
                done = self.transition(record['key'], 'failure_reported', ('failed',))
                if done:
                    return done.get('error') or '确认后下单没有成功，订单没有创建。'
        return None


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
