# -*- coding: utf-8 -*-
"""Lossless direct prompt path with caller-provided generation defaults and UC."""

from __future__ import annotations

from typing import Any, Callable, Final
from .prompt_policy import merge_tags

__all__ = ["DirectResolver", "MISS_REASON", "size_for"]

MISS_REASON: Final = "输入为空（直通解析没有可用的词）"


def size_for(base_size: str, raw: Any) -> str:
    """按 raw.orientation 换算尺寸：landscape 取宽≥高的那向，portrait 反之，square 取方图。

    输入不是 dict / 没带方向 / 尺寸不是 `WxH` 数字形态 / 已经是目标方向 → 原样返回。
    方形固定 1024x1024（NAI 标准方图）；配置本身是方的就保持配置。
    """

    base = str(base_size or "").strip()
    if not isinstance(raw, dict):
        return base
    orientation = str(raw.get("orientation") or "").strip().lower()
    if orientation not in ("landscape", "portrait", "square"):
        return base
    w, _, h = base.partition("x")
    w, h = w.strip(), h.strip()
    if not (w.isdigit() and h.isdigit()):
        return base
    if orientation == "square":
        return base if w == h else "1024x1024"
    if orientation == "landscape" and int(h) > int(w):
        return f"{h}x{w}"
    if orientation == "portrait" and int(w) > int(h):
        return f"{h}x{w}"
    return base


class DirectResolver:
    """`resolve(raw, ctx)` → `{status, req, cache_view, overridden, reason}`。"""

    def __init__(
        self,
        *,
        seed_factory: Callable[[], int],
        model: str = "nai-diffusion-5-full",
        size: str = "832x1216",
        steps: int = 23,
        scale: float = 5.0,
        cfg: float = 0.0,
        sampler: str = "k_euler_ancestral",
        noise_schedule: str = "karras",
        other: int = 0,
        negative: str = "",
    ) -> None:
        self._seed_factory = seed_factory
        self._model = str(model)
        self._size = str(size)
        self._steps = int(steps)
        self._scale = float(scale)
        self._cfg = float(cfg)
        self._sampler = str(sampler)
        self._noise_schedule = str(noise_schedule)
        self._other = int(other)
        self._negative = str(negative)
        #: 与 `LlmResolver.last_mode` 对齐：装配层统一读这个字段写日志
        self.last_mode: str = "direct"

    # ------------------------------------------------------------------ 协议

    def resolve(self, raw: Any, ctx: Any = None) -> dict[str, Any]:
        text = self._text(raw)
        if not text:
            return {
                "status": "miss",
                "req": None,
                "cache_view": {},
                "overridden": "none",
                "reason": MISS_REASON,
            }
        return {
            "status": "ok",
            "req": {
                "model": str(raw.get("model") or self._model) if isinstance(raw, dict) else self._model,
                "tag": text,
                "negative": self._negative_for(raw),
                "artist": "",
                "b_tag": "",
                "preserve_prompt": True,
                "size": size_for(self._size, raw),
                "seed": int(self._seed_factory()),
                "steps": self._steps,
                "scale": self._scale,
                "cfg": self._cfg,
                "sampler": self._sampler,
                "noise_schedule": self._noise_schedule,
                "other": self._other,
                "addition": {"multi_role_list": []},
            },
            "cache_view": {},
            "overridden": "none",
            "reason": "",
        }

    # ------------------------------------------------------------------ 内部

    def _negative_for(self, raw: Any) -> str:
        """负向 = 配置默认 + 用户 `/UC` 段（uc_enabled 打开时）。

        纯 tag 直出路径也要吃 `/UC`——用户口径 `/nai 横板 纯tag /UC 负面`：
        正向直出不动，负向原样并进 negative。保序去重（大小写/下划线空格等价）。
        """

        base = str(self._negative or "").strip()
        uc = ""
        if isinstance(raw, dict) and raw.get("uc_enabled", True):
            uc = str(raw.get("uc") or "").strip()
        if not uc:
            return base
        return merge_tags(base, uc)

    @staticmethod
    def _text(raw: Any) -> str:
        """兼容两种入参：wiring 传的 raw_input dict，或直接一段文本。"""

        if isinstance(raw, dict):
            for key in ("prompt", "raw", "text"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""
        return str(raw or "").strip()
