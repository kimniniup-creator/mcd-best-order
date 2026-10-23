import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from coupon_split.optimizer import quote, parse_cart, key, normalized, composition_key, specification_coverage, validate_quantities


def cart_signature(items):
    parsed=parse_cart(items)
    return key(normalized(items)) if items and isinstance(items[0],dict) else key(dict(parsed))


def owner(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Service:
    def __init__(self, upstream, db=".runtime/plans.sqlite3"):
        self.upstream = upstream
        self.db = sqlite3.connect(db)
        self.db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL, body TEXT NOT NULL)")
        self.locks = {}
        self.inflight_quotes = {}
        self.quote_seconds = {}
        self.quoting_accounts = set()

    def quote_status(self, token, store, items, allow_combos, context=None):
        account = owner(token)
        signature = (account, store, cart_signature(items), allow_combos, key(context or {}))
        pending = [sig for sig, task in self.inflight_quotes.items() if sig[0] == account and not task.done()]
        ahead = pending.index(signature) if signature in pending else 0
        wait = self.upstream.capacity_wait(token) if account not in self.quoting_accounts and hasattr(self.upstream, 'capacity_wait') else 0
        queued = signature in pending and (ahead > 0 or wait > 0)
        estimate = round(wait + ahead * self.quote_seconds.get(account, 5))
        return dict(queued=queued, ahead=ahead, estimated_seconds=max(1, estimate) if queued else 0)

    def save(self, plan):
        self.db.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?)", (plan["plan_id"], plan["owner"], json.dumps(plan, ensure_ascii=False)))
        self.db.commit()

    def load(self, plan_id, token):
        row = self.db.execute("SELECT body FROM plans WHERE id=? AND owner=?", (plan_id, owner(token))).fetchone()
        if not row:
            raise ValueError("方案不存在或不属于此账户")
        return json.loads(row[0])

    async def quote(self, token, store, items, allow_combos, context=None):
        parse_cart(items)
        signature = (owner(token), store, cart_signature(items), allow_combos, key(context or {}))
        task = self.inflight_quotes.get(signature)
        if task is None:
            task = asyncio.create_task(self._quote(token, store, items, allow_combos, context))
            self.inflight_quotes[signature] = task
            def finished(done):
                self.inflight_quotes.pop(signature, None)
                if not done.cancelled():
                    done.exception()  # Retrieve failures even if all waiters disconnected.
            task.add_done_callback(finished)
        return copy.deepcopy(await asyncio.shield(task))

    async def _quote(self, token, store, items, allow_combos, context=None):
        async with self.locks.setdefault("quote:"+owner(token),asyncio.Lock()):
            if hasattr(self.upstream,"ensure_capacity"):
                await self.upstream.ensure_capacity(token)
            started = time.monotonic()
            calls=getattr(self.upstream,"network_calls",0)
            hits=getattr(self.upstream,"cache_hits",0)
            self.quoting_accounts.add(owner(token))
            try:
                if hasattr(self.upstream, "batch"):
                    async with self.upstream.batch(token):
                        plan = await quote(self.upstream, token, store, items, allow_combos, context) if context else await quote(self.upstream, token, store, items, allow_combos)
                else:
                    plan = await quote(self.upstream, token, store, items, allow_combos, context) if context else await quote(self.upstream, token, store, items, allow_combos)
            finally:
                self.quoting_accounts.discard(owner(token))
            plan.update(upstream_calls=getattr(self.upstream,"network_calls",0)-calls,cache_hits=getattr(self.upstream,"cache_hits",0)-hits)
            self.quote_seconds[owner(token)] = time.monotonic() - started
        plan.update(plan_id=uuid.uuid4().hex, payment_key=secrets.token_urlsafe(32), owner=owner(token), confirm_token=secrets.token_urlsafe(32), created=time.time(), state="quoted", orders=[], real_orders_enabled=self.upstream.allow_real, requested_items=copy.deepcopy(items))
        self.save(plan)
        return plan

    async def place(self, token, plan_id, confirm_token, option_id="0"):
        async with self.locks.setdefault("place:"+owner(token),asyncio.Lock()):
            return await self._place(token,plan_id,confirm_token,option_id)

    async def _place(self, token, plan_id, confirm_token, option_id="0"):
        async with self.locks.setdefault(plan_id, asyncio.Lock()):
            plan = self.load(plan_id, token)
            if not hmac.compare_digest(confirm_token, plan["confirm_token"]):
                raise ValueError("确认凭证不匹配")
            if plan["state"] != "quoted":
                return plan  # idempotency: replay never creates a second order
            if time.time() - plan["created"] > 600:
                raise ValueError("报价已过期，请重新算价并确认")
            selected = next((p for p in plan["plans"] if p["option_id"] == option_id), None)
            if not selected:
                raise ValueError("方案选项不存在")
            self.validate_before_place(plan, selected)
            plan.update(state="placing", selected=selected, dry_run=not self.upstream.allow_real)
            if self.upstream.allow_real:
                before = (await self.upstream.call(token,"order-list",{}))["data"]["list"]
                plan.update(before_order_ids=[o["orderId"] for o in before], placing_started=time.time(), pending_requests=selected["orders"])
            self.save(plan)
            async def create(order):
                req = dict(order["request"])
                if self.upstream.allow_real and not plan.get("elements_validated"):
                    raise ValueError("订单要素未完整验证，禁止下单")
                req.update({k:v for k,v in plan.get("order_elements",{}).items() if k in {"takeWayCode","needTableware"} and v is not None})
                if req.get("beType")==2:req.pop("takeWayCode",None)
                if not self.upstream.allow_real:
                    return {"orderId": "dry-"+uuid.uuid4().hex, "orderStatus": "模拟 · 未下单", "status": "10", "realTotalAmount": f"{order['price']/100:.2f}", "expirePayTime": time.time()+900, "payH5Url": None, "price": order["price"], "productList": order["productList"]}
                r = (await self.upstream.call(token, "create-order", req))["data"]
                data = dict(r.get("orderDetail", r.get("order", r)))
                data.update({k:r[k] for k in ["orderId", "payId", "payH5Url"] if k in r})
                data.update(price=order["price"], productList=order["productList"])
                if not data.get("orderId"):
                    raise ValueError("下单结果缺少订单编号，需对账")
                return data
            results = await asyncio.gather(*(create(o) for o in selected["orders"]), return_exceptions=True)
            plan["orders"] = [r for r in results if not isinstance(r, Exception)]
            failures = any(isinstance(r, Exception) for r in results)
            if self.upstream.allow_real:
                plan["pending_requests"] = [o for o,r in zip(selected["orders"],results) if isinstance(r,Exception)]
                plan["unknown_orders"] = bool(plan["pending_requests"])
            from decimal import Decimal
            def amount_matches(order):
                try:
                    return Decimal(str(order.get("realTotalAmount", "-1")))*100 == order["price"]
                except Exception:
                    return False
            failures |= any(not amount_matches(r) for r in plan["orders"])
            plan["state"] = "rollback_pending" if failures else "awaiting_payment"
            self.save(plan)
            if failures:
                await self._cancel(token, plan)
            return plan

    def validate_before_place(self, plan, selected):
        if plan.get('elements_pending') or not plan.get('elements_validated'):
            raise ValueError('订单要素未完整验证，请先选择堂食/外带')
        demand = plan.get('requested_items')
        if not demand or not isinstance(demand[0], dict):
            raise ValueError('缺少原始规格需求，请重新报价')
        evidence = {e['evidence_id']: e for e in plan.get('evidence', [])}
        joined = []
        for order in selected['orders']:
            ev = evidence.get(order.get('evidence_id'))
            if not ev or composition_key(order['request']) != composition_key(ev['request']):
                raise ValueError('下单前复核：商品码、规格或数量与核价依据不一致')
            if not validate_quantities(order['request'], ev['response']['data']):
                raise ValueError('下单前复核：计价数量不一致')
            if order['price'] != ev['response']['data']['price']:
                raise ValueError('下单前复核：金额与核价依据不一致')
            joined.extend(order['request']['items'])
        if specification_coverage(joined) != specification_coverage(demand):
            raise ValueError('下单前复核：商品码、规格或数量与原始需求不一致')

    async def _reconcile(self, token, plan):
        """Only uniquely matched new account orders may be rolled back automatically."""
        from collections import Counter
        from decimal import Decimal
        try:
            rows = (await self.upstream.call(token,"order-list",{}))["data"]["list"]
            excluded = set(plan.get("before_order_ids",[])) | {o["orderId"] for o in plan["orders"]}
            remaining=[]
            for pending in plan.get("pending_requests",[]):
                expected=Counter()
                for i in pending["request"]["items"]:
                    expected[i["productCode"]]+=i["quantity"]
                if pending["request"].get("withOrder"):
                    expected["521234"]+=1
                matches=[]
                for r in rows:
                    if r["orderId"] in excluded or str(r.get("storeCode")) != str(plan["storeCode"]):
                        continue
                    if not plan["placing_started"]-5 <= deadline(r["createTime"]) <= plan["placing_started"]+120:
                        continue
                    if Decimal(str(r.get("realTotalAmount","-1")))*100 != pending["price"]:
                        continue
                    actual=Counter()
                    for i in r.get("orderProductList",[]):
                        actual[i["productCode"]]+=i["quantity"]
                    if actual == expected:
                        matches.append(r)
                if len(matches) != 1:
                    remaining.append(pending)
                    continue
                r=matches[0]
                detail=(await self.upstream.call(token,"query-order",{"orderId":r["orderId"]}))["data"]
                plan["orders"].append(dict(detail,orderId=r["orderId"],price=pending["price"],productList=pending["productList"]))
                excluded.add(r["orderId"])
            plan["pending_requests"]=remaining
            plan["unknown_orders"]=bool(remaining)
        except Exception:
            plan["unknown_orders"]=True
        if plan.get("unknown_orders"):
            plan["reconciliation_notice"]="有下单结果不明的请求，正在按账户订单重试对账；请勿重新下单，需人工核验时保留此方案。"
        self.save(plan)

    async def _cancel(self, token, plan):
        if not plan["dry_run"] and plan.get("unknown_orders"):
            await self._reconcile(token,plan)
        failed = bool(plan.get("unknown_orders"))
        for order in plan["orders"]:
            if order.get("status") == "60":
                continue
            if not plan["dry_run"]:
                try:
                    current = (await self.upstream.call(token, "query-order", {"orderId": order["orderId"]}))["data"]
                    if str(current.get("status")) == "60":
                        order.update(status="60",orderStatus="已撤回，退券待核验")
                        continue
                    if str(current.get("status")) != "10":
                        failed = True
                        order["cancel_error"] = "不是待付订单，需核验处理"
                        continue
                    result = await self.upstream.call(token, "cancel-order", {"orderId": order["orderId"], "cancelReasonCode": 3})
                    if result["data"].get("cancelResult") is not True:
                        raise ValueError("取消未确认")
                    verified=(await self.upstream.call(token,"query-order",{"orderId":order["orderId"]}))["data"]
                    if str(verified.get('status'))!='60':
                        raise ValueError('取消状态尚未核实')
                except Exception:
                    failed = True
                    order["cancel_error"] = "取消未确认，等待重试"
                    continue
            order.update(status="60", orderStatus="已撤回（模拟）" if plan["dry_run"] else "已撤回，退券待核验")
        plan["state"] = "rollback_pending" if failed else "cancelled"
        if not failed and not plan["dry_run"]:
            coupon_ids = {i["couponId"] for o in plan.get("selected",{}).get("orders",[]) for i in o["request"]["items"] if i.get("couponId")}
            if coupon_ids:
                plan["state"] = "awaiting_coupon_return"
                plan["coupon_return_verified"] = False
        self.save(plan)
        return plan

    async def cancel(self, token, plan_id):
        async with self.locks.setdefault(plan_id, asyncio.Lock()):
            plan = self.load(plan_id, token)
            if plan["state"] == "quoted":
                plan.update(state="cancelled", dry_run=not self.upstream.allow_real)
                self.save(plan)
                return plan
            return await self._cancel(token, plan)

    async def status(self, token, plan_id):
        async with self.locks.setdefault(plan_id, asyncio.Lock()):
            plan = self.load(plan_id, token)
            if plan["state"] == "awaiting_coupon_return":
                r=await self.upstream.call(token,"query-store-coupons",{"storeCode":plan["storeCode"],"orderType":1,"beType":1})
                wanted={i["couponId"] for o in plan["selected"]["orders"] for i in o["request"]["items"] if i.get("couponId")}
                available={c["couponId"] for c in r["data"]}
                if wanted <= available:
                    plan.update(state="cancelled",coupon_return_verified=True)
                    self.save(plan)
                return plan
            if plan["state"] == "placing" and not plan.get("dry_run"):
                plan.update(unknown_orders=True,state="rollback_pending")
            if plan["state"] not in {"awaiting_payment", "rollback_pending"}:
                return plan
            if not plan["dry_run"]:
                for order in plan["orders"]:
                    current = (await self.upstream.call(token, "query-order", {"orderId": order["orderId"]}))["data"]
                    order.update({k:current[k] for k in ["status", "orderStatus", "takeCode", "pickupCode", "orderCode", "mealCode", "expirePayTime"] if k in current})
            if plan["state"] == "rollback_pending" or any(deadline(o["expirePayTime"]) <= time.time()+60 and str(o.get("status")) == "10" for o in plan["orders"]):
                for o in plan['orders']:
                    if str(o.get('status'))=='10' and deadline(o['expirePayTime'])<=time.time()+60:o['auto_expired']=True
                return await self._cancel(token, plan)
            if plan["orders"] and all(str(o.get("status")) not in {"10", "60"} for o in plan["orders"]):
                plan["state"] = "paid"
            elif plan['orders'] and all(str(o.get('status'))=='60' for o in plan['orders']):
                plan['state']='cancelled'
            self.save(plan)
            return plan

    async def wait_payment(self, token, plan_id, timeout=60):
        until=time.monotonic()+max(0,min(60,float(timeout)))
        while True:
            plan=await self.status(token,plan_id)
            if plan['state']!='awaiting_payment' or time.monotonic()>=until:
                return plan
            await asyncio.sleep(min(3,max(0,until-time.monotonic())))


def deadline(value):
    if isinstance(value, (int, float)):
        return value
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
