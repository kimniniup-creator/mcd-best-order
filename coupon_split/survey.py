"""Product-form survey woven into ordering (PRD R20, docs/specs/form-survey.md).

After an order is built, at most one question a day replaces the 👍/👎 line: "would you use form N?". Nine forms, in an order
shuffled once per person, then one question for the favourite. Answers live next to feedback in feedback.sqlite3.
Nothing is asked unless COUPON_SPLIT_SURVEY_DOC_URL names the Feishu page with the pictures, so the public build never asks.
"""
import os
import random
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from coupon_split.diagnostics import scrub_text

FORMS = [('form-1', '独立 App'), ('form-2', '微信小程序'), ('form-3', '网页版'), ('form-4', '在 IDE / Agent 里直接显示的界面'),
         ('form-5', '虚拟引导助手'), ('form-6', '主动提醒'), ('form-7', '团队拼单 / 帮同事一起点'), ('form-8', '飞书机器人'),
         ('form-9', '“老样子”一键复购')]
NAMES = dict(FORMS)
FAVORITE = 'favorite'
FORM_CHOICES = {'1': '会用', '2': '看情况', '3': '不会'}
FAVORITE_CHOICES = {str(i): name for i, (_, name) in enumerate(FORMS, 1)}
SPECIAL = {'skip', 'stop', 'note_only'}
NOTE_CHARS = 200
DOC_PREFIX = 'https://'
DOC_HOST = '.feishu.cn/'


def doc_url():
    """The Feishu page with the pictures; only https://<tenant>.feishu.cn/... is accepted, otherwise the survey is off."""
    url = os.environ.get('COUPON_SPLIT_SURVEY_DOC_URL', '').strip()
    if not url.startswith(DOC_PREFIX) or DOC_HOST not in url:return ''
    host = url[len(DOC_PREFIX):].split('/', 1)[0]
    return url if host.endswith('.feishu.cn') and ' ' not in url else ''


