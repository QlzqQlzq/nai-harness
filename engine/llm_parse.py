# -*- coding: utf-8 -*-
"""Prompt resolver with an injectable tool loop and a lossless direct path."""
from __future__ import annotations

import asyncio
import json
import re
from contextvars import ContextVar
from typing import Any

from .prompt_agent import PromptAgent, PromptTools, SYSTEM, _strong_local_result
from .prompt_policy import explicit_style, merge_tags, needs_translation, protected_spans
from .resolver_direct import DirectResolver

SYSTEM_PROMPT = SYSTEM
_ROLE_HINT = re.compile(r"([^\s（）()]{1,24})[（(]([^（）()]{1,30})[)）]")
_SCENE_HINTS = re.compile(
    r"坐着|站着|躺着|走路|奔跑|看着|望向|窗边|房间|街道|城市|夜景|天空|背景|穿着|身穿|拿着|抱着|动作|姿势|画面|构图|sitting|standing|lying|walking|running|background|scene|wearing",
    re.I,
)


def _search_had_auth(fn: Any) -> bool:
    owner = getattr(fn, "__self__", None)
    if owner is not None:
        value = getattr(owner, "last_had_auth", None)
        if value is not None:
            return bool(value)
    value = getattr(fn, "last_had_auth", None)
    return True if value is None else bool(value)


def _search_query(text: str) -> str:
    """Use the old character-first query; work qualifiers stay in user text."""
    match = _ROLE_HINT.search(str(text or ""))
    if match:
        return "%s 角色 设定 发色 瞳色 服装" % match.group(1).strip()
    return str(text or "").strip()


def is_pure_tags(text: str) -> bool:
    return bool(str(text or "").strip()) and not needs_translation(text)


def parse_llm_json(text: str) -> dict | None:
    raw = str(text or "").strip()
    start = raw.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


