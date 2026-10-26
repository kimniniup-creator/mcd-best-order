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

from pathlib import Path

from coupon_split.optimizer import quote, parse_cart, key, normalized, composition_key, specification_coverage, validate_quantities
from coupon_split.upstream import UpstreamAuthError, UpstreamBusinessError, UpstreamTransportError, BUSY_TEXT
from coupon_split import diagnostics

# After create-order gives no clear answer we ask the account's order list at
# these offsets (seconds). 120 s with a readable list and no new order is final.
VERIFY_OFFSETS = (5, 15, 30, 60, 120)
VERIFY_CONCLUDE = 120
VERIFY_HARD_STOP = 180
# Every verification query is bounded, and the whole verification by the wall clock
# (placing_started + VERIFY_HARD_STOP), never by a count of rounds.
VERIFY_QUERY_TIMEOUT = 10
# While the order list cannot be read, the next try waits this long after the last one: 15 s after the
# first failed read, 30 s after later ones (so roughly 120 / 150 / 180 s) instead of one try per status
# call, which was 113 reads between 120 and 180 s in 0.7.2 and invites McDonald's rate limit.
VERIFY_RETRY_GAPS = (15, 30)


class NotSent(ValueError):
    """Refused locally before create-order was sent: an explicit, harmless failure."""


