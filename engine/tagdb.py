# -*- coding: utf-8 -*-
"""本地 danbooru tag 索引（只读）：中文名反查、tag 存在性校验、别名规范化。

数据来自旧插件的 `nai_tags_new.sqlite3`（170 万 tag / 11k 中文映射 / 3.8 万别名），
**不复制、不修改**，只以只读方式打开（旧插件随时可继续用自己那份）。

为什么需要它（取代"靠提示词求模型别编"）：
  - 模型会编 `rosemary_(arknights)`（迷迭香直译）、`luo_qian_...`（拼音），
    本地一查 `tags` 表就知道不存在 —— 这是**确定性校验**，不是概率约束；
  - `zh` 表把中文名直接映射到官方 tag（迷迭香 → rosmontis_(arknights)），
    还带中文描述（含外观线索），比联网搜索快且不会串到同名的现实词条（香草）。

硬约束
  - **fail-soft**：库不存在/打不开/查询异常 → `available=False`，所有查询返回空，
    调用方自然退回联网搜索与模型判断（功能降级，不报错、不阻断出图）；
  - 只读 URI（`mode=ro`），绝不建表写库；
  - 连接复用 + 互斥锁（asyncio 单线程但可能并发调用）。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Final

__all__ = ["TagDB", "TAG_CATEGORY"]

#: Danbooru categories: 3=copyright, 4=character, 5=meta.
TAG_CATEGORY: Final = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "meta"}


def lookup_name(query: str) -> str:
    """Normalize a lookup key only; never apply this to the drawing prompt."""
    return re.sub(r"\s+", "_", str(query).strip().lower())


def lookup_candidates(query: str) -> list[str]:
    """Bounded exact keys from a search phrase, not fuzzy identity replacements."""
    names = [query.strip().lower(), lookup_name(query)]
    for phrase in re.split(r"[,，;；]", query):
        words = re.findall(r"\([^()]*\)|[^\s]+", phrase)[:32]
        names.append(lookup_name(phrase))
        for width in range(min(len(words), 8), 0, -1):
            names.extend(lookup_name(" ".join(words[i:i + width]))
                         for i in range(len(words) - width + 1))
    return list(dict.fromkeys(n for n in names if n))[:256]


class TagDB:
    """只读 TagDB 客户端。任何异常都降级为"没有索引"，不让调用方感知错误。"""

    def __init__(self, db_path: str | Path, logger: Any = None) -> None:
        self._path = Path(db_path) if db_path else None
        self._logger = logger
        self._lock = threading.RLock()
        self._con: sqlite3.Connection | None = None
        self._tried = False

    # ------------------------------------------------------------------ 连接

    @property
    def available(self) -> bool:
        return self._ensure() is not None

    def _ensure(self) -> sqlite3.Connection | None:
        with self._lock:
            return self._open_locked()

    def _open_locked(self) -> sqlite3.Connection | None:
        if self._con is not None:
            return self._con
        if self._tried:
            return None
        self._tried = True
        path = self._path
        if path is None or not Path(path).exists():
            self._warn("本地 tag 索引不存在（%s）：中文反查/存在性校验降级为不可用" % path)
            return None
        con = None
        try:
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
            con.execute("SELECT 1 FROM tags LIMIT 1")
            self._con = con
            return con
        except Exception as exc:  # noqa: BLE001  索引坏了当没有，别让出图挂掉
            if con is not None:
                con.close()
            self._warn("本地 tag 索引打不开（%s）：%s" % (path, exc))
            return None

    def close(self) -> None:
        """Wait for an active query before releasing the read-only connection."""
        with self._lock:
            self._tried = True
            if self._con is not None:
                self._con.close()
                self._con = None

    # ------------------------------------------------------------------ 查询

    def evidence(self, query: str) -> dict:
        """Bounded candidates for the agent, with query failures kept distinct."""
        con = self._ensure()
        if con is None:
            return {"status": "unavailable", "candidates": []}
        try:
            with self._lock:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                keys = lookup_candidates(str(query)[:240])
                if not keys:
                    return {"status": "empty", "candidates": []}
                placeholders = ",".join("?" for _ in keys)
                aliases = {}
                if "aliases" in tables:
                    aliases = dict(con.execute(
                        "SELECT antecedent, consequent FROM aliases WHERE antecedent IN (" + placeholders + ")", keys))
                resolved = list(dict.fromkeys([*keys, *aliases.values()]))
                rows = con.execute(
                    "SELECT name, category, post_count FROM tags WHERE name IN (" +
                    ",".join("?" for _ in resolved) + ")", resolved).fetchall()
                by_name = {row[0]: row for row in rows}
                hits, seen = [], set()
                for key in keys:
                    for name in (key, aliases.get(key)):
                        if name not in by_name or name in seen:
                            continue
                        row = by_name[name]
                        seen.add(name)
                        hits.append({"source": "tag", "name": row[0], "category": row[1],
                                     "post_count": row[2], "matched_query": key,
                                     "match": "whole" if key in {query.strip().lower(), lookup_name(query)} else "fragment",
                                     "alias": name != key})
                whole = [hit for hit in hits if hit["match"] == "whole"]
                hits = (whole or hits)[:6]
                if "zh" in tables:
                    select_zh = ("SELECT zh.name, zh.zh_tag, zh.zh_desc, tags.category "
                                 "FROM zh LEFT JOIN tags ON tags.name=zh.name WHERE ")
                    rows = con.execute(select_zh + "zh.zh_tag=? LIMIT 6", (query,)).fetchall()
                    exact = bool(rows)
                    if not rows:
                        # Escape SQL wildcards in user/LLM text.
                        needle = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                        rows = con.execute(select_zh + "zh.zh_tag LIKE ? ESCAPE '!' LIMIT 6", ("%" + needle + "%",)).fetchall()
                    hits += [{"source": "zh", "tag": r[0], "zh": r[1], "desc": str(r[2] or "")[:1600],
                              "category": r[3], "exact": exact} for r in rows]
            return {"status": "ok" if hits else "empty", "candidates": hits}
        except sqlite3.Error as exc:
            return {"status": "error", "error_type": type(exc).__name__, "candidates": []}

    def exists(self, tag: str) -> bool | None:
        """tag 是否在索引里。`None` = 索引不可用（调用方据此跳过校验，不当成"不存在"）。"""

        con = self._ensure()
        name = str(tag or "").strip().lower()
        if con is None or not name:
            return None
        try:
            with self._lock:
                row = con.execute("SELECT 1 FROM tags WHERE name = ? LIMIT 1", (name,)).fetchone()
            return row is not None
        except Exception as exc:  # noqa: BLE001
            self._warn("tag 存在性查询失败：%s" % exc)
            return None

    def info(self, tag: str) -> dict[str, Any] | None:
        """tag 详情（post_count / 分类）。"""

        con = self._ensure()
        name = str(tag or "").strip().lower()
        if con is None or not name:
            return None
        try:
            with self._lock:
                row = con.execute(
                    "SELECT name, category, post_count FROM tags WHERE name = ? LIMIT 1", (name,)
                ).fetchone()
            if row is None:
                return None
            return {"name": row[0], "category": row[1],
                    "category_name": TAG_CATEGORY.get(row[1], str(row[1])),
                    "post_count": row[2]}
        except Exception:  # noqa: BLE001
            return None

    def lookup_cn(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        """中文名 → 官方 tag（先精确、再包含匹配）。

        返回 `[{"tag","zh","desc","exact":bool}]`，按精确优先。
        """

        con = self._ensure()
        raw = str(name or "").strip()
        if con is None or not raw:
            return []
        out: list[dict[str, Any]] = []
        try:
            with self._lock:
                rows = con.execute(
                    "SELECT name, zh_tag, zh_desc FROM zh WHERE zh_tag = ? LIMIT ?",
                    (raw, limit),
                ).fetchall()
                exact = {r[0] for r in rows}
                if len(rows) < limit:
                    more = con.execute(
                        "SELECT name, zh_tag, zh_desc FROM zh WHERE zh_tag LIKE ? LIMIT ?",
                        ("%" + raw + "%", limit),
                    ).fetchall()
                    rows = list(rows) + [m for m in more if m[0] not in exact]
            seen = set()
            for tag, zh, desc in rows:
                if tag in seen:
                    continue
                seen.add(tag)
                out.append({"tag": tag, "zh": zh or "", "desc": desc or "",
                            "exact": tag in exact})
                if len(out) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001
            self._warn("中文反查失败：%s" % exc)
            return []
        return out

    def normalize(self, tag: str) -> str:
        """按 aliases 表把 tag 规范到当前写法（没有别名就原样返回）。"""

        con = self._ensure()
        name = str(tag or "").strip().lower()
        if con is None or not name:
            return name
        try:
            with self._lock:
                row = con.execute(
                    "SELECT consequent FROM aliases WHERE antecedent = ? LIMIT 1", (name,)
                ).fetchone()
            return str(row[0]) if row else name
        except Exception:  # noqa: BLE001
            return name

    def names_like(self, prefix: str, limit: int = 10, categories: tuple[int, ...] = (4,)) -> list[dict]:
        """Exact name and literal name-prefix candidates for ambiguity checks."""

        con = self._ensure()
        raw = str(prefix or "").strip().lower()
        if con is None or not raw:
            return []
        marks = ",".join("?" for _ in categories)
        needle = raw.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        try:
            with self._lock:
                rows = con.execute(
                    "SELECT name, category, post_count FROM tags "
                    "WHERE (name = ? OR name LIKE ? ESCAPE '!') AND category IN (%s) "
                    "ORDER BY post_count DESC LIMIT ?" % marks,
                    (raw, needle + "!_%", *categories, limit),
                ).fetchall()
            return [{"name": r[0], "category": r[1], "post_count": r[2]} for r in rows]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------ 内部

    def _warn(self, message: str) -> None:
        if self._logger is not None:
            try:
                self._logger.warning("[nai_core][tagdb] " + message)
            except Exception:  # noqa: BLE001
                pass