class LlmResolver:
    def __init__(self, *, call: Any, seed_factory: Any, provider_id="",
                 model="nai-diffusion-5-full", size="832x1216", steps=23,
                 scale=5.0, cfg=0.0, sampler="k_euler_ancestral",
                 noise_schedule="karras", other=0, negative="", artist="",
                 style_of=None, style_btag_of=None, style_snapshot_of=None,
                 system_prompt=SYSTEM_PROMPT, passthrough_pure_tags=True,
                 search=None, refine=None, logger=None,
                 agent=None):
        self._call, self._provider_id = call, provider_id
        self._system_prompt = str(system_prompt or SYSTEM_PROMPT)
        self._negative, self._artist = str(negative or ""), str(artist or "")
        self._style_of, self._style_btag_of = style_of, style_btag_of
        self._style_snapshot_of = style_snapshot_of
        self._search = search
        self._refine = refine
        self._logger = logger
        self._passthrough = bool(passthrough_pure_tags)
        # Pure ASCII input is always immutable, including multi-word tags/prose.
        self._direct_resolver = DirectResolver(
            seed_factory=seed_factory, model=model, size=size, steps=steps,
            scale=scale, cfg=cfg, sampler=sampler, noise_schedule=noise_schedule,
            other=other, negative=negative)
        self._agent = agent or PromptAgent(PromptTools(search=search))
        self._mode = ContextVar("nai_parse_mode", default="direct")
        self._searched = ContextVar("nai_parse_search", default=False)
        self._trace = ContextVar("nai_parse_trace", default=())

    @property
    def last_mode(self):
        return self._mode.get()

    @property
    def last_search(self):
        return self._searched.get()

    @property
    def last_trace(self):
        return list(self._trace.get())

    async def resolve(self, raw: Any, ctx: Any = None) -> dict:
        self._mode.set("direct")
        self._searched.set(False)
        self._trace.set(())
        text = self._direct_resolver._text(raw)
        uc = self._uc_of(raw)
        out = self._direct_resolver.resolve(raw, ctx)
        if not text or (self._passthrough and is_pure_tags(text) and not needs_translation(uc)):
            return out
        self._mode.set("llm")
        # A strong local alias/tag hit already pins the identity. Do not pay
        # for a duplicate web prefetch before the agent performs its bounded
        # local confirmation.
        presearch = None
        if self._search is not None and not await asyncio.to_thread(self._has_strong_local_hit, text):
            presearch = await self._prefetch_search(text)
        try:
            data, trace = await self._agent.resolve(
                self._call, self._provider_id, self._system_prompt,
                text, uc, parse_llm_json, presearch=presearch)
        except Exception as exc:
            fn = getattr(self._logger, "warning", None)
            if callable(fn):
                fn("[nai_core][llm] prompt resolution failed: %s", type(exc).__name__)
            return self._failure("提示词解析服务暂未完成，请稍后重试；本次未提交生成。")
        self._trace.set(tuple(trace))
        self._searched.set(any(t.get("tool") == "web_search" for t in trace))
        if data.get("__resolution_status") == "ask":
            candidates = [str(c).strip() for c in (data.get("__resolution_candidates") or [])
                          if str(c).strip()][:13]
            question = str(data.get("__resolution_reason") or "未确认角色身份，请选择要用的角色。")[:240]
            if candidates:
                question += "\n" + "\n".join("%d. %s" % (i, c) for i, c in enumerate(candidates, 1))
                question += "\n回复 /nai 编号确认，或补充作品名重发。"
            return {**self._failure(question), "status": "ask", "candidates": candidates}
        if data.get("__resolution_status") == "miss":
            reason = str(data.get("__resolution_reason") or "未找到该角色官方 tag，本次未出图。")
            return {**self._failure(reason), "status": "miss", "reason": reason}
        if isinstance(data.get("ask"), list) and not data.get("tag"):
            candidates = [s.strip() for s in data["ask"] if isinstance(s, str) and s.strip()][:13]
            question = str(data.get("question") or "未确认角色身份，请补充作品名或外观描述。")[:240]
            if candidates:
                question += "\n" + "\n".join("%d. %s" % (i, c)
                                               for i, c in enumerate(candidates, 1))
                question += "\n回复 /nai 编号，或补充作品名。"
            return {**self._failure(question), "status": "ask", "candidates": candidates}
        req = out["req"]
        # Only the UC needs translation for an otherwise direct request.
        req["negative"] = merge_tags(self._negative,
                                    ", ".join(protected_spans(uc)) if needs_translation(uc) else uc,
                                    data.get("negative", ""))
        if is_pure_tags(text):
            return out
        req["preserve_prompt"] = False
        req["tag"] = data["tag"].strip()
        roles = []
        for item in data.get("multi_role_list", []):
            if item.get("prompt", "").strip():
                roles.append({k: str(item.get(k) or "").strip()
                              for k in ("prompt", "position", "uc", "source_name")})
        req["addition"] = {"multi_role_list": roles}
        req["artist"] = str(data.get("artist") or "").strip()
        req["b_tag"] = ""
        if not explicit_style(text, str(data.get("style_source") or "")):
            snapshot = self._style_snapshot(raw)
            req["artist"] = snapshot["artist"]
            req["b_tag"] = snapshot["b_tag"]
        return out

    def _has_strong_local_hit(self, text: str) -> bool:
        tools = getattr(self._agent, "tools", None)
        local = getattr(tools, "local", None)
        if not callable(local):
            return False
        # Probe likely identity spans rather than the whole mixed sentence.
        # This keeps a known Chinese alias on the local fast path while still
        # allowing ordinary prose/actions to reach the bounded agent loop.
        terms: list[str] = []
        hint = _ROLE_HINT.search(str(text or ""))
        if hint:
            terms.append(hint.group(1).strip())
        terms.extend(re.findall(r"[\u3400-\u9fff]{2,16}", str(text or "")))
        terms.extend(re.findall(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_'.:-]{2,63}(?:_\([^()]+\))?", str(text or "")))
        seen: set[str] = set()
        for term in terms:
            key = term.casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                result = local(term)
            except Exception:
                continue
            if _strong_local_result(result):
                return True
        return False

    async def _prefetch_search(self, text: str) -> dict[str, Any] | None:
        """Restore the old search-before-translation path for the real DSSearch client."""
        fn = self._search
        owner = getattr(fn, "__self__", None)
        if fn is None or owner is None or not hasattr(owner, "last_had_auth"):
            return None
        value = str(text or "").strip()
        if (len(value) > 12 and _SCENE_HINTS.search(value)
                and not _ROLE_HINT.search(value)):
            return None
        query = _search_query(text)
        try:
            evidence = await fn(query)
        except Exception:
            return None
        status = getattr(owner, "last_status", "ok" if evidence else "empty")
        event = {"tool": "web_search", "query": query[:240],
                 "status": status, "untrusted_evidence": str(evidence or "")[:6000]}
        if (evidence and not _search_had_auth(fn) and self._refine is not None
                and len(value) <= 16):
            try:
                terms = await self._refine(text)
            except Exception:
                terms = []
            for term in (terms or [])[:2]:
                candidate = str(term or "").strip()
                if not candidate:
                    continue
                try:
                    extra = await fn("%s 动漫角色 设定 干员" % candidate)
                except Exception:
                    extra = ""
                if extra:
                    event["untrusted_evidence"] += "\n\n" + str(extra)[:6000]
                    break
        return event

    def _style_snapshot(self, raw):
        uid = str(raw.get("uid") or "") if isinstance(raw, dict) else ""
        if callable(self._style_snapshot_of):
            snapshot = self._style_snapshot_of(uid)
            if snapshot.get("selected"):
                return {"artist": str(snapshot.get("artist") or ""),
                        "b_tag": str(snapshot.get("b_tag") or "")}
        artist = self._style_of(uid) if callable(self._style_of) else ""
        btag = self._style_btag_of(uid) if callable(self._style_btag_of) else ""
        # A b_tag-only selection replaces the entire default style.
        return {"artist": str(artist or ("" if btag else self._artist)),
                "b_tag": str(btag or "")}

    @staticmethod
    def _uc_of(raw):
        if not isinstance(raw, dict) or not raw.get("uc_enabled", True):
            return ""
        return str(raw.get("uc") or "").strip()

    @staticmethod
    def _failure(reason):
        return {"status": "miss", "req": None, "cache_view": {},
                "overridden": "none", "reason": reason}
