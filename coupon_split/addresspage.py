"""Local, owner-bound address requests. Full phone numbers are never persisted."""
import hashlib
import json
import re
import secrets
import sqlite3
import time

TTL_SECONDS = 900
FIELDS = {'city': 80, 'address': 200, 'addressDetail': 100, 'contactName': 40, 'phone': 11, 'gender': 2}


def redact(value):
    if isinstance(value, str):
        return re.sub(r'1[0-9]{10}', lambda m: m[0][:3]+'****'+m[0][-4:], value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def validate(values):
    clean = {}
    for name, limit in FIELDS.items():
        value = values.get(name, '')
        if not isinstance(value, str):
            raise ValueError('请填写完整的地址字段')
        value = value.strip()
        if (not value and name != 'gender') or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError('请填写完整的地址字段，且不要超过长度限制')
        if value:
            clean[name] = value
    if not re.fullmatch(r'1[0-9]{10}', clean['phone']):
        raise ValueError('手机号须为以 1 开头的 11 位数字')
    if clean.get('gender') not in (None, '先生', '女士'):
        raise ValueError('称呼请选择先生或女士')
    return clean


def rows(data):
    if isinstance(data, dict):
        if data.get('addressId'):
            yield data
        for value in data.values():
            yield from rows(value)
    elif isinstance(data, list):
        for value in data:
            yield from rows(value)


def equivalent(data, address):
    norm = lambda s: re.sub(r'\s+', '', str(s))
    full = norm(address['address'] + address['addressDetail'])
    for row in rows(data):
        if ((norm(row.get('address', '')) == norm(address['address']) and
             norm(row.get('addressDetail', '')) == norm(address['addressDetail'])) or
                full and full in norm(row.get('fullAddress', ''))):
            if row.get('city') and norm(row['city']) != norm(address['city']):
                continue
            return row
    return None


class AddressStore:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS address_requests (key TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS address_attempts (owner TEXT, signature TEXT, PRIMARY KEY(owner,signature))')

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def put(self, db, record):
        db.execute('INSERT OR REPLACE INTO address_requests VALUES (?,?)', (record['key'], json.dumps(record, ensure_ascii=False)))
        return record

    def create(self, owner, session_id='', city='', address=''):
        record = dict(key=secrets.token_urlsafe(32), owner=owner, session_id=session_id,
                      prefill=redact(dict(city=city[:80], address=address[:200])), state='pending', created=time.time())
        with self.db() as db:
            return self.put(db, record)

    def get(self, key, owner=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM address_requests WHERE key=?', (key,)).fetchone()
            if not row:
                return None
            record = json.loads(row[0])
            if owner is not None and record['owner'] != owner:
                return None
            if record['state'] != 'expired' and time.time()-record['created'] > TTL_SECONDS:
                record['state'] = 'expired'
                self.put(db, record)
            return record

    def claim(self, key, owner):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM address_requests WHERE key=?', (key,)).fetchone()
            if not row:
                return None
            record = json.loads(row[0])
            if record['owner'] != owner or record['state'] != 'pending' or time.time()-record['created'] > TTL_SECONDS:
                return None
            record['state'] = 'saving'
            return self.put(db, record)

    def attempt(self, owner, address):
        # Persist before dispatch: an ambiguous network failure must never cause a second write.
        signature = hashlib.sha256(json.dumps(address, sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO address_attempts VALUES (?,?)', (owner, signature)).rowcount == 1

    def finish(self, key, result=None, error=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM address_requests WHERE key=?', (key,)).fetchone()
            record = json.loads(row[0])
            record.update(state='failed' if error else 'saved')
            if error:
                record['error'] = redact(str(error))
            else:
                record['result'] = redact(result)
            return self.put(db, record)

    def live_until(self):
        with self.db() as db:
            records = [json.loads(r[0]) for r in db.execute('SELECT body FROM address_requests')]
        return max((r['created']+TTL_SECONDS+60 for r in records if r['state'] in {'pending', 'saving', 'expired'}), default=0)


async def save_clicked(store, record, token, upstream, values):
    address = validate(values)
    claimed = store.claim(record['key'], record['owner'])
    if not claimed:
        return store.get(record['key'], record['owner'])
    try:
        found = equivalent(await upstream.call(token, 'delivery-query-addresses', {}), address)
        existing = bool(found)
        if not found:
            if not store.attempt(record['owner'], address):
                raise ValueError('该地址已提交过，请在麦当劳 App 核实，暂不重复创建')
            signature = (record['owner'], json.dumps(address, sort_keys=True))
            upstream.confirmed_addresses.add(signature)
            try:
                created = await upstream.call(token, 'delivery-create-address', address)
            finally:
                upstream.confirmed_addresses.discard(signature)
            found = next(rows(created), None)
            if not found:
                found = equivalent(await upstream.call(token, 'delivery-query-addresses', {}), address)
        if not found:
            raise ValueError('地址已提交，暂未查到地址编号，请在麦当劳 App 核实，勿重复保存')
        return store.finish(record['key'], dict(addressId=str(found['addressId']), existing=existing,
                            summary=redact(address['city']+' '+address['address']+' '+address['addressDetail']+' · '+address['phone'])))
    except Exception as exc:
        return store.finish(record['key'], error=str(exc))
