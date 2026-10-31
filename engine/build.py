# -*- coding: utf-8 -*-
"""第 2 步：build —— 纯函数，`build(req, cache_view) -> (body, log_payload, appearance_fragment)`。

硬约束（第 0 步契约表）：
  - 不碰网络、不碰文件、不读全局、不调随机源（random / datetime.now() 均禁用）；
    模块级除常量外无任何可依赖对象，negative 等中间值一律为局部变量。
  - 日志不在这里落盘，落 send；build 只产出待记的 log_payload。
  - 角色对象单真相：先构造 _Role（名字 / 外观 / 动作 / 中心点），
    characterPrompts 与 v4_prompt.caption.char_captions 由同一对象经两个渲染函数派生，
    禁止两边分别拼字符串；发前逐字自检，不等即拒发。
  - 幂等回填按「角色整块」粒度：角色外观位非空 → 整块不动（有则不动，来源=model）；
    外观位为空 → 整块注入口径内缓存外观（缺则补，来源=cache）；无缓存/无资格 → fallback。
    一个角色不允许部分字段来自缓存、部分来自模型。回填资格只看来源字段
    （post id + 抓取时间）；复核标记只读透传，不参与任何判定。
  - 保序：tag 不得经过 set 再输出，保序去重用 list（set 仅用于成员判定）。
  - `structural_signature()` 带自检：其引用的字段必须是 body 实际存在的字段，
    不满足即 BuildError（防止结构信号与 build 输出不同步而变成摆设）。

本期不支持（qlzq 定）：img2img / vibe transfer / character_keep（director_reference）
三个图片输入分支；模型→key 映射（model_selector）与 NSFW 打标（nsfw_gate）也不在本步。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "BuildError",
    "AppearanceFragment",
    "APPEARANCE_CACHE",
    "APPEARANCE_MODEL",
    "APPEARANCE_FALLBACK",
    "APPEARANCE_MIXED",
    "OVERRIDDEN_NONE",
    "OVERRIDDEN_USER_COLOR",
    "build",
    "body_to_json",
    "structural_signature",
    "render_character_prompt",
    "render_char_caption",
]

APPEARANCE_CACHE: Final = "cache"
APPEARANCE_MODEL: Final = "model"
APPEARANCE_FALLBACK: Final = "fallback"
APPEARANCE_MIXED: Final = "mixed"
OVERRIDDEN_NONE: Final = "none"
OVERRIDDEN_USER_COLOR: Final = "user_color_override"

_CHAR_TAG_PAT: Final = r"[a-z0-9_'\-]+_\([a-z0-9_'\-]+\)"
_CATEGORY_SUFFIX: Final = (("_hair", "hair"), ("_eyes", "eyes"))

class BuildError(Exception):
    """build 拒发。

    只带机器可读的 `code` 与**原始上下文**（`context`），**不带任何面向用户的话术**：
    人话一律由 `error_map` 单一出口渲染（有映射的走表内，真正没映射的才走表外兜底）。
    """

    def __init__(self, message: str, *, code: str = "build_failed", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = str(context)

    def as_event(self) -> dict[str, str]:
        return {"source": "gate", "code": self.code, "raw_text": self.context}


@dataclass(frozen=True)
class AppearanceFragment:
    """粘在 status 上的「外观片段」，固定四字段（见 WIRING_CONTRACT.md）。

    source     ∈ {cache, model, fallback, mixed}
               mixed 专指「同一请求里不同角色来源不同」（千夏 cache、南宫 fallback），
               不是同一角色部分字段缓存部分字段模型（不允许）。
    injected   实际注入的字段名列表（hair / eyes / ...），保序；回执「按缓存值」靠它生成。
    overridden none | user_color_override（本步只可能这两种，预留不扩）。
    reviewed   复核标记只读透传：整块来自缓存的角色全部已复核才为 True。
    """

    source: str
    injected: tuple[str, ...] = ()
    overridden: str = OVERRIDDEN_NONE
    reviewed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "injected": list(self.injected),
            "overridden": self.overridden,
            "reviewed": self.reviewed,
        }


@dataclass
class _Role:
    """单真相角色对象。"""

    key: str
    name: str
    appearance: list[str]
    tags: list[str]
    position: str
    uc: str


# ------------------------------------------------------------------- 小工具


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """req / role 读取接口：对象属性优先，兼容 mapping。"""

    if obj is None:
        return default
    value = obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)
    return default if value is None else value


def _dedupe_keep_order(items: Sequence[str]) -> list[str]:
    """保序去重。set 只用于成员判定，输出顺序完全来自 list（可复现性硬要求）。"""

    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        tag = str(raw).strip()
        if not tag:
            continue
        low = tag.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(tag)
    return out


def _split_tags(text: Any) -> list[str]:
    if not text:
        return []
    from .prompt_policy import split_tags
    return split_tags(str(text))


def _to_int(value: Any, field: str) -> int:
    """数值参数收口：脏输入不许抛原生 ValueError（那会被上游当成 `build_failed`，看不出是哪一项）。"""

    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise BuildError(
            f"{field} 不是整数：{value!r}",
            code="param_invalid",
            context=f"req.{field}={value!r}",
        ) from exc


def _to_float(value: Any, field: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise BuildError(
            f"{field} 不是数字：{value!r}",
            code="param_invalid",
            context=f"req.{field}={value!r}",
        ) from exc


def _dedupe_artist_tags(text: str) -> str:
    """artist:xxx 只留一份并置顶（沿用旧插件行为，不动其他 tag）。"""

    artists: list[str] = []
    rest: list[str] = []
    seen: set[str] = set()
    for p in _split_tags(text):
        low = re.sub(r"\s+", " ", p.lower())
        if low.startswith("artist:"):
            if low in seen:
                continue
            seen.add(low)
            artists.append(p)
        else:
            rest.append(p)
    return ", ".join(artists + rest)


def _append_style_tags(prompt: str, b_tag: str) -> str:
    """画风 b_tag 追加进正向末尾（去重：大小写/下划线空格等价），原样保留其余 tag。"""

    add = str(b_tag or "").strip()
    if not add:
        return prompt
    base = str(prompt or "").strip()
    seen = {p.strip().lower().replace("_", " ") for p in _split_tags(base) if p.strip()}
    fresh = [p.strip() for p in _split_tags(add)
             if p.strip() and p.strip().lower().replace("_", " ") not in seen]
    if not fresh:
        return base
    return base + ", " + ", ".join(fresh) if base else ", ".join(fresh)


def _category_of(tag: str) -> str | None:
    low = str(tag).strip().lower()
    for suffix, category in _CATEGORY_SUFFIX:
        if low.endswith(suffix) and len(low) > len(suffix):
            return category
    return None


def _character_tags_in(text: str) -> list[str]:
    return re.findall(_CHAR_TAG_PAT, text or "")


def _strip_tags(base: str, tags: Sequence[str]) -> str:
    if not base or not tags:
        return base
    drop = {t.lower() for t in tags}
    kept = [t for t in _split_tags(base) if t.lower() not in drop]
    return ", ".join(kept)


def _multi_role_center(position: str, idx: int, total: int) -> tuple[float, float]:
    """位置(A1~E5) → 归一化坐标；A-E 横向左→右，1-5 纵向上→下。不读全局。"""

    m = re.match(r"^([A-E])([1-5])$", str(position or "").strip().upper())
    if m:
        return (
            round(0.1 + 0.2 * (ord(m.group(1)) - ord("A")), 3),
            round(0.1 + 0.2 * (int(m.group(2)) - 1), 3),
        )
    if total <= 1:
        return (0.5, 0.5)
    return (round((idx + 1) / (total + 1), 3), 0.5)


def _sanitize_for_log(obj: Any) -> Any:
    """日志旁路用：token 抹掉、base64 只留长度，其余原样保留供排查。"""

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "token" and isinstance(v, str) and v:
                out[k] = "***"
            elif k == "base64" and isinstance(v, str) and v:
                out[k] = f"<base64, {len(v)} chars>"
            else:
                out[k] = _sanitize_for_log(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_for_log(i) for i in obj]
    if isinstance(obj, str) and obj.startswith("data:") and len(obj) > 64:
        return f"<data-uri, {len(obj)} chars>"
    return obj


# -------------------------------------------------------- 角色对象 + 渲染


def _build_role(role: Any) -> _Role:
    """把解析层给的单个角色构造为单真相对象（名字 / 外观 / 动作 / 中心点）。"""

    tags = _split_tags(_field(role, "prompt", "") or "")
    key = str(_field(role, "key", "") or _field(role, "name", "") or "")
    name = key
    appearance: list[str] = []
    for tag in tags:
        if not name and re.fullmatch(_CHAR_TAG_PAT, tag):
            name = tag
            continue
        if _category_of(tag):
            appearance.append(tag)
    if not name and tags:
        name = tags[0]
    return _Role(
        key=key or name,
        name=name,
        appearance=appearance,
        tags=tags,
        position=str(_field(role, "position", "") or ""),
        uc=str(_field(role, "uc", "") or _field(role, "negative_prompt", "") or ""),
    )


def _role_prompt_text(role: _Role) -> str:
    """唯一拼装点：名字 + 外观位 + 动作位，全程保序。"""

    prefix = [role.name] if role.name not in role.tags else []
    return ", ".join(_dedupe_keep_order([*prefix, *role.tags, *role.appearance]))


def render_character_prompt(role: _Role) -> str:
    """characterPrompts[i].prompt。"""

    return _role_prompt_text(role)


def render_char_caption(role: _Role) -> str:
    """v4_prompt.caption.char_captions[i].char_caption。"""

    return _role_prompt_text(role)


# ------------------------------------------------------------------- 回填


def _cache_entry(cache_view: Mapping[str, Any], role: _Role) -> Mapping[str, Any] | None:
    roles = cache_view.get("roles") or {}
    if not isinstance(roles, Mapping):
        return None
    for cand in (role.key, role.name):
        entry = roles.get(cand)
        if isinstance(entry, Mapping):
            return entry
    return None


def _backfill(roles: list[_Role], cache_view: Mapping[str, Any]) -> tuple[list[str], list[str], bool]:
    """按角色整块回填。返回 (逐角色来源, 实际注入字段名列表, reviewed)。"""

    per_role: list[str] = []
    injected: list[str] = []
    reviewed_roles: list[bool] = []
    for role in roles:
        entry = _cache_entry(cache_view, role)
        source_meta = (entry or {}).get("source") or {}
        appearance = (entry or {}).get("appearance") or {}
        eligible = (
            isinstance(source_meta, Mapping)
            and bool(source_meta.get("post_id"))
            and bool(source_meta.get("fetched_at"))
            and isinstance(appearance, Mapping)
            and any(str(v).strip() for v in appearance.values())
        )
        if role.appearance:
            per_role.append(APPEARANCE_MODEL)          # 有则不动（用户/模型已给，优先）
            continue
        if not eligible:
            per_role.append(APPEARANCE_FALLBACK)        # 外观位为空，不注入任何外观 tag
            continue
        for category, cache_tag in appearance.items():  # 缺则补：整块写入，保序
            tag = str(cache_tag).strip()
            if tag:
                role.appearance.append(tag)
                injected.append(str(category))
        per_role.append(APPEARANCE_CACHE)
        reviewed_roles.append(bool((entry or {}).get("reviewed")))
    return per_role, injected, bool(reviewed_roles) and all(reviewed_roles)


def _appearance_source(per_role: Sequence[str]) -> str:
    uniq = list(dict.fromkeys(per_role))
    if not uniq:
        return APPEARANCE_FALLBACK
    if len(uniq) > 1:
        return APPEARANCE_MIXED
    return uniq[0]


# ----------------------------------------------------------------- 结构信号

_SIGNATURE_PATHS: Final = (
    ("model",),
    ("action",),
    ("parameters",),
    ("parameters", "params_version"),
    ("parameters", "characterPrompts"),
    ("parameters", "v4_prompt", "caption", "char_captions"),
)


def structural_signature(body: Mapping[str, Any]) -> dict[str, Any]:
    """交给 send 的「结构指纹」：NAI5 结构类 500 的比对基准。

    自检：引用的每个字段必须真实存在于 body，否则 BuildError。
    """

    missing: list[str] = []
    for path in _SIGNATURE_PATHS:
        cur: Any = body
        for key in path:
            if not isinstance(cur, Mapping) or key not in cur:
                missing.append(".".join(path))
                break
            cur = cur[key]
    if missing:
        raise BuildError(
            f"structural_signature 引用了 body 里不存在的字段：{missing}",
            code="signature_out_of_sync",
            context=",".join(missing),
        )
    params = body["parameters"]
    char_prompts = params["characterPrompts"]
    char_captions = params["v4_prompt"]["caption"]["char_captions"]
    same = len(char_captions) == len(char_prompts) and all(
        cc.get("char_caption") == cp.get("prompt") for cc, cp in zip(char_captions, char_prompts)
    )
    return {
        "model": body["model"],
        "action": body["action"],
        "top_keys": sorted(body.keys()),
        "parameter_keys": sorted(params.keys()),
        "params_version": params.get("params_version"),
        "n_roles": len(char_prompts),
        "double_write_ok": bool(same),
        "referenced": [".".join(p) for p in _SIGNATURE_PATHS],
    }


def body_to_json(body: Mapping[str, Any]) -> str:
    """打印件：与构造同一份 body 的另一种输出（旧 _ref_payload.py 折进 build）。"""

    return json.dumps(body, ensure_ascii=False)


# --------------------------------------------------------------------- build


def build(req: Any, cache_view: Mapping[str, Any] | None = None) -> tuple[dict, dict, AppearanceFragment]:
    cache: Mapping[str, Any] = cache_view or {}

    size = str(_field(req, "size", "832x1216"))
    try:
        width, height = (int(x) for x in size.split("x"))
    except Exception as exc:  # noqa: BLE001
        raise BuildError(
            f"size 不合法：{size!r}", code="size_invalid", context=f"req.size={size!r}"
        ) from exc

    raw_seed = _field(req, "seed", "")
    if not str(raw_seed).strip():
        raise BuildError(
            "seed 未给定：build 是纯函数，不读全局随机源；随机种子由调用方注入 req.seed",
            code="seed_missing",
            context=f"req.seed={raw_seed!r}",
        )
    seed = _to_int(raw_seed, "seed")

    negative = str(_field(req, "negative", "") or "")
    other_val = _to_int(_field(req, "other", "0"), "other")
    parameters: dict[str, Any] = {
        "params_version": 3,
        "width": width,
        "height": height,
        "steps": _to_int(_field(req, "steps", "23"), "steps"),
        "scale": _to_float(_field(req, "scale", "5"), "scale"),
        "seed": seed,
        "sampler": _field(req, "sampler", "k_euler_ancestral"),
        "noise_schedule": _field(req, "noise_schedule", "karras"),
        "negative_prompt": negative,
        "cfg_rescale": _to_float(_field(req, "cfg", "0"), "cfg"),
        "sm": other_val in (2, 3, 4, 5),
        "sm_dyn": other_val in (3, 5),
        "n_samples": 1,
        "ucPreset": 0,
        "qualityToggle": True,
        "dynamic_thresholding": False,
        "controlnet_strength": 1,
        "legacy": False,
        "add_original_image": True,
        "legacy_v3_extend": False,
        "skip_cfg_above_sigma": None,
        "use_coords": False,
        "use_order": True,
        "characterPrompts": [],
        "v4_prompt": {
            "caption": {"base_caption": "", "char_captions": []},
            "use_coords": False,
            "use_order": True,
        },
        "v4_negative_prompt": {"caption": {"base_caption": negative, "char_captions": []}},
        "reference_image_multiple": [],
        "reference_information_extracted_multiple": [],
        "reference_strength_multiple": [],
        "normalize_reference_strength_multiple": True,
    }

    artist = str(_field(req, "artist", "") or "")
    base = str(_field(req, "tag", "") or "")
    prompt = f"{artist}, {base}" if (artist and base) else (artist or base)
    # 画风 b_tag（/nai画风 里的风格tag预设）：由解析层按 uid 塞进 req，这里统一追加去重
    prompt = _append_style_tags(prompt, str(_field(req, "b_tag", "") or ""))

    roles_in = _field(_field(req, "addition", None), "multi_role_list", []) or []
    roles = [_build_role(r) for r in roles_in if str(_field(r, "prompt", "") or "").strip()]

    per_role, injected, reviewed = _backfill(roles, cache)
    overridden = str(cache.get("overridden") or OVERRIDDEN_NONE)
    if overridden not in (OVERRIDDEN_NONE, OVERRIDDEN_USER_COLOR):
        raise BuildError(
            f"overridden 取值非法：{overridden!r}",
            code="build_failed",
            context=f"cache_view.overridden={overridden!r}",
        )

    if roles:
        char_prompts: list[dict[str, Any]] = []
        char_captions: list[dict[str, Any]] = []
        role_tags: list[str] = []
        used: list[tuple[float, float]] = []
        for idx, role in enumerate(roles):
            x, y = _multi_role_center(role.position, idx, len(roles))
            guard = 0
            explicit = bool(re.fullmatch(r"[A-E][1-5]", role.position.upper()))
            while not explicit and any(abs(x - ux) < 1e-6 and abs(y - uy) < 1e-6 for ux, uy in used) and guard < 4:
                x = min(0.9, round(x + 0.2, 3))
                if any(abs(x - ux) < 1e-6 and abs(y - uy) < 1e-6 for ux, uy in used):
                    x = max(0.1, round(x - 0.4, 3))
                guard += 1
            used.append((x, y))
            center = {"x": x, "y": y}
            text = render_character_prompt(role)
            char_prompts.append({"prompt": text, "uc": role.uc, "center": dict(center)})
            char_captions.append({"char_caption": render_char_caption(role), "centers": [dict(center)]})
            role_tags.extend(_character_tags_in(text))
        parameters["characterPrompts"] = char_prompts
        parameters["v4_prompt"]["caption"]["char_captions"] = char_captions

        use_coords = any(re.fullmatch(r"[A-E][1-5]", role.position.upper())
                         for role in roles)
        parameters["use_coords"] = use_coords
        parameters["v4_prompt"]["use_coords"] = use_coords
        parameters["v4_negative_prompt"]["caption"]["char_captions"] = [
            {"char_caption": role.uc, "centers": cc["centers"]}
            for role, cc in zip(roles, char_captions)
        ]
        if role_tags:
            prompt = _strip_tags(prompt, role_tags)

    final_prompt = base if _field(req, "preserve_prompt", False) else _dedupe_artist_tags(prompt)
    body = {
        "input": final_prompt,
        "model": _field(req, "model", ""),
        "action": "generate",
        "parameters": parameters,
    }
    parameters["v4_prompt"]["caption"]["base_caption"] = final_prompt

    # 发前自检：双写必须逐字相等，不等即拒发。
    for i, (cc, cp) in enumerate(
        zip(parameters["v4_prompt"]["caption"]["char_captions"], parameters["characterPrompts"])
    ):
        if cc["char_caption"] != cp["prompt"]:
            raise BuildError(
                f"第 {i} 个角色双写不一致（char_caption != prompt），拒发",
                code="double_write_mismatch",
                context=f"role_index={i}",
            )
    if parameters["v4_negative_prompt"]["caption"]["base_caption"] != parameters["negative_prompt"]:
        raise BuildError(
            "negative 双写不一致，拒发",
            code="double_write_mismatch",
            context="v4_negative_prompt.caption.base_caption != parameters.negative_prompt",
        )

    fragment = AppearanceFragment(
        source=_appearance_source(per_role),
        injected=tuple(_dedupe_keep_order(injected)),
        overridden=overridden,
        reviewed=reviewed,
    )

    log_payload = {
        "model": body["model"],
        "action": body["action"],
        "seed": seed,
        "width": width,
        "height": height,
        "steps": parameters["steps"],
        "n_roles": len(parameters["characterPrompts"]),
        "body": _sanitize_for_log(body),
        "appearance": fragment.as_dict(),
        "structure": structural_signature(body),
    }
    return body, log_payload, fragment
