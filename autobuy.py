"""Optional rinko-compatible account calls, sharing the drawing request budget."""
import asyncio
import hashlib
import os
import time

from .routing import ServiceError, request


class AutoBuy:
    def __init__(self, state, router):
        self.state, self.router = state, router
        self.lock = asyncio.Lock()

    @staticmethod
    def identity(settings):
        return hashlib.sha256((settings.login_url + "\n" + settings.username).encode()).hexdigest()

    async def ensure(self, endpoint, budget, before_request=None):
        c = endpoint.auto_buy
        password = os.environ.get(c.password_env, "") if c.password_env else c.password
        if not c.enabled or not password:
            return False
        async with self.lock:
            identity = self.identity(c)
            day = int(time.time() // 86400)
            state = dict(self.state.get("autobuy", identity, {}))
            if state.get("uncertain"):
                return False
            if state.get("day") != day:
                state.update(day=day, packs=0)
            if (time.time() - state.get("last", 0) < c.cooldown_seconds
                    or state.get("packs", 0) + c.packs_per_buy > c.max_packs_per_day):
                return False

            async def call(url, data, method="POST", token=""):
                if before_request is not None:
                    before_request()
                left = budget.deadline - time.monotonic()
                if budget.remaining <= 0 or left <= 0:
                    raise ServiceError("budget")
                budget.remaining -= 1
                record = {"pool": "autobuy", "service": endpoint.name, "key": "account", "result": "started"}
                budget.attempts.append(record)
                target = endpoint.model_copy(update={"url": url, "extra_body": {}, "headers": {}})
                headers = {"Authorization": "Bearer " + token} if token else {}
                try:
                    response = await asyncio.wait_for(request(self.router._client(endpoint), target, headers,
                        min(left, c.timeout_seconds), data, method=method,
                        connect_timeout=self.router.network.connect_timeout,
                        max_bytes=self.router.network.max_response_bytes), min(left, c.timeout_seconds))
                    value = response.json()
                    if not isinstance(value, dict):
                        raise ValueError("invalid response")
                    record["result"] = "ok"
                    return value
                except asyncio.CancelledError:
                    record["result"] = "cancelled"
                    raise
                except Exception:
                    record["result"] = "failed"
                    raise

            login = await call(c.login_url, {"username": c.username, "password": password})
            account = login.get("data")
            token = account.get("access_token") if isinstance(account, dict) else None
            if not isinstance(token, str) or not token:
                return False
            status = await call(c.status_url, {}, "GET", token)
            data = status.get("data")
            if not isinstance(data, dict):
                return False
            try:
                values = [data[k] for k in ("image_pack_credits", "balance_gems", "pack_gems")]
                if any(isinstance(value, bool) or not isinstance(value, (int, str)) for value in values):
                    return False
                credits, gems, price = map(int, values)
            except (KeyError, ValueError, TypeError):
                return False
            cost = price * c.packs_per_buy
            if credits < 0 or credits >= c.min_credits or price <= 0 or cost > min(gems, c.max_gems_per_buy):
                return False
            # A successful purchase must leave one attempt for the original drawing.
            if budget.remaining < 2 or budget.deadline <= time.monotonic():
                return False
            # Persist before submitting: an uncertain purchase must never be silently repeated.
            previous = dict(state)
            state.update(packs=state.get("packs", 0) + c.packs_per_buy, last=time.time(), uncertain=True)
            await self.state.set("autobuy", identity, state)
            recorded = len(budget.attempts)
            try:
                value = await call(c.purchase_url, {"packs": c.packs_per_buy}, token=token)
            except Exception:
                if len(budget.attempts) == recorded:
                    # Budget or policy rejected the call before dispatch.
                    await self.state.set("autobuy", identity, previous)
                raise
            if value.get("message") != "success":
                return False
            await self.state.set("autobuy", identity, {**state, "uncertain": False})
            return True
