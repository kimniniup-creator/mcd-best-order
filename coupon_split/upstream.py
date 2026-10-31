import asyncio
import hashlib
import json
import time
import copy
import math
import logging
import contextvars
from contextlib import asynccontextmanager
from collections import defaultdict, deque

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

READ_ONLY = {"query-nearby-stores", "query-meals", "query-meal-detail", "query-store-coupons", "query-my-coupons", "calculate-price", "query-order", "order-list", "delivery-query-addresses", "delivery-query-stores", "query-my-account"}


def authorization_error(exc):
    if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in {401,403}:
        return True
    if any(s in str(exc).lower() for s in ["401", "403", "unauthorized", "invalid token"]):
        return True
    return any(authorization_error(e) for e in getattr(exc,"exceptions",[]))


class UpstreamError(ValueError):
    """Typed upstream failure. Messages are user-facing; raw fields stay as attributes."""
    kind = 'error'
    http_status = None
    code = None
    upstream_message = ''
    trace_id = ''
    duration_ms = None

    def __init__(self, message, *, code=None, upstream_message='', trace_id='', http_status=None, kind=None):
        super().__init__(message)
        self.code, self.upstream_message, self.trace_id = code, upstream_message, trace_id
        self.http_status = http_status
        if kind:
            self.kind = kind


class UpstreamAuthError(UpstreamError):
    kind = 'auth'


class UpstreamBusinessError(UpstreamError):
    """McDonald's answered with success:false / a non-200 code: an explicit refusal."""
    kind = 'business'


class UpstreamBusyError(UpstreamBusinessError):
    """Rate limit (code 508 / HTTP 429). Read-only calls are retried with backoff."""
    kind = 'busy'


class UpstreamTransportError(UpstreamError):
    """No usable answer: timeout, disconnect, HTTP 5xx, unparseable body. Outcome unknown."""
    kind = 'transport'


BUSY_TEXT = '麦当劳那边有点忙，稍等我重试'


def business_message(code, message):
    text = str(message or '')
    if code in (508, 429) or '太火爆' in text:
        return BUSY_TEXT
    if str(code) == '60006':
        return '这个餐品现在无法下单：可能已售罄，也可能是套餐选配数量不符，请重新选配或换一道餐品。麦当劳原话：' + (text or '商品已售罄或不可售')
    if str(code) == '600012':
        return '套餐选配数量超出限制，请重新选配。麦当劳原话：' + text
    if str(code) == '600010':
        return '优惠券参数有误，请重新报价。麦当劳原话：' + text
    if any(word in text for word in ('不可售', '停售', '售罄', '未售', '不支持外送')):
        return '所选餐品暂不可售，请换一道餐品或改为到店自取。麦当劳原话：' + text
    return ('麦当劳提示：' + text) if text else '麦当劳暂时无法处理这次请求，请稍后重试'


def _leaves(error):
    nested = getattr(error, 'exceptions', None)
    return [leaf for child in nested for leaf in _leaves(child)] if nested else [error]


def is_timeout(exc):
    return any(isinstance(e, (httpx.TimeoutException, TimeoutError)) for e in _leaves(exc))


