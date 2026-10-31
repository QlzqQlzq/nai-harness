"""Modern MaiBot SDK entry point; business logic stays in runtime.py."""
import asyncio
import re
import secrets
import time

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import NaiConfig
from .config_ui import build_webui_schema
from .runtime import Runtime
from .state import UserError
from .transport import SdkTransport, command_context


class NaiPlugin(MaiBotPlugin):
    config_model = NaiConfig

    @classmethod
    def build_config_schema(cls, **metadata):
        return build_webui_schema(super().build_config_schema(**metadata))

    def __init__(self):
        super().__init__()
        self.runtime = None
        self._maintenance = None
        self._confirmations = {}
        self._handlers = set()
        self._lifecycle_lock = asyncio.Lock()
        self._accepting = False

    def get_components(self):
        c = self.config
        if not c.plugin.enabled:
            return []
        components = []
        for component in super().get_components():
            meta = component["metadata"]
            meta["timeout_ms"] = int((c.queue.task_timeout + 30) * 1000)
            if component["type"].lower() == "tool":
                if not c.modules.natural_tool or not c.modules.draw:
                    continue
                meta["description"] = meta["brief_description"] = c.natural.description
            else:
                names = c.plugin.command_names + c.plugin.uc_names
                alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                meta["command_pattern"] = (r"(?i)^" + re.escape(c.plugin.prefix) + "(?:" + alternatives
                    + r")(?=\s|$|反推(?:\s|$)|重画(?:\s|$)|模型(?:\s|$)|画风(?:\s|$)|额度(?:\s|$)|状态(?:\s|$))[\s\S]*$")
            components.append(component)
        return components

    async def on_load(self):
        async with self._lifecycle_lock:
            if self.runtime is not None:
                await self._stop_runtime()
            self._start_runtime()

    def _start_runtime(self):
        self.runtime = Runtime(self.config, self.ctx.paths.data_dir, SdkTransport(self.ctx))
        if self.config.plugin.enabled:
            self._maintenance = asyncio.create_task(self._maintain())
            self._accepting = True

    async def _maintain(self):
        while True:
            try:
                await self.runtime.cleanup()
            except Exception as exc:
                self.ctx.logger.warning("NAI cleanup failed: %s", type(exc).__name__)
            await asyncio.sleep(self.config.storage.cleanup_seconds)

    async def on_unload(self):
        async with self._lifecycle_lock:
            await self._stop_runtime()

    async def _stop_runtime(self):
        self._accepting = False
        if self.runtime:
            self.runtime.closing = True
        handlers = [task for task in self._handlers if task is not asyncio.current_task()]
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if self._maintenance:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
            self._maintenance = None
        if self.runtime:
            await self.runtime.close()
            self.runtime = None
        self._confirmations.clear()

    async def on_config_update(self, scope, config_data, version):
        if scope != "self":
            return
        config = NaiConfig.model_validate(config_data)
        async with self._lifecycle_lock:
            await self._stop_runtime()
            self.set_plugin_config(config.model_dump())
            self._start_runtime()

    @Command("nai", description="NAI绘图、负面词、反推与重画", pattern=r"^/(?:nai|uc)(?:\s|$)")
    async def handle_command(self, text="", **kwargs):
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            return await self._handle_command(text, kwargs)
        finally:
            self._handlers.discard(task)

    async def _handle_command(self, text, kwargs):
        if not self._accepting or self.runtime is None:
            return False, "", 0
        if not isinstance(text, str):
            return True, "指令文本格式无效。", 2
        try:
            context = command_context(kwargs)
        except UserError as exc:
            return True, str(exc), 2
        entry = self.runtime.parse_entry(text)
        pieces = entry[1].split(None, 1) if entry and entry[0] == "nai" else []
        if pieces and pieces[0] == "确认":
            token = pieces[1] if len(pieces) > 1 else ""
            try:
                self.runtime.check_permission(context, "draw")
            except UserError as exc:
                await self.runtime.say(context, str(exc))
                return True, "", 2
            item = self._confirmations.pop(token, None)
            if not item or item["until"] < time.monotonic():
                await self.runtime.say(context, "绘图确认已过期或不存在。")
                return True, "", 2
            await self.runtime.handle(context, item["prompt"], natural=True)
            return True, "", 2
        handled = await self.runtime.handle(context, text)
        return handled, "", 2 if handled else 0

    @Tool("nai_drawing_request", description="创建绘图确认指令，按确认指令发送者的权限提交。",
          parameters=[ToolParameterInfo(name="prompt", param_type=ToolParamType.STRING,
                      description="用户绘图要求，保留原始tag和角色名称", required=True)])
    async def drawing_request(self, prompt="", **kwargs):
        # Tool kwargs can contain model-supplied IDs on some hosts. Never authorize from them.
        c = self.config
        if not self._accepting or not c.plugin.enabled or not c.modules.natural_tool or not c.modules.draw:
            return {"success": False, "content": "自然对话绘图入口已关闭。"}
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > c.plugin.max_input_chars:
            return {"success": False, "content": "绘图要求为空或超过长度上限。"}
        now = time.monotonic()
        self._confirmations = {k: v for k, v in self._confirmations.items() if v["until"] > now}
        if len(self._confirmations) >= c.parser.pending_limit:
            return {"success": False, "content": "待确认请求已达上限，请稍后再试。"}
        token = secrets.token_hex(8)
        self._confirmations[token] = {"prompt": prompt, "until": now + c.natural.confirmation_seconds}
        command = c.plugin.prefix + c.plugin.command_names[0] + " 确认 " + token
        return {"success": True, "content": "尚未生图，请用户发送确认指令：" + command}


def create_plugin():
    return NaiPlugin()
