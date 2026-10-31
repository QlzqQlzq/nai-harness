"""Platform-independent commands and generation lifecycle."""
import asyncio
import base64
import copy
import io
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, PngImagePlugin

from .config import NaiConfig
from .autobuy import AutoBuy
from .engine.build import BuildError, build
from .engine.command_parse import parse_command_text
from .parsing import Parser
from .routing import Budget, Router, ServiceError
from .services import Services
from .state import Slots, State, UserError, finish_before_cancel


@dataclass
class Context:
    stream_id: str
    user_id: str
    group_id: str = ""
    message_id: str = ""
    reply_id: str = ""
    images: list = field(default_factory=list)
    platform: str = "qq"

    @property
    def scope(self):
        return self.platform + (":g:" + self.group_id if self.group_id else ":u:" + self.user_id)

    @property
    def owner(self):
        return self.platform + ":" + self.user_id


ERRORS = {"quota": "上游额度不足", "auth": "上游认证失败", "rate_limit": "上游限流",
          "timeout": "上游超时", "server": "上游服务器错误", "budget": "尝试次数或总时限已达上限",
          "invalid_response": "上游响应无效或缺少图片", "unconfigured": "当前内容模式没有可用服务，请管理员检查URL和key配置",
          "bad_request": "上游拒绝参数或接口配置不匹配"}