class Upstream:
    """Allowlisted upstream adapter. Tokens never enter persisted evidence."""
    def __init__(self, url="https://mcp.mcd.cn", allow_real=False, max_per_minute=120):
        self.url, self.allow_real = url, allow_real
        # McDonald's answers 508 long before the nominal account limit; stay well below it.
        self.max_per_minute=max_per_minute
        self.retry_delays=(1.0,2.0,4.0)  # read-only calls only; never create/cancel
        self.busy_retries=0
        self.confirmed_addresses = set()
        self.gate = asyncio.Semaphore(4)
        self.windows = defaultdict(deque)
        self.rate_lock = asyncio.Lock()
        self.cache = {}
        self.cache_hits = 0
        self.network_calls = 0
        self.session = contextvars.ContextVar("upstream_session", default=None)

    @asynccontextmanager
    async def batch(self, token):
        """One authenticated MCP connection per quote; child price tasks share it."""
        try:
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=45) as http:
                async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        ctx = self.session.set((hashlib.sha256(token.encode()).hexdigest(), session))
                        try:
                            yield
                        finally:
                            self.session.reset(ctx)
        except ValueError:
            raise
        except (KeyError,TypeError,AssertionError):
            raise
        except Exception as exc:
            if authorization_error(exc):
                raise ValueError("麦当劳账户授权失效，请重新授权") from None
            def leaves(error):
                nested=getattr(error,'exceptions',None)
                return [leaf for child in nested for leaf in leaves(child)] if nested else [error]
            business=[e for e in leaves(exc) if isinstance(e,ValueError)]
            if business:raise business[0] from None
            raise ValueError("麦当劳报价连接失败，请稍后重试") from None

    def capacity_wait(self, token, required=38):
        now = time.monotonic()
        window = self.windows[hashlib.sha256(token.encode()).hexdigest()]
        active = [stamp for stamp in window if stamp > now - 60]
        excess = len(active) + required - self.max_per_minute
        return max(0, active[excess-1] + 60 - now) if excess > 0 else 0

    async def ensure_capacity(self, token, required=38, reserve=False):
        while True:
            async with self.rate_lock:
                window=self.windows[hashlib.sha256(token.encode()).hexdigest()]
                now=time.monotonic()
                while window and window[0] <= now-60:
                    window.popleft()
                if len(window)+required <= self.max_per_minute:
                    if reserve:
                        window.append(now)
                        self.network_calls += 1
                    return
                delay=max(.01,60-(now-window[0]))
            await asyncio.sleep(delay)

    async def call(self, token, name, args):
        if name == "delivery-create-address":
            signature = (hashlib.sha256(token.encode()).hexdigest(), json.dumps(args,sort_keys=True))
            if signature not in self.confirmed_addresses:
                raise ValueError("新地址尚未确认")
            self.confirmed_addresses.remove(signature)
        elif name not in READ_ONLY and not (self.allow_real and name in {"create-order", "cancel-order"}):
            raise ValueError("上游写操作已关闭")
        key = hashlib.sha256(token.encode()).hexdigest()
        cache_key = (key, name, json.dumps(args, sort_keys=True))
        # Coupon availability must be fresh after real creation/cancellation.
        ttl = 60 if name in {"query-meals", "query-meal-detail"} else 0
        cached = self.cache.get(cache_key)
        if ttl and cached and time.monotonic() - cached[0] < ttl:
            self.cache_hits += 1
            logging.info("upstream cache hit: %s", name)
            return copy.deepcopy(cached[1])
        delays = self.retry_delays if name in READ_ONLY else ()
        attempt = 0
        while True:
            try:
                data = await self._attempt(token, name, args, key)
                break
            except UpstreamError as exc:
                retryable = isinstance(exc, UpstreamBusyError) or (isinstance(exc, UpstreamTransportError) and exc.kind == 'http5xx')
                if not retryable or attempt >= len(delays):
                    raise
                self.busy_retries += 1
                logging.info("upstream retry %s after %s", name, exc.kind)
                await asyncio.sleep(delays[attempt])
                attempt += 1
        if ttl:
            self.cache[cache_key] = (time.monotonic(), copy.deepcopy(data))
        return data

    async def _attempt(self, token, name, args, key):
        async with self.gate:
            # Reserve atomically, including concurrent readers outside quote().
            await self.ensure_capacity(token, required=1, reserve=True)
            started = time.monotonic()
            try:
                shared = self.session.get()
                if shared and shared[0] == key:
                    result = await shared[1].call_tool(name, args)
                else:
                    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=45) as http:
                        async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                result = await session.call_tool(name, args)
                data = result.structuredContent
                if data is None:
                    raw = "\n".join(c.text for c in result.content if c.type == "text")
                    try:
                        data = json.loads(raw.split("## Original Response")[-1].strip().strip("`"))
                    except ValueError:
                        raise UpstreamTransportError('麦当劳返回了无法识别的内容，请稍后重试', kind='parse') from None
                if not isinstance(data, dict):
                    raise UpstreamTransportError('麦当劳返回了无法识别的内容，请稍后重试', kind='parse')
                code = data.get('code')
                if result.isError or data.get("success") is False or data.get("code", 200) != 200:
                    message = data.get('message') or ''
                    trace = str(data.get('traceId') or '')
                    if result.isError and code is None and not message and data.get('success') is not False:
                        raise UpstreamTransportError('麦当劳返回了无法识别的内容，请稍后重试', kind='parse')
                    haystack = str(data).lower()
                    if code in {401, 403} or any(s in haystack for s in ["token", "unauthorized", "授权", "登录"]):
                        raise UpstreamAuthError("麦当劳账户授权失效，请重新授权", code=code, upstream_message=str(message), trace_id=trace)
                    cls = UpstreamBusyError if (code in (508, 429) or '太火爆' in str(message)) else UpstreamBusinessError
                    raise cls(business_message(code, message), code=code, upstream_message=str(message), trace_id=trace)
                return data
            except UpstreamError as exc:
                exc.duration_ms = round((time.monotonic() - started) * 1000)
                raise
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {401, 403}:
                    error = UpstreamAuthError("麦当劳账户授权失效，请重新授权", http_status=status)
                elif status == 429:
                    error = UpstreamBusyError(BUSY_TEXT, http_status=status, code=429)
                elif status >= 500:
                    error = UpstreamTransportError("麦当劳服务暂时不可用，请稍后重试", kind='http5xx', http_status=status)
                else:
                    error = UpstreamTransportError("麦当劳服务暂时不可用，请稍后重试", kind='http4xx', http_status=status)
                error.duration_ms = round((time.monotonic() - started) * 1000)
                raise error from None
            except Exception as exc:
                if authorization_error(exc):
                    error = UpstreamAuthError("麦当劳账户授权失效，请重新授权")
                else:
                    # Never propagate a transport exception containing authorization or URL credentials.
                    error = UpstreamTransportError('无法连接麦当劳服务，请稍后重试', kind='timeout' if is_timeout(exc) else 'disconnect')
                error.duration_ms = round((time.monotonic() - started) * 1000)
                raise error from None
