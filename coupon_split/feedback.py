"""User feedback after an order, and order statistics read from the local plan store.

Feedback is a thumbs-up / thumbs-down the person gives in the conversation, with an optional short note. It is stored next to
plans.sqlite3, and, when COUPON_SPLIT_FEEDBACK_WEBHOOK names a Lark (Feishu) custom-bot webhook, also posted to that group.
Nothing here records tokens, order ids, coupon codes, addresses or payment links; the note is scrubbed the same way as the
diagnostics log.
"""
import os
import sqlite3
import time
from collections import Counter

from coupon_split.diagnostics import scrub_text

RATINGS = {'up': '👍', 'down': '👎'}
NOTE_CHARS = 200
LARK_HOSTS = ('https://open.feishu.cn/open-apis/bot/v2/hook/', 'https://open.larksuite.com/open-apis/bot/v2/hook/')


class FeedbackStore:
    def __init__(self, db):
        self.db = sqlite3.connect(db)
        self.db.execute("CREATE TABLE IF NOT EXISTS feedback (plan_id TEXT NOT NULL, owner TEXT NOT NULL, rating TEXT NOT NULL,"
                        " note TEXT NOT NULL, save_cents INTEGER, pay_cents INTEGER, created REAL NOT NULL,"
                        " PRIMARY KEY (plan_id, owner))")
        self.db.commit()

    def record(self, plan, account, rating, note=''):
        """One rating per order and person; a later rating replaces the earlier one."""
        if rating not in RATINGS:raise ValueError('rating 只能是 up 或 down')
        selected = plan.get('selected') or {}
        row = dict(plan_id=plan['plan_id'], rating=rating, note=scrub_text(note)[:NOTE_CHARS],
                   save_cents=selected.get('savings'), pay_cents=sum(o['price'] for o in plan.get('orders', [])) or selected.get('price'),
                   created=time.time())
        self.db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?,?)",
                        (row['plan_id'], account, rating, row['note'], row['save_cents'], row['pay_cents'], row['created']))
        self.db.commit()
        return row

    def ratings(self, since=0):
        return Counter(r for (r,) in self.db.execute("SELECT rating FROM feedback WHERE created>=?", (since,)))

    def notes(self, since=0, limit=5):
        rows = self.db.execute("SELECT rating, note FROM feedback WHERE created>=? AND note!='' ORDER BY created DESC LIMIT ?", (since, limit))
        return [dict(rating=r, note=n) for r, n in rows]


def webhook_url():
    url = os.environ.get('COUPON_SPLIT_FEEDBACK_WEBHOOK', '').strip()
    return url if url.startswith(LARK_HOSTS) else ''


def lark_message(row):
    saved = f"，省 ¥{row['save_cents']/100:.2f}" if row.get('save_cents') else ''
    paid = f"实付 ¥{row['pay_cents']/100:.2f}" if row.get('pay_cents') else '未建单'
    text = f"点餐反馈 {RATINGS[row['rating']]}　{paid}{saved}" + (f"\n{row['note']}" if row['note'] else '')
    return {'msg_type': 'text', 'content': {'text': text}}


async def post_to_lark(row, message=None):
    """Post one feedback (or a ready message, e.g. a survey answer) to the configured Lark group.
    Returns True when Lark accepted it; never raises into the tool."""
    url = webhook_url()
    if not url:return False
    message = message or lark_message(row)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            reply = await client.post(url, json=message)
        return reply.status_code == 200 and reply.json().get('code', 0) == 0
    except (httpx.HTTPError, ValueError):
        return False


# ---- statistics over the local plan store
CREATED = {'awaiting_payment', 'paid', 'partially_completed', 'cancelled', 'awaiting_coupon_return', 'completed'}


def order_stats(rows, since=0, feedback=None):
    """Counts and money over plans (dicts from the plan store) whose orders were created since `since` (epoch seconds), plus feedback counts."""
    rows = list(rows)
    created = [p for p in rows if p.get('state') in CREATED and p.get('orders') and p.get('created', 0) >= since]
    paid = [p for p in created if p['state'] in {'paid', 'partially_completed', 'completed'}]
    unpaid = [p for p in created if p['state'] in {'cancelled', 'awaiting_coupon_return'}]
    savings = [(p.get('selected') or {}).get('savings', 0) for p in created]
    real = [p for p in created if not p.get('dry_run')]
    stats = dict(quoted=sum(1 for p in rows if p.get('created', 0) >= since and p.get('plans')),
                 created=len(created), real=len(real), paid=len(paid), not_paid=len(unpaid),
                 waiting=sum(1 for p in created if p['state'] == 'awaiting_payment'),
                 split=sum(1 for p in created if len(p['orders']) > 1),
                 buys_card=sum(1 for p in created if (p.get('selected') or {}).get('buys_card')),
                 saved_cents=sum(savings), saved_avg_cents=round(sum(savings) / len(savings)) if savings else 0,
                 saved_max_cents=max(savings, default=0))
    if feedback is not None:
        counts = feedback.ratings(since)
        stats.update(feedback_up=counts['up'], feedback_down=counts['down'], feedback_notes=feedback.notes(since))
    return stats


def stats_text(stats, days):
    money = lambda c: f"¥{c/100:.2f}"
    lines = [f"近 {days} 天：报价 {stats['quoted']} 次，建单 {stats['created']} 个（真实 {stats['real']}），"
             f"已付 {stats['paid']}，没付就取消 {stats['not_paid']}，待付款 {stats['waiting']}。"]
    if stats['created']:
        lines.append(f"共省 {money(stats['saved_cents'])}，平均每单省 {money(stats['saved_avg_cents'])}，最多一单省 {money(stats['saved_max_cents'])}；"
                     f"拆单 {stats['split']} 个，买卡 {stats['buys_card']} 个。")
    if 'feedback_up' in stats:
        lines.append(f"反馈：👍 {stats['feedback_up']}，👎 {stats['feedback_down']}。")
        lines += [f"{RATINGS[n['rating']]} {n['note']}" for n in stats['feedback_notes']]
    if stats.get('survey_text'):lines.append(stats['survey_text'])
    return '\n'.join(lines)
