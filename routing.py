"""Bounded service/key routing, shared by parsing, search, reverse and drawing."""
import asyncio
import hashlib
import json
import os
import time
import zlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from .config import Pool, SplitPool


class ServiceError(Exception):
    def __init__(self, category, attempts=(), status=None):
        super().__init__(category)
        self.category, self.attempts, self.status = category, list(attempts), status


@dataclass
class Budget:
    remaining: int
    deadline: float
    attempts: list = field(default_factory=list)


class Router:
    def __init__(self, network, client=None):
        self.network, self.external_client = network, client
        self.clients, self.cursor, self.health = {}, {}, {}
        self.last_attempts = ContextVar("nai_routes", default=())
        self.save_health = None

    def _client(self, endpoint):
        if self.external_client is not None:
            return self.external_client
        proxy = endpoint.proxy or self.network.proxy
        if proxy not in self.clients:
            self.clients[proxy] = httpx.AsyncClient(
                proxy=proxy or None, trust_env=False, follow_redirects=False,
                limits=httpx.Limits(max_connections=self.network.max_connections))
        return self.clients[proxy]

    async def close(self):
        for client in self.clients.values():
            await client.aclose()

    def _ordered(self, items, name, strategy):
        if not items or strategy == "ordered":
            return list(items)
        start = self.cursor.get(name, 0) % len(items)
        self.cursor[name] = (start + 1) % len(items)
        return list(items[start:]) + list(items[:start])

    @staticmethod
    def _identity(label, endpoint, key, secret):
        return (label, endpoint.name, key.name, hashlib.sha256((endpoint.url + "\n" + secret).encode()).hexdigest())

    def restore(self, service="", key=""):
        selected = [identity for identity in self.health
                    if (not service or identity[1] == service) and (not key or identity[2] == key)]
        for identity in selected:
            del self.health[identity]
        return len(selected)

    def status(self):
        now = time.time()
        return [{"pool": i[0], "service": i[1], "key": i[2],
                 "state": "disabled" if until == float("inf") else "cooldown",
                 "seconds": None if until == float("inf") else max(0, round(until - now))}
                for i, until in self.health.items() if until > now]

    async def call(self, config, nsfw, label, operation, model="", budget=None):
        pools = [(label, config)] if isinstance(config, Pool) else []
        if isinstance(config, SplitPool):
            selected = "nsfw" if nsfw else "sfw"
            pools = [(label + "." + selected, getattr(config, selected))]
            permitted = "nsfw_to_sfw" if nsfw else "sfw_to_nsfw"
            if config.cross_fallback in (permitted, "both"):
                other = "sfw" if nsfw else "nsfw"
                pools.append((label + "." + other, getattr(config, other)))
        primary = pools[0][1]
        budget = budget or Budget(primary.max_attempts, time.monotonic() + primary.total_timeout)
        last = ServiceError("unconfigured")
        try:
            for pool_label, pool in pools:
                for endpoint in self._ordered(pool.endpoints, pool_label, pool.strategy):
                    if not endpoint.enabled or not endpoint.url or (endpoint.paid and not pool.allow_paid):
                        continue
                    if model and endpoint.models and model not in endpoint.models:
                        continue
                    for key in self._ordered(endpoint.keys, pool_label + ":" + endpoint.name, endpoint.key_strategy):
                        secret = os.environ.get(key.env, "") if key.env else key.secret
                        if not key.enabled or (not secret and (key.env or not endpoint.anonymous)):
                            continue
                        identity = self._identity(pool_label, endpoint, key, secret)
                        if self.health.get(identity, 0) > time.time():
                            continue
                        retries = key.retries if key.retries is not None else endpoint.defaults.retries
                        interval = key.retry_interval if key.retry_interval is not None else endpoint.defaults.retry_interval
                        for attempt in range(1 + retries):
                            left = budget.deadline - time.monotonic()
                            if budget.remaining <= 0 or left <= 0:
                                raise ServiceError("budget", budget.attempts)
                            budget.remaining -= 1
                            headers = dict(endpoint.headers)
                            if secret:
                                headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                                headers["Authorization"] = "Bearer " + secret
                            timeout = min(left, key.timeout_seconds or endpoint.timeout_seconds)
                            record = {"pool": pool_label, "service": endpoint.name, "key": key.name,
                                      "try": attempt + 1, "status": None, "result": "started"}
                            budget.attempts.append(record)
                            try:
                                value = await asyncio.wait_for(operation(endpoint, self._client(endpoint), headers, timeout), timeout)
                                record["result"] = "ok"
                                return value
                            except asyncio.CancelledError:
                                record["result"] = "cancelled"
                                raise
                            except (asyncio.TimeoutError, httpx.TimeoutException):
                                last = ServiceError("timeout")
                            except httpx.RequestError:
                                last = ServiceError("server")
                            except ServiceError as exc:
                                last = exc
                            record.update(result=last.category, status=last.status)
                            policy = getattr(endpoint.defaults, last.category, None)
                            override = getattr(key, last.category, None)
                            if policy is not None and override is not None:
                                policy = policy.model_copy(update=override.model_dump(exclude_none=True))
                            if policy is None:
                                raise ServiceError(last.category, budget.attempts, last.status)
                            if policy.retry and attempt < retries and policy.action != "stop":
                                await asyncio.sleep(min(interval, max(0, budget.deadline - time.monotonic())))
                                continue
                            if policy.action == "stop":
                                raise ServiceError(last.category, budget.attempts, last.status)
                            if policy.action == "disable":
                                self.health[identity] = float("inf")
                            elif policy.action == "cooldown":
                                until = time.time() + policy.cooldown_seconds
                                if last.category == "quota" and key.quota_reset_hour_utc is not None:
                                    now = datetime.now(timezone.utc)
                                    reset = now.replace(hour=key.quota_reset_hour_utc, minute=0, second=0, microsecond=0)
                                    if reset <= now:
                                        reset += timedelta(days=1)
                                    until = reset.timestamp()
                                self.health[identity] = until
                            if self.save_health is not None:
                                await self.save_health()
                            break
            raise ServiceError(last.category, budget.attempts, last.status)
        finally:
            self.last_attempts.set(tuple(dict(a) for a in budget.attempts))