def classify(exc):
    """failed = McDonald's/local answered clearly; unknown = no usable answer."""
    return 'failed' if isinstance(exc, (NotSent, UpstreamAuthError, UpstreamBusinessError)) else 'unknown'


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
        self.log_dir = Path(db).parent if str(db) not in {':memory:', ''} else None
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

    def _record(self, plan, op, started, exc=None, data=None):
        """Redacted per-attempt diagnostics (plan + orders.log); never raises."""
        outcome = 'ok' if exc is None else classify(exc)
        duration = getattr(exc, 'duration_ms', None) if exc is not None else None
        if duration is None:
            duration = round((time.monotonic() - started) * 1000)
        entry = diagnostics.attempt_entry(op, duration, outcome, exc, data)
        plan.setdefault('place_attempts', []).append(entry)
        del plan['place_attempts'][:-20]
        if self.log_dir:
            diagnostics.write_log(self.log_dir, plan['plan_id'], entry)

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
                    raise NotSent("订单要素未完整验证，禁止下单")
                req.update({k:v for k,v in plan.get("order_elements",{}).items() if k in {"takeWayCode","needTableware"} and v is not None})
                if req.get("beType")==2:req.pop("takeWayCode",None)
                if not self.upstream.allow_real:
                    return {"orderId": "dry-"+uuid.uuid4().hex, "orderStatus": "模拟 · 未下单", "status": "10", "realTotalAmount": f"{order['price']/100:.2f}", "expirePayTime": time.time()+900, "payH5Url": None, "price": order["price"], "productList": order["productList"]}
                started = time.monotonic()
                try:
                    response = await self.upstream.call(token, "create-order", req)
                    r = response["data"]
                    data = dict(r.get("orderDetail", r.get("order", r)))
                    data.update({k:r[k] for k in ["orderId", "payId", "payH5Url"] if k in r})
                    data.update(price=order["price"], productList=order["productList"])
                    if not data.get("orderId"):
                        raise UpstreamTransportError("下单结果缺少订单编号，需核对", kind='noorder')
                except Exception as exc:
                    self._record(plan, 'create-order', started, exc)
                    raise
                self._record(plan, 'create-order', started, None, response)
                return data
            results = await asyncio.gather(*(create(o) for o in selected["orders"]), return_exceptions=True)
            from decimal import Decimal
            def amount_matches(order):
                try:
                    return Decimal(str(order.get("realTotalAmount", "-1")))*100 == order["price"]
                except Exception:
                    return False
            created = [r for r in results if not isinstance(r, Exception)]
            failed = [(o, r) for o, r in zip(selected["orders"], results) if isinstance(r, Exception) and classify(r) == 'failed']
            unknown = [(o, r) for o, r in zip(selected["orders"], results) if isinstance(r, Exception) and classify(r) == 'unknown']
            plan["orders"] = created
            if not self.upstream.allow_real:
                plan["state"] = "awaiting_payment"
                self.save(plan)
                return plan
            plan["pending_requests"] = [o for o, _ in unknown]
            plan["failed_requests"] = [o for o, _ in failed]
            plan["unknown_orders"] = bool(unknown)
            if failed:
                plan["failure"] = failure_info(failed[0][1])
            mismatch = [r for r in created if not amount_matches(r)]
            if unknown:
                # No usable answer: verify against the account's order list on a schedule.
                plan.update(state="verifying", verify_checks=[], verify_errors=0)
                self.save(plan)
                return plan
            if failed or mismatch:
                if mismatch:
                    actual = mismatch[0].get("realTotalAmount")
                    plan["cancel_reason"] = f"麦当劳实收 ¥{actual}，和报价 ¥{mismatch[0]['price']/100:.2f} 不一致，已经帮你撤回，没扣钱。"
                elif created:
                    plan["cancel_reason"] = f"有一笔没下成（{failed[0][1]}），为避免只下成一半，已撤回已下的单，没扣钱。"
                if created:
                    plan["state"] = "rollback_pending"
                    self.save(plan)
                    await self._cancel(token, plan)
                    return plan
                # Nothing was created. One quick look at the account list guards the rare
                # "reported an error but still created it" case; it never blocks the answer.
                plan["pending_requests"] = [o for o, _ in failed]
                await self._reconcile(token, plan)
                await self._conclude(token, plan, 'business')
                return plan
            plan["state"] = "awaiting_payment"
            self.save(plan)
            return plan

    async def _conclude(self, token, plan, kind):
        """Finish verification. Found orders are kept (to pay); anything else ends as a clear failure."""
        remaining = plan.get("pending_requests", [])
        plan["pending_requests"] = []
        plan["unknown_orders"] = False
        plan.pop("reconciliation_notice", None)
        found_all = bool(plan["orders"]) and not remaining and not plan.get("failed_requests")
        if found_all and not plan.get("cancel_requested"):
            plan.update(state="awaiting_payment", late_found=True)
            self.save(plan)
            return plan
        if plan["orders"]:
            if found_all:
                plan["cancel_reason"] = "你要求撤回，已把这笔订单撤回，没扣钱。"
            else:
                plan["cancel_reason"] = "这次下单只成功了一部分，为避免只下成一半，已撤回已下成的单，没扣钱。"
            plan["state"] = "rollback_pending"
            self.save(plan)
            return await self._cancel(token, plan)
        if kind == 'business' and plan.get("failure"):
            info = plan["failure"]
        elif kind == 'manual':
            info = {"kind": "manual", "message": "暂时无法确认这单有没有下成。请打开微信麦当劳小程序 → 我的订单看一下：有新订单就直接付款，没有再重新下单。"}
        else:
            info = {"kind": "not_created", "message": "刚才这单没拿到麦当劳的明确结果（原因：网络没拿到结果）。已核对你的麦当劳订单，没有新单，没扣钱，可以重新下单。"}
        plan.update(state="place_failed", failure=info)
        self.save(plan)
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

    async def _ask(self, token, name, args, deadline_at=None):
        """One bounded upstream read: at most VERIFY_QUERY_TIMEOUT s and never past the wall-clock deadline."""
        budget = VERIFY_QUERY_TIMEOUT if deadline_at is None else min(VERIFY_QUERY_TIMEOUT, deadline_at - time.time())
        if budget <= 0:
            raise TimeoutError('verification deadline reached')
        return await asyncio.wait_for(self.upstream.call(token, name, args), budget)

    async def _reconcile(self, token, plan, deadline_at=None):
        """Match the account's new orders to unresolved requests. Only a unique match counts."""
        from collections import Counter
        from decimal import Decimal
        info = {"ok": False, "ambiguous": False}
        still = list(plan.get("pending_requests", []))
        try:
            rows = (await self._ask(token, "order-list", {}, deadline_at))["data"]["list"]
            excluded = set(plan.get("before_order_ids",[])) | {o["orderId"] for o in plan["orders"]}
            for pending in list(still):
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
                    info["ambiguous"] = info["ambiguous"] or len(matches) > 1
                    continue
                r=matches[0]
                detail=(await self._ask(token,"query-order",{"orderId":r["orderId"]},deadline_at))["data"]
                plan["orders"].append(dict(detail,orderId=r["orderId"],price=pending["price"],productList=pending["productList"]))
                still.remove(pending)
                excluded.add(r["orderId"])
            info["ok"] = True
        except Exception:
            # Reading the list failed this round; it neither proves nor disproves an order.
            plan["verify_errors"] = plan.get("verify_errors", 0) + 1
        plan["pending_requests"]=still
        plan["unknown_orders"]=bool(still)
        if plan.get("unknown_orders"):
            plan["reconciliation_notice"]="有下单结果不明的请求，正在按账户订单核对；请勿重新下单。"
        self.save(plan)
        return info

    async def _verify(self, token, plan):
        """Scheduled convergence of an unknown outcome: ends by the wall clock at VERIFY_HARD_STOP seconds."""
        started = plan.get("placing_started") or time.time()
        deadline_at = started + VERIFY_HARD_STOP
        elapsed = time.time() - started
        if elapsed >= VERIFY_HARD_STOP:
            return await self._conclude(token, plan, 'manual')
        checks = plan.setdefault("verify_checks", [])
        due = [o for o in VERIFY_OFFSETS if o <= elapsed and o not in checks]
        final_phase = elapsed >= VERIFY_CONCLUDE
        streak = plan.get("verify_fail_streak", 0)
        retry = False
        if not due and final_phase and not plan.get("verify_read_ok") and streak:
            gap = VERIFY_RETRY_GAPS[min(streak, len(VERIFY_RETRY_GAPS)) - 1]
            retry = time.time() - plan.get("verify_last_try", 0) >= gap
        if not due and not retry:
            return plan
        checks.extend(due)
        info = await self._reconcile(token, plan, deadline_at)
        plan["verify_last_try"] = time.time()
        plan["verify_fail_streak"] = 0 if info["ok"] else streak + 1
        elapsed = time.time() - started
        if info["ok"] and elapsed >= 100:
            plan["verify_read_ok"] = True
        if not plan.get("pending_requests"):
            return await self._conclude(token, plan, 'found')
        if final_phase and info["ambiguous"]:
            return await self._conclude(token, plan, 'manual')
        if final_phase and plan.get("verify_read_ok"):
            return await self._conclude(token, plan, 'not_created')
        if elapsed >= VERIFY_HARD_STOP:
            return await self._conclude(token, plan, 'manual')
        self.save(plan)
        return plan

    async def _cancel(self, token, plan, only_expiring=False):
        plan['rollback_scope']='expiring' if only_expiring else 'all'
        if not plan["dry_run"] and plan.get("unknown_orders"):
            await self._reconcile(token,plan)
        failed = False
        for order in plan["orders"]:
            if str(order.get("status","10")) != "10":
                continue
            if only_expiring and deadline(order['expirePayTime']) > time.time()+60:
                continue
            if not plan["dry_run"]:
                try:
                    current = (await self.upstream.call(token, "query-order", {"orderId": order["orderId"]}))["data"]
                    if str(current.get("status")) == "60":
                        order.update(status="60",orderStatus="已撤回，退券待核验")
                        continue
                    if str(current.get("status")) != "10":
                        order.update({k:current[k] for k in ('status','orderStatus','takeCode','pickupCode','orderCode','mealCode','expirePayTime') if k in current})
                        continue
                    if only_expiring and deadline(current.get('expirePayTime',order['expirePayTime'])) > time.time()+60:
                        order.update(expirePayTime=current['expirePayTime'])
                        continue
                    started = time.monotonic()
                    try:
                        result = await self.upstream.call(token, "cancel-order", {"orderId": order["orderId"], "cancelReasonCode": 3})
                    except Exception as exc:
                        self._record(plan, 'cancel-order', started, exc)
                        raise
                    self._record(plan, 'cancel-order', started, None, result)
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
            if only_expiring:order['auto_expired']=True
        if failed:
            plan["state"] = "rollback_pending"
        elif plan.get("unknown_orders"):
            # Cancelled what was found; the unresolved request keeps being verified and is cancelled if it appears.
            plan.update(state="verifying", cancel_requested=True)
        else:
            plan["state"] = payment_status(plan)
        if not failed and not plan["dry_run"] and plan['state']=='cancelled':
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
                plan.update(unknown_orders=True,state="verifying",verify_checks=[],verify_errors=0)
            if plan["state"] == "verifying":
                return await self._verify(token, plan)
            if plan["state"] not in {"awaiting_payment", "rollback_pending"}:
                return plan
            if not plan["dry_run"]:
                for order in plan["orders"]:
                    current = (await self.upstream.call(token, "query-order", {"orderId": order["orderId"]}))["data"]
                    order.update({k:current[k] for k in ["status", "orderStatus", "takeCode", "pickupCode", "orderCode", "mealCode", "expirePayTime"] if k in current})
            if plan["state"] == "rollback_pending" or any(deadline(o["expirePayTime"]) <= time.time()+60 and str(o.get("status")) == "10" for o in plan["orders"]):
                return await self._cancel(token, plan,only_expiring=plan['state']!='rollback_pending' or plan.get('rollback_scope')=='expiring')
            plan['state']=payment_status(plan)
            self.save(plan)
            return plan

    async def inspect_payment(self, token, plan_id):
        """Read upstream status into a detached snapshot; never persist or cancel."""
        plan=self.load(plan_id,token)
        if plan['state'] in {'quoted','placing'}:return plan
        if not plan.get('dry_run'):
            for order in plan.get('orders',[]):
                try:
                    current=(await self.upstream.call(token,'query-order',{'orderId':order['orderId']}))['data']
                    order.update({k:current[k] for k in ('status','orderStatus','takeCode','pickupCode','orderCode','mealCode','expirePayTime') if k in current})
                    order.pop('query_error',None)
                except Exception:
                    order['query_error']='暂时查不到订单状态，正在重试'
        if not plan.get('unknown_orders'):plan['state']=payment_status(plan)
        return plan

    async def wait_payment(self, token, plan_id, timeout=60):
        until=time.monotonic()+max(0,min(60,float(timeout)))
        while True:
            plan=await self.inspect_payment(token,plan_id)
            if plan['state']=='verifying':
                # Advance the scheduled order-list check; reads only the account's own orders.
                try:plan=await self.status(token,plan_id)
                except Exception:pass
            if plan['state'] not in {'awaiting_payment','verifying'} or time.monotonic()>=until:
                return plan
            await asyncio.sleep(min(3,max(0,until-time.monotonic())))


def failure_info(exc):
    """What the user may be told about an explicit failure; raw upstream wording included."""
    return {"kind": "business", "message": str(exc), "code": getattr(exc, "code", None),
            "upstream_message": diagnostics.scrub_text(getattr(exc, "upstream_message", "")),
            "busy": str(exc) == BUSY_TEXT}


def payment_status(plan):
    statuses=[str(o.get('status','10')) for o in plan.get('orders',[])]
    if not statuses:return plan['state']
    if '10' in statuses:return 'awaiting_payment'
    if all(s=='60' for s in statuses):return 'cancelled'
    if '60' in statuses:return 'partially_completed'
    return 'paid'


def deadline(value):
    if isinstance(value, (int, float)):
        return value
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
