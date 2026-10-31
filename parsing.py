"""Adapter around the production tag resolver; lookup keys never rewrite user tags."""
import asyncio
import secrets
from pathlib import Path

from .config import Endpoint

from .engine.llm_parse import LlmResolver
from .engine.prompt_agent import PromptAgent, PromptTools
from .engine.prompt_policy import needs_translation
from .engine.resolver_direct import DirectResolver
from .engine.tagdb import TagDB, lookup_name
from .state import UserError


class Tools(PromptTools):
    def __init__(self, config, services, data_dir):
        self.config = config
        path = Path(config.dictionary.path) if config.dictionary.path else None
        if path is not None and not path.is_absolute():
            path = Path(data_dir) / path
        db = TagDB(path) if config.dictionary.enabled and path else None
        super().__init__(db=db, search=services.search if config.search.enabled else None,
                         skill_dir=Path(data_dir) / config.parser.skills_dir,
                         skills=config.parser.skills, tag_api=config.search.tag_api_enabled)
        self.services = services

    def local(self, query):
        value = super().local(query)
        tag = self.config.dictionary.aliases.get(query)
        if tag:
            value["candidates"] = [{"name": tag, "source": "manual_alias", "category": 4, "exact": True}] + value.get("candidates", [])
            value["status"] = "ok"
        value["candidates"] = value.get("candidates", [])[:self.config.dictionary.candidate_limit]
        return value

    async def run(self, name, query):
        if name == "web_search":
            if not self.config.search.enabled:
                return {"status": "unavailable"}
            if not query or len(query) > 240:
                return {"status": "invalid"}
            evidence = await self.services.search(query)
            return {"status": self.services.last_status, "untrusted_evidence": evidence}
        if name == "local_tags":
            return await asyncio.to_thread(self.local, query)
        if name == "read_skill":
            return await asyncio.to_thread(self.read_skill, query)
        if name != "tag_api":
            return await super().run(name, query)
        if not self.tag_api:
            return {"status": "unavailable"}
        if not query or len(query) > 240:
            return {"status": "invalid"}
        c = self.config.search
        try:
            endpoint = Endpoint(name="tag-api", url=c.tag_api_url)
            response = await self.services.request(self.services.router._client(endpoint), endpoint,
                c.tag_api_headers, c.tag_api_timeout, {"search[name_matches]": lookup_name(query), "limit": c.tag_api_limit}, method="GET")
            if response.status_code != 200:
                return {"status": "error", "http_status": response.status_code}
            rows = response.json()
            if not isinstance(rows, list):
                return {"status": "error"}
            hits = [{k: r.get(k) for k in ("name", "category", "post_count")} for r in rows[:c.tag_api_limit] if isinstance(r, dict)]
            return {"status": "ok" if hits else "empty", "candidates": hits}
        except Exception:
            return {"status": "error"}


class Parser:
    def __init__(self, config, services, data_dir):
        self.config, self.services = config, services
        self.tools = Tools(config, services, data_dir)

    async def resolve(self, text, uc, model, uid, style, nsfw, orientation=""):
        c = self.config
        g = c.generation
        params = {k: getattr(g, k) for k in ("size", "steps", "scale", "cfg", "sampler", "noise_schedule", "other", "negative")}
        params.update(seed_factory=lambda: secrets.randbits(32), model=model)
        raw = {"prompt": text, "uc": uc, "uc_enabled": True, "model": model, "uid": uid, "orientation": orientation}
        if not c.parser.enabled:
            if needs_translation(text) or needs_translation(uc):
                raise UserError("解析功能已关闭，请提供可直出的 tag。")
            result = DirectResolver(**params).resolve(raw)
        else:
            async def call(provider, system, user):
                return await self.services.llm(nsfw, system, user)
            resolver = LlmResolver(call=call, agent=PromptAgent(self.tools, rounds=c.parser.max_rounds,
                calls=c.parser.max_tool_calls, timeout=c.parser.total_timeout),
                system_prompt=c.parser.system_prompt, passthrough_pure_tags=c.parser.pure_tags_direct,
                style_snapshot_of=lambda _: {"selected": True, "artist": style, "b_tag": ""}, **params)
            result = await asyncio.wait_for(resolver.resolve(raw), c.parser.total_timeout)
        if result.get("status") != "ok":
            return result
        req = result["req"]
        if c.modules.styles and style:
            if not needs_translation(text) and c.styles.apply_to_direct:
                req["tag"] = style + ", " + req["tag"]
            elif needs_translation(text) and not c.styles.explicit_style_wins:
                req["artist"] = style
        return result
