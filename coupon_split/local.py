"""Account-local credentials and private conversation state."""
import json
import os
import sqlite3
import time
import copy
from collections.abc import MutableMapping
from pathlib import Path


def state_dir():
    root = Path(os.environ.get('COUPON_SPLIT_HOME', Path.home()/'.coupon-split'))
    root.mkdir(parents=True, exist_ok=True)
    return root


def load_token(prefer_saved=False):
    if not prefer_saved and os.environ.get('MCD_MCP_TOKEN'):
        return os.environ['MCD_MCP_TOKEN']
    try:
        import keyring
        value = keyring.get_password('coupon-split', 'upstream')
        if value:
            return value
    except Exception:
        pass
    if os.name=='nt':return os.environ.get('MCD_MCP_TOKEN','')
    file = state_dir()/'credential'
    return file.read_text(encoding='utf-8') if file.exists() else os.environ.get('MCD_MCP_TOKEN','')


def save_token(value):
    try:
        import keyring
        keyring.set_password('coupon-split', 'upstream', value)
        return '系统凭据管理器'
    except Exception:
        if os.name == 'nt':
            raise ValueError('Windows 凭据管理器不可用，请修复 keyring 后重试') from None
        file = state_dir()/'credential'
        fd = os.open(file, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        os.chmod(file, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
        return '仅本人可读的本机文件'


class LocalState:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, account):
        file = self.root/(account+'.json')
        return json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}

    def write(self, account, data):
        file = self.root/(account+'.json')
        import uuid
        temp=self.root/(account+'.'+uuid.uuid4().hex+'.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False),encoding='utf-8')
        if os.name!='nt':temp.chmod(0o600)
        os.replace(temp,file)
        if os.name != 'nt':
            file.chmod(0o600)


class SavedCart(dict):
    def __init__(self, store, sid, value):
        super().__init__(value);self.store=store;self.sid=sid;self.snapshot=copy.deepcopy(value)

    def update(self, *args, **kwargs):
        changes={k:v for k,v in self.items() if k not in self.snapshot or v!=self.snapshot[k]}
        changes.update(dict(*args,**kwargs))
        fresh=self.store.patch(self.sid,changes)
        super().clear();super().update(fresh)
        self.snapshot=copy.deepcopy(fresh)

    def __setitem__(self, key, value):
        self.update({key:value})


class CartStore(MutableMapping):
    """Shared across MCP rounds and the local page service; bearer IDs expire."""
    def __init__(self, file, ttl=24*60*60):
        self.file=Path(file);self.file.parent.mkdir(parents=True,exist_ok=True);self.ttl=ttl
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS carts (sid TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL, body TEXT NOT NULL)')
        if os.name!='nt':self.file.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.file,timeout=10)

    def __getitem__(self,sid):
        with self.connect() as db:
            row=db.execute('SELECT body,expires FROM carts WHERE sid=?',(sid,)).fetchone()
            if row and row[1]<=time.time():
                db.execute('DELETE FROM carts WHERE sid=?',(sid,));row=None
        if not row:raise KeyError(sid)
        return SavedCart(self,sid,json.loads(row[0]))

    def __setitem__(self,sid,value):
        with self.connect() as db:
            old=db.execute('SELECT owner,expires FROM carts WHERE sid=?',(sid,)).fetchone()
            if old and old[0]!=value['owner']:raise ValueError('购物车账户不一致')
            expires=old[1] if old else time.time()+self.ttl
            db.execute('INSERT OR REPLACE INTO carts VALUES (?,?,?,?)',(sid,value['owner'],expires,json.dumps(value,ensure_ascii=False)))

    def __delitem__(self,sid):
        with self.connect() as db:db.execute('DELETE FROM carts WHERE sid=?',(sid,))

    def patch(self,sid,changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT owner,body,expires FROM carts WHERE sid=?',(sid,)).fetchone()
            if not row or row[2]<=time.time():raise KeyError(sid)
            body=json.loads(row[1]);body.update(changes)
            if body['owner']!=row[0]:raise ValueError('购物车账户不一致')
            db.execute('UPDATE carts SET body=? WHERE sid=?',(json.dumps(body,ensure_ascii=False),sid))
            return body

    def __iter__(self):
        with self.connect() as db:rows=db.execute('SELECT sid FROM carts WHERE expires>?',(time.time(),)).fetchall()
        return iter(r[0] for r in rows)

    def __len__(self):return sum(1 for _ in self)


def reservation_slots(options):
    import re
    from datetime import datetime, timedelta
    slots=[]
    for option in options:
        date=option.get('date','')
        for start,end in re.findall(r'(\d{2}:\d{2})(?:至|至|-) *(\d{2}:\d{2})',option.get('reservationOptionText','')):
            point=datetime.fromisoformat(date+' '+start)
            last=datetime.fromisoformat(date+' '+end)
            while point<=last:
                slots.append(point.strftime('%Y-%m-%d %H:%M'))
                point+=timedelta(minutes=1)
    return slots


def order_context(store, elements, require_take=True):
    from coupon_split.elements import normalize_elements
    elements = normalize_elements(elements)
    mode = elements.get('fulfillment')
    missing = [k for k in ('fulfillment', 'account_name', 'account_last4') if not elements.get(k)]
    if require_take and mode in ('pickup','drive') and not elements.get('takeWayCode'):
        missing.append('takeWayCode')
    if mode == 'delivery' and not elements.get('addressId'):
        missing.append('addressId')
    if mode not in ('pickup','drive','delivery'):
        missing.append('有效履约方式')
    if missing:
        labels={'fulfillment':'取餐或配送方式','account_name':'账户信息','account_last4':'账户信息','takeWayCode':'堂食或外带','addressId':'配送地址','有效履约方式':'取餐或配送方式'}
        raise ValueError('只需补充：'+ '、'.join(dict.fromkeys(labels[k] for k in missing)))
    if len(str(elements['account_last4']))!=4:
        raise ValueError('账户号后四位格式无效')
    be_type = {'pickup':1,'drive':5,'delivery':2}[mode]
    context = {'beType':be_type, 'orderType':2 if mode=='delivery' else 1,
               'needTableware':bool(elements.get('needTableware',False))}
    if be_type != 1:
        if not store.get('beCode'):
            raise ValueError('这家门店没有这种取餐方式（缺 beCode）')
        context['beCode'] = store['beCode']
    date = elements.get('reservationDate')
    if date:
        # Fail closed: only exact official selectable timestamps are accepted.
        def values(value):
            if isinstance(value,str): yield value
            elif isinstance(value,list):
                for v in value: yield from values(v)
            elif isinstance(value,dict):
                for v in value.values(): yield from values(v)
        if date not in set(values(store.get('reservationTimeOptions',[]))) | set(reservation_slots(store.get('reservationTimeOptions',[]))):
            from coupon_split.presentation import human_time
            options=store.get('reservationTimeOptions',[])
            ranges=[human_time(o['date']+' 00:00').split(' ')[0]+' '+o['reservationOptionText'] for o in options if o.get('date') and o.get('reservationOptionText')]
            if not ranges:
                ranges=[human_time(v) for v in values(options) if isinstance(v,str) and len(v)==16 and v[10]==' ']
            raise ValueError('这个时间暂时不能预约。'+('可选时段：'+'；'.join(ranges) if ranges else '这家店暂未开放预约。'))
        context['reservationDate'] = date
    return context


AMAP_SERVICE = ('coupon-split', 'amap')


def _env_file_value(name):
    """One key from the user's own ~/.coupon-split/.env. Only optional, non-ordering keys are read from it."""
    file = state_dir()/'.env'
    if not file.exists():
        return ''
    from dotenv import dotenv_values
    return (dotenv_values(file).get(name) or '').strip()


def load_amap_key():
    """The user's own AMap key: environment, then the system credential manager, then ~/.coupon-split/.env."""
    value = os.environ.get('AMAP_KEY', '').strip()
    if value:
        return value
    try:
        import keyring
        value = (keyring.get_password(*AMAP_SERVICE) or '').strip()
        if value:
            return value
    except Exception:
        pass
    file = state_dir()/'amap-key'
    if os.name != 'nt' and file.exists():
        return file.read_text(encoding='utf-8').strip()
    return _env_file_value('AMAP_KEY')


def save_amap_key(value):
    try:
        import keyring
        keyring.set_password(*AMAP_SERVICE, value)
        return '系统凭据管理器'
    except Exception:
        if os.name == 'nt':
            raise ValueError('Windows 凭据管理器不可用，请修复 keyring 后重试') from None
        file = state_dir()/'amap-key'
        fd = os.open(file, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        os.chmod(file, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
        return '仅本人可读的本机文件'


def forget_amap_key():
    try:
        import keyring
        from keyring.errors import PasswordDeleteError
        try:
            keyring.delete_password(*AMAP_SERVICE)
        except PasswordDeleteError:
            pass
    except Exception:
        pass
    file = state_dir()/'amap-key'
    if file.exists():
        file.unlink()
