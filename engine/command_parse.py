"""Split the first standalone /UC token, then parse orientation on the positive side."""

from __future__ import annotations

import re
from typing import Any, Final

COMMAND: Final = "nai"
UC_TOKEN: Final = "/UC"
UC_ENABLED: Final = True  # 2026-10-24 用户确认要「纯 tag 直出 + /UC 当负面」，负向链路已就绪

# 横竖板内联参数：`--l`/`--land`/`--landscape`/`--横`/`--横板` = 横板，
# `--p`/`--portrait`/`--竖`/`--竖板` = 竖板。从 prompt 里剥掉，orientation 传给解析层换尺寸。
_LANDSCAPE: Final = frozenset({"l", "land", "landscape", "横", "横板"})
_FLAG_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_])\s*--(landscape|land|l|portrait|p|横板|横|竖板|竖)(?=\s|$)",
    re.IGNORECASE,
)

# 开头方向词（2026-10-24 用户口径）：`/nai 横板 xxx`、`/nai 方形 xxx`、`/nai 竖版 xxx`，
# 不写默认竖版（用配置尺寸）。必须是命令词后的**第一个独立 token**，连写不识别
# （「横板桥上的人」这类正文不受影响）。
_HEAD_WORDS: Final[dict[str, str]] = {
    # 横：横板/横版/横图/横屏 都是同音近义写法，实际用户三种都写过
    "横板": "landscape", "横版": "landscape", "横图": "landscape",
    "横屏": "landscape", "横": "landscape", "landscape": "landscape", "l": "landscape",
    "方形": "square", "方版": "square", "正方形": "square", "方": "square",
    "square": "square", "s": "square",
    "竖版": "portrait", "竖板": "portrait", "竖图": "portrait", "竖屏": "portrait",
    "竖": "portrait", "portrait": "portrait", "p": "portrait",
}


def _extract_head_orientation(rest: str) -> tuple[str, str]:
    """剥掉开头的方向词（横板/方形/竖版），返回 (剩余文本, orientation)。

    只认第一个 token 精确等于方向词——`/nai 横板 1girl` 生效，
    `/nai 横板的女孩`（连写）不生效，正文里的「横板」不会误伤。
    """

    parts = rest.split(None, 1)
    if not parts:
        return rest, ""
    head = parts[0].strip().lower()
    orientation = _HEAD_WORDS.get(head)
    if not orientation:
        return rest, ""
    return (parts[1].strip() if len(parts) > 1 else ""), orientation

# 空正向提示：同属「命令解析层提示」这一类文案（第三类来源），逐字给、由入口层逐字用。
# 不进 `error_map`、不进 `receipt_render._COPY`（那不是错误、也不是降级）。
EMPTY_PROMPT: Final = "正向为空，请提供内容"


_UC_RE: Final = re.compile(r"(?<!\S)/uc(?=\s|$)", re.IGNORECASE)


def _extract_orientation(text: str) -> tuple[str, str]:
    """剥掉 `--l`/`--横` 这类横竖板内联参数；返回 (剩余文本, orientation)。

    同一条输入里出现两个冲突 flag 时按先出现的算（后一个只剥掉不生效）。
    """

    state = {"orientation": ""}

    def _sub(m: re.Match[str]) -> str:
        token = m.group(1).lower()
        if not state["orientation"]:
            state["orientation"] = "landscape" if token in _LANDSCAPE else "portrait"
        return ""

    rest = _FLAG_RE.sub(_sub, text)
    return rest.strip(), state["orientation"]


def parse_command_text(text: str) -> dict[str, Any]:
    """Parse command text while preserving the negative side verbatim."""

    body = str(text or "").strip()
    if body.startswith("/"):
        body = body[1:].lstrip()
    if not body:
        return {
            "command": "",
            "prompt": "",
            "uc": None,
            "uc_enabled": UC_ENABLED,
            "warning": None,
            "orientation": "",
        }

    # 按任意空白切一次（不只是半角空格）：`/nai\ta`、`/nai   a` 这类输入不该被当成空正向
    parts = body.split(None, 1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    m = _UC_RE.search(rest)
    uc = rest[m.end():].strip() if m is not None else None
    rest = rest[:m.start()].strip() if m is not None else rest
    # The first positive head word wins, but all positive flags are consumed.
    rest, orientation = _extract_head_orientation(rest)
    rest, inline_orientation = _extract_orientation(rest)
    orientation = orientation or inline_orientation
    return {
        "command": head,
        "prompt": rest.strip(),
        "uc": uc,
        "uc_enabled": UC_ENABLED,
        "warning": None,
        "orientation": orientation,
    }


__all__ = [
    "COMMAND",
    "EMPTY_PROMPT",
    "UC_TOKEN",
    "UC_ENABLED",
    "parse_command_text",
]