class Runtime:
    def __init__(self, config, data_dir, transport, client=None):
        self.config = config if isinstance(config, NaiConfig) else NaiConfig.model_validate(config)
        self.state = State(data_dir, self.config.quota.crash_recovery)
        self.transport = transport
        self.router = Router(self.config.network, client)
        if self.config.storage.persist_key_health:
            self.router.health = {tuple(json.loads(k)): float("inf") if v is None else v
                                  for k, v in self.state.data.get("key_health", {}).items()}
            self.router.save_health = self.save_health
        self.services = Services(self.config, self.router)
        self.services.autobuy = AutoBuy(self.state, self.router)
        self.parser = Parser(self.config, self.services, data_dir)
        self.slots = Slots(self.config.queue)
        self.tasks, self.pending = {}, {}
        self.closing = False
        self.history = self.state.data.setdefault("history", {})
        self._content_words = list(self.config.content.nsfw_terms)
        if self.config.content.wordlist_path:
            self._content_words += (Path(data_dir) / self.config.content.wordlist_path).read_text(encoding="utf-8-sig").splitlines()

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.router.close()
        db = self.parser.tools.db
        if db is not None:
            await asyncio.to_thread(db.close)

    async def save_health(self):
        async with self.state.transaction():
            self.state.data["key_health"] = {json.dumps(k): None if v == float("inf") else v
                                             for k, v in self.router.health.items() if v > time.time()}

    def check_permission(self, ctx, feature, management=False):
        c = self.config.permissions
        if not ctx.stream_id or not ctx.user_id:
            raise UserError("无法确认当前会话或用户身份。")
        if ctx.user_id in c.blocked_users or (c.allowed_users and ctx.user_id not in c.allowed_users):
            raise UserError("当前用户没有使用权限。")
        if ctx.group_id:
            if not c.group_enabled or ctx.group_id in c.blocked_groups or (c.allowed_groups and ctx.group_id not in c.allowed_groups):
                raise UserError("当前群未开放此功能。")
            group = c.groups.get(ctx.group_id)
            if group and (not group.enabled or feature not in group.features):
                raise UserError("当前群未开放此功能。")
        elif not c.private_enabled or feature not in c.private_features:
            raise UserError("私聊未开放此功能。")
        if management:
            allowed = ctx.user_id in c.admin_qq or (ctx.group_id and ctx.user_id in c.group_managers.get(ctx.group_id, []))
            if not allowed and not (not ctx.group_id and c.private_manage):
                raise UserError("没有修改当前会话设置的权限。")

    def nsfw(self, ctx):
        default = self.config.content.group_nsfw if ctx.group_id else self.config.content.private_nsfw
        group = self.config.permissions.groups.get(ctx.group_id)
        if group is not None and group.nsfw is not None:
            default = group.nsfw
        return bool(self.state.get("nsfw", ctx.scope, default))

    def inspect(self, text, nsfw):
        terms = self.config.content.blocked_terms + ([] if nsfw else self._content_words)
        for term in terms:
            term = term.strip()
            if not term:
                continue
            pattern = re.escape(term) if re.search(r"[^\x00-\x7f]", term) else r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])"
            if re.search(pattern, text, re.I):
                raise UserError("内容不符合当前会话设置，本次未提交。")

    def check_mode(self, ctx, expected):
        if self.nsfw(ctx) != expected:
            raise UserError("当前会话的 NSFW 设置已更改，任务已停止，请重新提交。")

    async def say(self, ctx, text):
        return await self.transport.text(ctx, text, self.config.output.quote_reply)

    async def say_after_generation(self, ctx, text):
        try:
            await self.say(ctx, text)
        except Exception as exc:
            logging.getLogger(__name__).warning("NAI receipt delivery failed (%s)", type(exc).__name__)

    def parse_entry(self, text):
        p = self.config.plugin
        if not text.startswith(p.prefix):
            return None
        body = text[len(p.prefix):]
        names = [(n, "uc") for n in p.uc_names] + [(n, "nai") for n in p.command_names]
        for name, kind in sorted(names, key=lambda pair: len(pair[0]), reverse=True):
            if body[:len(name)].casefold() == name.casefold() and (len(body) == len(name) or body[len(name)].isspace()):
                return kind, body[len(name):].strip()
            if kind == "nai" and body.casefold().startswith(name.casefold()):
                rest = body[len(name):]
                if any(rest == word or (rest.startswith(word) and rest[len(word):len(word) + 1].isspace())
                       for word in ("反推", "重画", "模型", "画风", "额度", "状态")):
                    return kind, rest
        return None

    async def handle(self, ctx, text, natural=False):
        if not self.config.plugin.enabled or self.closing:
            return False
        if natural:
            if not self.config.modules.natural_tool:
                return False
            parsed = ("nai", text)
        else:
            parsed = self.parse_entry(text)
        if parsed is None:
            return False
        try:
            kind, args = parsed
            if len(text) > self.config.plugin.max_input_chars:
                raise UserError("输入超过配置长度限制。")
            if natural:
                await self._draw_task(ctx, args)
                return True
            if kind == "uc":
                self.check_permission(ctx, "uc")
                if not self.config.modules.uc:
                    raise UserError("负面词模块已关闭。")
                if args:
                    await self.state.set("uc", ctx.owner, "" if args.casefold() in ("clear", "清空") else args)
                await self.say(ctx, "个人负面词：" + (self.state.get("uc", ctx.owner, "") or "未设置"))
                return True
            pieces = args.split(None, 1)
            sub, rest = (pieces[0], pieces[1] if len(pieces) > 1 else "") if pieces else ("", "")
            sub = sub.casefold()
            if sub in ("帮助", "help", ""):
                self.check_permission(ctx, "status")
                help_text = "/nai 内容 [/uc 负面词]\n/nai 模型 名称\n/nai nsfw on|off\n/nai 画风 [名称或tag]\n/nai 反推（引用图片）\n/nai 重画 [新提示词]\n/nai 状态\n/nai 取消\n/uc 负面词|清空"
                help_text = help_text.replace("/nai", self.config.plugin.prefix + self.config.plugin.command_names[0])
                if self.config.plugin.uc_names:
                    help_text = help_text.replace("/uc", self.config.plugin.prefix + self.config.plugin.uc_names[0])
                await self.say(ctx, help_text)
            elif sub in ("模型", "model", "nsfw"):
                feature = "model" if sub in ("模型", "model") else "nsfw"
                self.check_permission(ctx, feature, management=bool(rest))
                if feature == "model":
                    if rest and rest not in self.config.generation.allowed_models:
                        raise UserError("可选模型：" + ", ".join(self.config.generation.allowed_models))
                    if rest:
                        await self.state.set("model", ctx.scope, rest)
                    group = self.config.permissions.groups.get(ctx.group_id)
                    default = (group.model if group else None) or self.config.generation.model
                    await self.say(ctx, "当前模型：" + self.state.get("model", ctx.scope, default))
                else:
                    if rest and rest.lower() not in ("on", "off"):
                        raise UserError("用法：/nai nsfw on|off")
                    if rest:
                        await self.state.set("nsfw", ctx.scope, rest.lower() == "on")
                    await self.say(ctx, "NSFW：" + ("on" if self.nsfw(ctx) else "off"))
            elif sub in ("画风", "style"):
                self.check_permission(ctx, "styles")
                if not self.config.modules.styles:
                    raise UserError("画风模块已关闭。")
                if rest:
                    if not self.config.styles.allow_personal:
                        raise UserError("个人画风修改已关闭。")
                    value = "" if rest in ("清空", "clear") else self.config.styles.templates.get(rest, rest)
                    await self.state.set("styles", ctx.owner, value)
                await self.say(ctx, "个人画风：" + (self.state.get("styles", ctx.owner, "") or "默认") + "\n模板：" + ", ".join(self.config.styles.templates))
            elif sub in ("取消", "cancel"):
                self.check_permission(ctx, "status")
                task = self.tasks.get((ctx.stream_id, ctx.user_id))
                if task:
                    task.cancel()
                await self.say(ctx, "已请求取消。" if task else "当前没有自己的活动任务。")
            elif sub in ("恢复key", "reset-key"):
                self.check_permission(ctx, "status")
                if ctx.user_id not in self.config.permissions.admin_qq:
                    raise UserError("仅全局管理员可恢复key。")
                service, _, key = rest.partition(" ")
                restored = self.router.restore(service, key)
                if self.config.storage.persist_key_health:
                    await self.save_health()
                await self.say(ctx, "已恢复 %d 项key状态。" % restored)
            elif sub in ("恢复补购", "reset-purchase"):
                self.check_permission(ctx, "status")
                if ctx.user_id not in self.config.permissions.admin_qq:
                    raise UserError("仅全局管理员可恢复补购。")
                found = False
                for pool in (self.config.drawing.sfw, self.config.drawing.nsfw):
                    for endpoint in pool.endpoints:
                        if endpoint.name == rest and endpoint.auto_buy.enabled:
                            identity = AutoBuy.identity(endpoint.auto_buy)
                            record = dict(self.state.get("autobuy", identity, {}))
                            record["uncertain"] = False
                            await self.state.set("autobuy", identity, record)
                            found = True
                await self.say(ctx, "已解除补购待确认状态；每日上限与冷却保留。" if found else "未找到启用补购的服务名。")
            elif sub in ("状态", "额度", "status"):
                self.check_permission(ctx, "status")
                info = {"active": dict(self.slots.active), "waiting": self.slots.waiting,
                        "local_usage": self.state.get("usage", "u:" + ctx.owner, 0), "nsfw": self.nsfw(ctx)}
                if ctx.user_id in self.config.permissions.admin_qq:
                    info["key_status"] = self.router.status()
                await self.say(ctx, json.dumps(info, ensure_ascii=False))
            elif sub in ("反推", "reverse"):
                self.check_permission(ctx, "reverse")
                if not self.config.modules.reverse:
                    raise UserError("反推模块已关闭。")
                await self._run_task(ctx, self._reverse(ctx))
            elif sub in ("重画", "redraw"):
                self.check_permission(ctx, "redraw")
                if not self.config.modules.redraw:
                    raise UserError("重画模块已关闭。")
                old = self.lookup_history(ctx)
                if not old:
                    raise UserError("未找到当前会话可用的引用生成记录，请引用本插件生成的图片。")
                await self._draw_task(ctx, rest or old["text"], inherited=old)
            elif args.isdigit() and (ctx.stream_id, ctx.user_id) in self.pending:
                pending = self.pending[(ctx.stream_id, ctx.user_id)]
                if time.monotonic() > pending["until"]:
                    raise UserError("候选已过期，请重新输入。")
                index = int(args) - 1
                if not 0 <= index < len(pending["candidates"]):
                    raise UserError("候选编号不正确。")
                self.pending.pop((ctx.stream_id, ctx.user_id), None)
                await self._draw_task(ctx, pending["text"] + "，角色明确为 " + pending["candidates"][index])
            else:
                await self._draw_task(ctx, args)
        except UserError as exc:
            await self.say(ctx, str(exc))
        except ServiceError as exc:
            await self.say(ctx, "处理失败：" + ERRORS.get(exc.category, "服务错误") + "。")
        except asyncio.TimeoutError:
            await self.say(ctx, "处理超时，本次任务已结束。")
        except asyncio.CancelledError:
            if not self.closing:
                await self.say(ctx, "任务已取消。")
        except BuildError:
            await self.say(ctx, "生成参数或角色结构无效，本次未提交。")
        except Exception as exc:
            logging.getLogger(__name__).error("NAI request failed (%s)", type(exc).__name__)
            await self.say(ctx, "处理失败，请管理员查看本地诊断记录。")
        return True

    async def _run_task(self, ctx, operation):
        key = ctx.stream_id, ctx.user_id
        if key in self.tasks:
            operation.close()
            raise UserError("当前会话中你的上一项任务仍在处理中。")
        async def execute():
            async with self.slots.acquire(ctx):
                if self.config.output.progress:
                    await self.say(ctx, self.config.output.progress_text)
                return await operation
        task = asyncio.create_task(asyncio.wait_for(execute(), self.config.queue.task_timeout))
        self.tasks[key] = task
        try:
            return await task
        finally:
            operation.close()
            self.tasks.pop(key, None)

    async def _draw_task(self, ctx, text, inherited=None):
        self.check_permission(ctx, "draw")
        if not self.config.modules.draw:
            raise UserError("出图模块已关闭。")
        await self._run_task(ctx, self._draw(ctx, text, inherited))

    async def _draw(self, ctx, text, inherited=None):
        c, nsfw = self.config, self.nsfw(ctx)
        self.inspect(text, nsfw)
        overrides = {}
        def parameter(match):
            key, value = match.groups()
            if key not in c.generation.overrides:
                raise UserError("不允许覆盖参数：" + key)
            overrides[key] = value
            return ""
        clean = re.sub(r"(?<!\S)--([a-z_]+)=([^\s]+)", parameter, text)
        aliases = "|".join(re.escape(n) for n in c.plugin.uc_names)
        if aliases:
            clean = re.sub(r"(?<!\S)" + re.escape(c.plugin.prefix) + "(?:" + aliases + r")(?=\s|$)",
                           "/uc", clean, flags=re.I)
        parsed = parse_command_text("/nai " + clean)
        uc = parsed["uc"] if parsed["uc"] is not None else self.state.get("uc", ctx.owner, "")
        if not c.modules.uc:
            if parsed["uc"] is not None:
                raise UserError("负面词模块已关闭。")
            uc = ""
        group = c.permissions.groups.get(ctx.group_id)
        model = self.state.get("model", ctx.scope, (group.model if group else None) or c.generation.model)
        if inherited and c.redraw.inherit_model:
            model = inherited.get("req", {}).get("model", model)
        if model not in c.generation.allowed_models:
            raise UserError("保存的模型已不在允许列表，请重新选择。")
        style = (self.state.get("styles", ctx.owner, "") or c.styles.default) if c.modules.styles else ""
        self.pending.pop((ctx.stream_id, ctx.user_id), None)
        resolution = await self.parser.resolve(parsed["prompt"], uc, model, ctx.owner, style, nsfw, parsed["orientation"])
        if resolution["status"] != "ok":
            if resolution.get("candidates"):
                self.pending = {k: v for k, v in self.pending.items() if v["until"] > time.monotonic()}
                if len(self.pending) >= c.parser.pending_limit:
                    self.pending.pop(next(iter(self.pending)))
                self.pending[(ctx.stream_id, ctx.user_id)] = {"text": text, "candidates": resolution["candidates"], "until": time.monotonic() + c.parser.candidate_ttl}
            raise UserError(resolution.get("reason") or "未确认提示词，本次未生成。")
        req = resolution["req"]
        if inherited and c.redraw.inherit_parameters:
            for k in ("size", "steps", "scale", "cfg", "sampler", "noise_schedule", "other"):
                req[k] = inherited.get("req", {}).get(k, req.get(k))
        if inherited and c.redraw.inherit_seed:
            req["seed"] = inherited.get("req", {}).get("seed", req["seed"])
        for k, v in overrides.items():
            try:
                req[k] = int(v) if k in ("seed", "steps", "other") else float(v) if k in ("scale", "cfg") else v
            except ValueError as exc:
                raise UserError("参数格式不正确：" + k) from exc
        try:
            w, h = map(int, req["size"].split("x"))
            if min(w, h) < 64 or w % 64 or h % 64 or w * h > c.generation.max_pixels:
                raise ValueError("dimensions")
            if not 1 <= req["steps"] <= 100 or not 0 <= req["scale"] <= 30 or not 0 <= req["cfg"] <= 1:
                raise ValueError("parameters")
            if not all(math.isfinite(req[k]) for k in ("scale", "cfg")) or not 0 <= req["other"] <= 5:
                raise ValueError("parameters")
            if not 0 <= req["seed"] <= 2**32 - 1:
                raise ValueError("seed")
        except (ValueError, KeyError, TypeError) as exc:
            raise UserError("生成参数超出允许范围，尺寸须为64的倍数。") from exc
        roles = req.get("addition", {}).get("multi_role_list", [])
        if len(roles) > c.generation.max_roles:
            raise UserError("角色数量超过上限。")
        self.inspect(req["tag"] + " " + " ".join(r.get("prompt", "") for r in roles) + " " + req.get("artist", ""), nsfw)
        self.check_mode(ctx, nsfw)
        body, _, _ = build(req, {})
        if c.plugin.dry_run:
            await self.say(ctx, "仅解析，未调用出图接口：\n" + json.dumps(body, ensure_ascii=False))
            return
        reservation = await self.state.reserve(c.quota, ctx.owner, ctx.scope if ctx.group_id else "", model)
        generated = False
        try:
            pool = c.drawing.nsfw if nsfw else c.drawing.sfw
            budget = Budget(pool.max_attempts, time.monotonic() + pool.total_timeout)
            while True:
                try:
                    self.check_mode(ctx, nsfw)
                    image = await self.services.draw(body, nsfw, budget, lambda: self.check_mode(ctx, nsfw))
                    break
                except ServiceError as exc:
                    lower = c.generation.fallback_models.get(body["model"])
                    if not lower or exc.category not in c.generation.fallback_errors:
                        raise
                    await self.state.refund(reservation)
                    reservation = None
                    reservation = await self.state.reserve(c.quota, ctx.owner, ctx.scope if ctx.group_id else "", lower)
                    req["model"] = lower
                    body, _, _ = build(req, {})
            generated = True
            try:
                image = await asyncio.to_thread(self.decorate, image, body)
            except Exception as exc:
                logging.getLogger(__name__).error("NAI image processing failed (%s)", type(exc).__name__)
                raise UserError("图片已生成，但本地图片处理失败；不会自动重复生图。") from exc
            token = uuid.uuid4().hex
            image_path = ""
            if c.storage.save_images:
                path = self.state.directory / "images" / (token + ".png")
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(path.write_bytes, image)
                    image_path = str(path.name)
                except OSError as exc:
                    logging.getLogger(__name__).warning("NAI image persistence failed (%s)", type(exc).__name__)
            sent = False
            message_id = ""
            for attempt in range(c.output.send_retries + 1):
                self.check_mode(ctx, nsfw)
                try:
                    result = await self.transport.image(ctx, base64.b64encode(image).decode(), c.output.quote_reply)
                    sent = bool(result.get("sent")) if isinstance(result, dict) else bool(result)
                    message_id = str(result.get("message_id") or "") if isinstance(result, dict) else ""
                except Exception:
                    sent = False
                if sent:
                    break
                if attempt < c.output.send_retries:
                    await asyncio.sleep(c.output.send_interval)
            try:
                await self.remember(ctx, token, message_id, clean, req, image_path)
            except Exception as exc:
                logging.getLogger(__name__).warning("NAI history persistence failed (%s)", type(exc).__name__)
            if not sent:
                if c.quota.refund_delivery_failure:
                    await self.state.refund(reservation)
                    reservation = None
                raise UserError("图片已生成，但消息发送失败；不会自动重复生图。")
            if c.output.mode != "image":
                receipt = "已生成：" + body["model"]
                if c.output.show_route:
                    attempts = self.router.last_attempts.get()
                    if attempts:
                        receipt += "\n路由：" + attempts[-1]["service"] + "/" + attempts[-1]["key"]
                if c.output.show_tags or c.output.mode == "detailed":
                    receipt += "\n" + body["input"]
                await self.say_after_generation(ctx, receipt)
        finally:
            try:
                if not generated:
                    await finish_before_cancel(self.state.refund(reservation))
                else:
                    await finish_before_cancel(self.state.commit(reservation))
            except Exception as exc:
                logging.getLogger(__name__).error("NAI quota settlement failed (%s)", type(exc).__name__)
                if not generated:
                    raise
                await self.say_after_generation(ctx, "图片已生成，但本地额度记录保存失败，请管理员检查。")
            try:
                await self.log(text, generated)
            except Exception as exc:
                logging.getLogger(__name__).warning("NAI log write failed (%s)", type(exc).__name__)

    async def _reverse(self, ctx):
        nsfw = self.nsfw(ctx)
        images = await self.transport.images(ctx, self.config.reverse.quote_first)
        if not images:
            raise UserError("未能读取引用或附件图片，请确认引用的是图片消息。")
        loaded = []
        for item in images[:self.config.reverse.max_images]:
            self.check_mode(ctx, nsfw)
            if isinstance(item, bytes):
                loaded.append(self.services.verify_image(item))
            elif isinstance(item, str) and item.startswith(("https://", "http://")):
                loaded.append(await self.services.download(item))
            elif isinstance(item, str):
                try:
                    encoded = item.partition(",")[2] if item.startswith("data:") else item.removeprefix("base64://")
                    if len(encoded) > self.config.network.max_image_bytes * 1.4:
                        raise ValueError("too large")
                    loaded.append(self.services.verify_image(base64.b64decode(encoded, validate=True)))
                except ValueError as exc:
                    raise UserError("图片数据格式无效。") from exc
        if not loaded:
            raise UserError("图片格式不受支持，本次未反推。")
        self.check_mode(ctx, nsfw)
        tags = await self.services.reverse(loaded, nsfw, lambda: self.check_mode(ctx, nsfw))
        self.check_mode(ctx, nsfw)
        self.inspect(tags, nsfw)
        await self.say(ctx, tags)

    def decorate(self, data, body):
        c = self.config.storage
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            if c.watermark:
                try:
                    ImageDraw.Draw(image).text((8, image.height - 22), c.watermark, fill="white", stroke_width=1, stroke_fill="black")
                except Exception as exc:
                    logging.getLogger(__name__).warning("NAI watermark failed (%s)", type(exc).__name__)
            meta = PngImagePlugin.PngInfo()
            if c.metadata:
                meta.add_text("nai_parameters", json.dumps(body, ensure_ascii=False))
            out = io.BytesIO()
            image.save(out, format="PNG", pnginfo=meta)
            return out.getvalue()

    async def remember(self, ctx, token, message_id, text, req, image_path):
        c = self.config.storage
        if not c.history_seconds:
            return
        async with self.state.transaction():
            self.history[token] = {"stream": ctx.stream_id, "uid": ctx.user_id, "message_id": message_id,
                "text": text, "req": req, "image": image_path, "time": time.time()}
            while len(self.history) > c.history_limit:
                self.history.pop(next(iter(self.history)))

    def lookup_history(self, ctx):
        for value in reversed(list(self.history.values())):
            if value["stream"] != ctx.stream_id or time.time() - value["time"] > self.config.storage.history_seconds:
                continue
            if value["uid"] != ctx.user_id and not self.config.redraw.allow_other_users:
                continue
            if ctx.reply_id and value["message_id"] == ctx.reply_id:
                return copy.deepcopy(value)
            if not ctx.reply_id and self.config.redraw.allow_last_without_quote and value["uid"] == ctx.user_id:
                return copy.deepcopy(value)
        return None

    async def log(self, text, generated):
        if not self.config.storage.logs_enabled:
            return
        record = {"time": time.time(), "generated": generated, "attempts": list(self.router.last_attempts.get())}
        if self.config.storage.log_prompts:
            record["prompt"] = text
        path = self.state.directory / ("events-" + time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
        def write():
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        await asyncio.to_thread(write)

    async def cleanup(self):
        async with self.state.transaction():
            expired = [k for k, v in self.history.items()
                       if time.time() - v.get("time", 0) > self.config.storage.history_seconds]
            for key in expired:
                self.history.pop(key, None)
        def clean():
            now = time.time()
            for pattern, days in (("images/*.png", self.config.storage.image_days), ("events-*.jsonl", self.config.storage.log_days)):
                if days:
                    for path in self.state.directory.glob(pattern):
                        if path.is_file() and now - path.stat().st_mtime > days * 86400:
                            path.unlink()
            images = sorted(self.state.directory.glob("images/*.png"), key=lambda p: p.stat().st_mtime)
            sizes = [(p, p.stat().st_size) for p in images]
            total, count = sum(s for _, s in sizes), len(sizes)
            for path, size in sizes:
                if ((self.config.storage.max_image_files and count > self.config.storage.max_image_files)
                        or (self.config.storage.max_image_disk_bytes and total > self.config.storage.max_image_disk_bytes)):
                    path.unlink()
                    total, count = total - size, count - 1
                else:
                    break
        await asyncio.to_thread(clean)
