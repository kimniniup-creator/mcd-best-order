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

READ_ONLY = {"query-nearby-stores", "query-meals", "query-meal-detail", "query-store-coupons", "calculate-price", "query-order", "order-list", "delivery-query-addresses", "delivery-query-stores", "query-my-account"}


def authorization_error(exc):
    if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in {401,403}:
        return True
    if any(s in str(exc).lower() for s in ["401", "403", "unauthorized", "invalid token"]):
        return True
    return any(authorization_error(e) for e in getattr(exc,"exceptions",[]))


class Upstream:
    """Allowlisted upstream adapter. Tokens never enter persisted evidence."""
    def __init__(self, url="https://mcp.mcd.cn", allow_real=False, max_per_minute=480):
        self.url, self.allow_real = url, allow_real
        self.max_per_minute=max_per_minute  # leave 120 calls below the official account limit (600)
        self.confirmed_addresses = set()
        self.gate = asyncio.Semaphore(10)
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
        async with self.gate:
            # Reserve atomically, including concurrent readers outside quote().
            await self.ensure_capacity(token, required=1, reserve=True)
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
                    data = json.loads(raw.split("## Original Response")[-1].strip().strip("`"))
                if result.isError or data.get("success") is False or data.get("code", 200) != 200:
                    message = str(data).lower()
                    if data.get("code") in {401,403} or any(s in message for s in ["token", "unauthorized", "授权", "登录"]):
                        raise ValueError("麦当劳账户授权失效，请重新授权")
                    if any(word in message for word in ('不可售','停售','售罄','未售','不支持外送')):
                        raise ValueError('所选餐品暂不可售，请换一道餐品或改为到店自取')
                    raise ValueError('麦当劳暂时无法处理这次查询，请检查餐品和取餐方式后重试')
                if ttl:
                    self.cache[cache_key] = (time.monotonic(), copy.deepcopy(data))
                return data
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401,403}:
                    raise ValueError("麦当劳账户授权失效，请重新授权") from None
                raise ValueError("麦当劳服务暂时不可用，请稍后重试") from None
            except ValueError:
                raise
            except Exception as exc:
                if authorization_error(exc):
                    raise ValueError("麦当劳账户授权失效，请重新授权") from None
                # Never propagate a transport exception containing authorization or URL credentials.
                raise ValueError('无法连接麦当劳服务，请稍后重试') from None
