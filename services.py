"""Independent network clients. No AstrBot or host LLM dependency."""
import asyncio
import base64
import io
import ipaddress
import socket
import time
import zipfile
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .routing import Budget, ServiceError, read_response, request


def json_body(response):
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise ServiceError("invalid_response") from exc


class Services:
    def __init__(self, config, router):
        self.config, self.router = config, router
        self.search_cache = {}
        self.autobuy = None
        self._search_status = ContextVar("nai_search_status", default="unavailable")

    async def request(self, client, endpoint, headers, timeout, payload, **kwargs):
        return await request(client, endpoint, headers, timeout, payload,
                             connect_timeout=self.config.network.connect_timeout,
                             max_bytes=self.config.network.max_response_bytes, **kwargs)

    @property
    def last_status(self):
        return self._search_status.get()

    async def chat(self, pool, nsfw, messages, temperature, max_tokens, label):
        async def operation(endpoint, client, headers, timeout):
            if endpoint.kind != "openai_chat":
                raise ServiceError("bad_request")
            response = await self.request(client, endpoint, headers, timeout, {
                "model": endpoint.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens, "stream": False})
            try:
                content = json_body(response)["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("empty")
                return content
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ServiceError("invalid_response") from exc
        return await self.router.call(pool, nsfw, label, operation)

    async def llm(self, nsfw, system, user):
        c = self.config.parser
        return await self.chat(c.services, nsfw,
                               [{"role": "system", "content": system}, {"role": "user", "content": user}],
                               c.temperature, c.max_tokens, "parser")

    async def search(self, query):
        c = self.config.search
        self._search_status.set("unavailable")
        if not c.enabled:
            return ""
        cached = self.search_cache.get(query)
        if cached and cached[0] > time.monotonic():
            self._search_status.set("ok")
            return cached[1]

        async def operation(endpoint, client, headers, timeout):
            method = "POST"
            if endpoint.kind == "deepseek_search":
                payload = {"query": query[:128], "count": c.result_count}
            elif endpoint.kind == "tavily":
                payload = {"query": query, "max_results": c.result_count}
            elif endpoint.kind == "searxng":
                payload, method = {"q": query, "format": "json"}, "GET"
            else:
                raise ServiceError("bad_request")
            data = json_body(await self.request(client, endpoint, headers, timeout, payload, method=method))
            if isinstance(data, dict):
                data = data.get("results", data.get("data"))
            if isinstance(data, dict):
                data = data.get("results")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise ServiceError("invalid_response")
            return data
        try:
            rows = await self.router.call(c.services, False, "search", operation)
        except ServiceError:
            self._search_status.set("error")
            return ""
        if c.allowed_domains:
            rows = [r for r in rows if self.host_allowed(r.get("url", ""), c.allowed_domains)]
        rows = rows[:c.result_count]
        text = "\n\n".join("Title: %s\nURL: %s\n%s" % (
            r.get("title", ""), r.get("url", ""), r.get("content", r.get("snippet", r.get("description", "")))) for r in rows)
        if rows and c.fetch_enabled and self.fetchable_url(rows[0].get("url")):
            async def fetch(endpoint, client, headers, timeout):
                if endpoint.kind != "json_fetch":
                    raise ServiceError("bad_request")
                value = json_body(await self.request(client, endpoint, headers, timeout, {"url": rows[0]["url"]}))
                return str(value.get("content", value.get("text", value.get("data", "")))) if isinstance(value, dict) else ""
            try:
                text += "\n" + await self.router.call(c.fetch_services, False, "fetch", fetch)
            except ServiceError:
                pass  # Existing search evidence remains valid when optional fetching fails.
        text = text[:c.max_text_chars]
        self._search_status.set("ok" if text else "empty")
        if text and c.cache_seconds and c.cache_entries:
            if len(self.search_cache) >= c.cache_entries:
                self.search_cache.pop(next(iter(self.search_cache)))
            self.search_cache[query] = (time.monotonic() + c.cache_seconds, text)
        return text

    @staticmethod
    def fetchable_url(url):
        if not isinstance(url, str) or any(ch.isspace() or ord(ch) < 32 for ch in url):
            return False
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme not in {"http", "https"} or not host or parsed.port == 0
                    or parsed.username is not None or parsed.password is not None):
                return False
        except ValueError:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            try:
                # Also reject legacy decimal/octal/hex IPv4 spellings without DNS.
                return ipaddress.ip_address(socket.inet_aton(host)).is_global
            except OSError:
                return "." in host and not host.endswith((".localhost", ".local", ".internal"))

    @staticmethod
    def host_allowed(url, hosts):
        try:
            parsed = urlsplit(str(url))
            host = (parsed.hostname or "").lower()
            parsed.port
            if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
                return False
        except ValueError:
            return False
        return any(host == h.lower() or host.endswith("." + h.lower()) for h in hosts)

    async def download(self, url):
        try:
            parsed = urlsplit(str(url))
            port = parsed.port
        except ValueError as exc:
            raise ServiceError("invalid_response") from exc
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise ServiceError("invalid_response")
        allowed = self.config.network.image_download_hosts
        if allowed and not self.host_allowed(url, allowed):
            raise ServiceError("invalid_response")
        try:
            addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, parsed.hostname,
                port or (443 if parsed.scheme == "https" else 80)), self.config.network.connect_timeout)
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            raise ServiceError("invalid_response") from exc
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ServiceError("invalid_response")
        # Connect to the validated IP, preserving TLS SNI, to avoid a second DNS lookup.
        ip = addresses[0][4][0]
        authority = ("[" + ip + "]") if ":" in ip else ip
        if port:
            authority += ":" + str(port)
        pinned = parsed._replace(netloc=authority).geturl()
        async with asyncio.timeout(self.config.network.download_timeout), httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=self.config.network.download_timeout) as client:
            async with client.stream("GET", pinned, headers={"Host": parsed.netloc, "Accept-Encoding": "gzip, deflate"},
                                     extensions={"sni_hostname": parsed.hostname}) as response:
                # CDN failures are not authentication failures of the drawing key.
                if not 200 <= response.status_code < 300:
                    raise ServiceError("invalid_response", status=response.status_code)
                result = await read_response(response, self.config.network.max_image_bytes)
                return self.verify_image(result.content)

    def verify_image(self, data):
        if not data or len(data) > self.config.network.max_image_bytes:
            raise ServiceError("invalid_response")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > self.config.generation.max_pixels * 4:
                    raise ValueError("image too large")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        except Exception as exc:
            raise ServiceError("invalid_response") from exc
        return data

    async def draw(self, body, nsfw, budget=None, before_request=None):
        pool = self.config.drawing.nsfw if nsfw else self.config.drawing.sfw
        budget = budget or Budget(pool.max_attempts, time.monotonic() + pool.total_timeout)
        attempted_purchase = set()
        async def operation(endpoint, client, headers, timeout):
            if before_request is not None:
                before_request()
            model = endpoint.model_map.get(body["model"], body["model"])
            if endpoint.kind == "native":
                payload = {**body, "model": model}
            elif endpoint.kind == "openai_image":
                p = body["parameters"]
                roles = p.get("characterPrompts", [])
                if roles and endpoint.role_mode == "reject":
                    raise ServiceError("invalid_response")
                prompt = body["input"]
                if endpoint.role_mode == "fold":
                    prompt = ", ".join([prompt] + [r["prompt"] for r in roles if r.get("prompt")])
                payload = {"model": model, "prompt": prompt, "negative_prompt": p["negative_prompt"],
                           "size": "%sx%s" % (p["width"], p["height"]), "n": 1, "response_format": "b64_json"}
                payload.update({k: p[k] for k in ("seed", "steps", "scale", "cfg_rescale", "sampler", "noise_schedule") if k in p})
                if endpoint.role_mode == "native":
                    payload.update({k: p[k] for k in ("characterPrompts", "v4_prompt", "v4_negative_prompt") if k in p})
            else:
                raise ServiceError("bad_request")
            try:
                response = await self.request(client, endpoint, headers, timeout, payload)
            except ServiceError as exc:
                purchase_id = self.autobuy.identity(endpoint.auto_buy) if self.autobuy is not None else None
                if (exc.category != "quota" or not endpoint.auto_buy.enabled or self.autobuy is None
                        or purchase_id in attempted_purchase):
                    raise
                attempted_purchase.add(purchase_id)
                if before_request is not None:
                    before_request()
                bought = False
                try:
                    bought = await self.autobuy.ensure(endpoint, budget, before_request)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                if before_request is not None:
                    before_request()
                if not bought:
                    if endpoint.auto_buy.failure == "stop":
                        raise ServiceError("purchase_stopped") from exc
                    raise exc
                if budget.remaining <= 0 or budget.deadline <= time.monotonic():
                    raise ServiceError("budget")
                budget.remaining -= 1
                retry = {"pool": "drawing", "service": endpoint.name, "key": "after_purchase", "result": "started"}
                budget.attempts.append(retry)
                try:
                    response = await self.request(client, endpoint, headers,
                        min(timeout, budget.deadline - time.monotonic()), payload)
                    retry["result"] = "ok"
                except ServiceError as failure:
                    retry["result"] = failure.category
                    raise
            data = response.content
            if data.startswith(b"PK"):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        entries = [i for i in archive.infolist() if i.filename.lower().endswith((".png", ".jpg", ".webp"))]
                        if not entries or entries[0].file_size > self.config.network.max_image_bytes:
                            raise ValueError("invalid archive")
                        data = archive.read(entries[0])
                except Exception as exc:
                    raise ServiceError("invalid_response") from exc
            elif "json" in response.headers.get("content-type", "").lower():
                try:
                    item = json_body(response)["data"][0]
                    if not isinstance(item, dict):
                        raise ValueError("invalid image entry")
                    if item.get("b64_json"):
                        if len(item["b64_json"]) > self.config.network.max_image_bytes * 1.4:
                            raise ValueError("too large")
                        data = base64.b64decode(item["b64_json"], validate=True)
                    elif item.get("url"):
                        data = await self.download(item["url"])
                    else:
                        raise ValueError("no image")
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ServiceError("invalid_response") from exc
            return self.verify_image(data)
        return await self.router.call(self.config.drawing, nsfw, "drawing", operation, body["model"], budget)

    async def reverse(self, images, nsfw, before_request=None):
        c = self.config.reverse
        if not images:
            raise ServiceError("invalid_response")
        parts = [{"type": "text", "text": c.prompt}]
        for data in images[:c.max_images]:
            self.verify_image(data)
            with Image.open(io.BytesIO(data)) as image:
                mime = Image.MIME.get(image.format, "image/png")
            parts.append({"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(data).decode()}})
        async def operation(endpoint, client, headers, timeout):
            if before_request is not None:
                before_request()
            if endpoint.kind == "json_tagger":
                payload = {"images": [base64.b64encode(i).decode() for i in images[:c.max_images]], "threshold": c.json_tagger_threshold}
                data = json_body(await self.request(client, endpoint, headers, timeout, payload))
                tags = data.get("tags", []) if isinstance(data, dict) else []
                if isinstance(tags, dict):
                    tags = [k for k, v in tags.items() if isinstance(v, (int, float)) and v >= c.json_tagger_threshold]
                if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                    raise ServiceError("invalid_response")
                text = ", ".join(tags)
            elif endpoint.kind == "openai_chat":
                payload = {"model": endpoint.model, "messages": [{"role": "user", "content": parts}],
                           "temperature": c.temperature, "max_tokens": c.max_tokens, "stream": False}
                data = json_body(await self.request(client, endpoint, headers, timeout, payload))
                try:
                    text = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ServiceError("invalid_response") from exc
            else:
                raise ServiceError("bad_request")
            if not isinstance(text, str) or not text.strip():
                raise ServiceError("invalid_response")
            return text.strip()
        return await self.router.call(c.services, nsfw, "reverse", operation)