def today(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')


def question_text(question_id, url):
    if question_id == FAVORITE:
        return '这 9 种里你最想要哪个？回编号 1–9：' + ' / '.join(f'{n} {name}' for n, name in FAVORITE_CHOICES.items())
    n = question_id.split('-')[1]
    return f'如果有「{NAMES[question_id]}」，你会用来点餐吗？（看形态 {n}：{url}）回 1 会用 / 2 看情况 / 3 不会，也可以直接说想法'


def question_view(question_id, url):
    choices = list(FAVORITE_CHOICES if question_id == FAVORITE else FORM_CHOICES)
    return dict(question_id=question_id, text=question_text(question_id, url), choices=choices)


class SurveyStore:
    def __init__(self, db):
        self.db = sqlite3.connect(db)
        self.db.execute("CREATE TABLE IF NOT EXISTS survey_state (owner TEXT PRIMARY KEY, order_seed INTEGER NOT NULL, opted_out INTEGER NOT NULL,"
                        " last_asked_day TEXT NOT NULL, pending_question TEXT NOT NULL, pending_plan_id TEXT NOT NULL, skipped TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS survey_answers (owner TEXT NOT NULL, question_id TEXT NOT NULL, choice TEXT NOT NULL,"
                        " note TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (owner, question_id))")
        self.db.commit()

    def state(self, owner):
        row = self.db.execute("SELECT order_seed, opted_out, last_asked_day, pending_question, pending_plan_id, skipped FROM survey_state WHERE owner=?",
                              (owner,)).fetchone()
        if row is None:return None
        return dict(order_seed=row[0], opted_out=bool(row[1]), last_asked_day=row[2], pending_question=row[3], pending_plan_id=row[4],
                    skipped=[q for q in row[5].split(',') if q])

    def save_state(self, owner, s):
        self.db.execute("INSERT OR REPLACE INTO survey_state VALUES (?,?,?,?,?,?,?)",
                        (owner, s['order_seed'], int(s['opted_out']), s['last_asked_day'], s['pending_question'], s['pending_plan_id'], ','.join(s['skipped'])))
        self.db.commit()

    def answered(self, owner):
        return {q for (q,) in self.db.execute("SELECT question_id FROM survey_answers WHERE owner=?", (owner,))}

    def queue(self, s, answered):
        """Questions still to ask, in this person's order: unanswered forms first, skipped ones last, then the favourite."""
        forms = [q for q, _ in FORMS]
        random.Random(s['order_seed']).shuffle(forms)
        fresh = [q for q in forms if q not in answered and q not in s['skipped']]
        skipped = [q for q in s['skipped'] if q not in answered]
        if fresh or skipped:return fresh + skipped
        return [] if FAVORITE in answered else [FAVORITE]

    def next_question(self, owner, plan_id, now=None):
        """The question to show under this order's payment links, or None (spec §4)."""
        url = doc_url()
        if not url:return None
        s = self.state(owner) or dict(order_seed=random.SystemRandom().randrange(2**31), opted_out=False, last_asked_day='',
                                      pending_question='', pending_plan_id='', skipped=[])
        if s['opted_out']:return None
        if s['pending_plan_id'] == plan_id and s['pending_question']:return question_view(s['pending_question'], url)
        day = today(now)
        if s['last_asked_day'] == day:return None
        queue = self.queue(s, self.answered(owner))
        if not queue:return None
        s.update(pending_question=queue[0], pending_plan_id=plan_id, last_asked_day=day)
        self.save_state(owner, s)
        return question_view(queue[0], url)

    def answer(self, owner, question_id, choice, note=''):
        """Record one answer to the question this person was last asked. Returns the stored row."""
        s = self.state(owner)
        if not s or s['opted_out'] or s['pending_question'] != question_id:raise ValueError('这道题没有问过你')
        valid = FAVORITE_CHOICES if question_id == FAVORITE else FORM_CHOICES
        if choice not in valid and choice not in SPECIAL:
            raise ValueError('这道题只收 ' + ' / '.join(valid) + '，或 skip / stop / note_only')
        note = scrub_text(note)[:NOTE_CHARS]
        if choice == 'note_only' and not note:raise ValueError('note_only 需要带上用户的意见')
        if choice == 'stop':
            s['opted_out'] = True
        elif choice == 'skip':
            s['skipped'] = [q for q in s['skipped'] if q != question_id] + [question_id]
        else:
            self.db.execute("INSERT OR REPLACE INTO survey_answers VALUES (?,?,?,?,?)", (owner, question_id, choice, note, time.time()))
            s['skipped'] = [q for q in s['skipped'] if q != question_id]
        s['pending_question'] = ''
        self.save_state(owner, s)
        return dict(question_id=question_id, choice=choice, note=note)

    def distribution(self, since=0):
        """Answer counts per question and choice, favourite votes, and how many people opted out."""
        forms = {q: {} for q, _ in FORMS}
        favorite = {}
        for q, c in self.db.execute("SELECT question_id, choice FROM survey_answers WHERE created>=?", (since,)):
            if q == FAVORITE:favorite[c] = favorite.get(c, 0) + 1
            elif q in forms:forms[q][c] = forms[q].get(c, 0) + 1
        stopped = self.db.execute("SELECT COUNT(*) FROM survey_state WHERE opted_out=1").fetchone()[0]
        return dict(forms=forms, favorite=favorite, stopped=stopped)


def distribution_text(d):
    asked = [(q, c) for q, c in d['forms'].items() if c]
    if not asked and not d['favorite'] and not d['stopped']:return ''
    parts = [f"{NAMES[q]} " + ' / '.join(f"{label} {c.get(k, 0)}" for k, label in FORM_CHOICES.items()) for q, c in asked]
    line = '形态调研：' + ('；'.join(parts) if parts else '还没有回答')
    if d['favorite']:
        votes = sorted(d['favorite'].items(), key=lambda kv: -kv[1])
        line += '；最想要：' + '、'.join(f"{FAVORITE_CHOICES[k]} {v}" for k, v in votes if k in FAVORITE_CHOICES)
    if d['stopped']:line += f"；不再参与 {d['stopped']} 人"
    return line + '。'


def lark_message(row):
    if row['choice'] in SPECIAL - {'note_only'}:return None
    q = row['question_id']
    head = '形态调研 最想要：' + FAVORITE_CHOICES.get(row['choice'], '') if q == FAVORITE else \
        f"形态调研 {NAMES[q]}：" + FORM_CHOICES.get(row['choice'], '只留了意见')
    return {'msg_type': 'text', 'content': {'text': head + (f"\n{row['note']}" if row['note'] else '')}}