def check_response(response):
    status = response.status_code
    if 200 <= status < 300:
        return
    category = "bad_request"
    evidence = response.text.lower()
    try:
        evidence += " " + json.dumps(response.json(), ensure_ascii=False).lower()
    except ValueError:
        pass
    quota_error = any(word in evidence for word in (
        "insufficient_user_quota", "insufficient_quota", "insufficient quota", "quota exhausted", "额度不足", "余额不足", "预扣费"))
    if status in (401, 403):
        category = "auth"
        if status == 403 and quota_error:
            category = "quota"
    elif status == 402:
        category = "quota"
    elif status == 429:
        category = "quota" if quota_error else "rate_limit"
    elif status >= 500:
        category = "server"
    raise ServiceError(category, status=status)


async def request(client, endpoint, headers, timeout, payload, *, method="POST", connect_timeout=10, max_bytes=33554432):
    values = {**endpoint.extra_body, **payload}
    args = {"params": values} if method == "GET" else {"json": values}
    outgoing = client.build_request(method, endpoint.url, headers=headers,
        timeout=httpx.Timeout(timeout, connect=min(timeout, connect_timeout)), **args)
    # Clients are shared across keys: server cookies must not transfer account sessions.
    if not any(name.lower() == "cookie" for name in headers):
        outgoing.headers.pop("cookie", None)
    if not any(name.lower() == "accept-encoding" for name in headers):
        outgoing.headers["accept-encoding"] = "gzip, deflate"
    streamed = await client.send(outgoing, stream=True, follow_redirects=False)
    try:
        response = await read_response(streamed, max_bytes)
    finally:
        await streamed.aclose()
    check_response(response)
    return response


async def read_response(streamed, max_bytes):
    data = bytearray()
    encoding = streamed.headers.get("content-encoding", "identity").strip().lower()
    invalid = ServiceError("invalid_response", status=streamed.status_code)
    if streamed.is_stream_consumed:
        if len(streamed.content) > max_bytes:
            raise invalid
        data.extend(streamed.content)
    else:
        if encoding not in {"", "identity", "gzip", "x-gzip", "deflate"}:
            raise invalid
        compressed = encoding in {"gzip", "x-gzip", "deflate"}
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding in {"gzip", "x-gzip"} else None
        packed = bytearray()
        received = 0
        try:
            async for raw in streamed.aiter_raw():
                received += len(raw)
                if received > max_bytes:
                    raise invalid
                if not compressed:
                    if len(data) + len(raw) > max_bytes:
                        raise invalid
                    data.extend(raw)
                    continue
                if encoding == "deflate":
                    packed.extend(raw)
                    continue
                while raw:
                    chunk = decoder.decompress(raw, max_bytes - len(data) + 1)
                    if len(data) + len(chunk) > max_bytes:
                        raise invalid
                    data.extend(chunk)
                    raw = decoder.unconsumed_tail
                    if decoder.eof and decoder.unused_data:
                        raw = decoder.unused_data
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            if encoding == "deflate":
                # Raw DEFLATE can resemble a zlib header: validate both bounded forms.
                for bits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
                    decoder = zlib.decompressobj(bits)
                    try:
                        chunk = decoder.decompress(packed, max_bytes + 1)
                    except zlib.error:
                        continue
                    if len(chunk) > max_bytes:
                        raise invalid
                    if decoder.eof and not decoder.unused_data:
                        data.extend(chunk)
                        break
                else:
                    raise invalid
            elif compressed and not decoder.eof:
                raise invalid
        except zlib.error as exc:
            raise invalid from exc
    # Content is now decoded, so retaining these headers would decode it twice.
    headers = streamed.headers.copy()
    headers.pop("content-encoding", None)
    headers.pop("content-length", None)
    return httpx.Response(streamed.status_code, headers=headers,
                          content=bytes(data), request=streamed.request)
