"""Local state, bounded concurrency and transactional quota reservations."""
import asyncio
import copy
import json
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path


class UserError(Exception):
    pass


async def finish_before_cancel(awaitable):
    """Finish a state writer even when cancellation is requested more than once."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class State:
    def __init__(self, directory, recovery="retain"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.lock = asyncio.Lock()
        pending = self.data.pop("reservations", {})
        self.data["recovered_reservations"] = len(pending)
        if recovery == "refund":
            for value in pending.values():
                if value["epoch"] == self.data.get("quota_epoch"):
                    usage = self.data.setdefault("usage", {})
                    for target in value["targets"]:
                        usage[target] = max(0, usage.get(target, 0) - value["cost"])

    async def save(self):
        # Callers hold lock when changing financial state; no awaits during snapshot.
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        def write():
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(self.path)
        await finish_before_cancel(asyncio.to_thread(write))

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            previous = copy.deepcopy(self.data)
            try:
                yield
                if self.data != previous:
                    await self.save()
            except Exception:
                # Keep dictionaries referenced by Runtime (notably history) alive.
                for key in set(self.data) - set(previous):
                    del self.data[key]
                for key, value in previous.items():
                    if isinstance(value, dict) and isinstance(self.data.get(key), dict):
                        self.data[key].clear()
                        self.data[key].update(value)
                    else:
                        self.data[key] = value
                raise

    def get(self, section, key, default=None):
        return self.data.get(section, {}).get(key, default)

    async def set(self, section, key, value):
        async with self.transaction():
            self.data.setdefault(section, {})[key] = value

    async def reserve(self, config, uid, group, model):
        if not config.enabled:
            return None
        epoch = int(time.time() // (config.reset_hours * 3600))
        cost = config.model_costs.get(model, 1)
        reservation = None
        try:
            async with self.transaction():
                if self.data.get("quota_epoch") != epoch:
                    self.data["quota_epoch"] = epoch
                    self.data["usage"] = {}
                usage = self.data.setdefault("usage", {})
                targets = [("u:" + uid, config.user_limits.get(uid, config.user_limit))]
                if group:
                    targets.append(("g:" + group, config.group_limits.get(group, config.group_limit)))
                if any(usage.get(k, 0) + cost > limit for k, limit in targets):
                    raise UserError("本地额度不足，本次未生成。")
                for k, _ in targets:
                    usage[k] = usage.get(k, 0) + cost
                reservation = uuid.uuid4().hex
                self.data.setdefault("reservations", {})[reservation] = {
                    "epoch": epoch, "cost": cost, "targets": [k for k, _ in targets]}
        except asyncio.CancelledError:
            await finish_before_cancel(self.refund(reservation))
            raise
        return reservation

    async def commit(self, reservation):
        if reservation is None:
            return
        async with self.transaction():
            self.data.setdefault("reservations", {}).pop(reservation, None)

    async def refund(self, reservation):
        if reservation is None:
            return
        async with self.transaction():
            value = self.data.setdefault("reservations", {}).pop(reservation, None)
            if value is None:
                return
            epoch, cost, targets = value["epoch"], value["cost"], value["targets"]
            if self.data.get("quota_epoch") == epoch:
                usage = self.data.setdefault("usage", {})
                for k in targets:
                    usage[k] = max(0, usage.get(k, 0) - cost)


class Slots:
    def __init__(self, config):
        self.config = config
        self.condition = asyncio.Condition()
        self.active, self.users, self.sessions = Counter(), Counter(), Counter()
        self.waiting = 0
        self.last_request = {}

    @asynccontextmanager
    async def acquire(self, context):
        c = self.config
        lane = "group" if context.group_id else "private"
        uid, session = context.owner, context.stream_id
        session_limit = c.group_overrides.get(context.group_id, c.per_session) if context.group_id else c.per_session
        def available():
            return (sum(self.active.values()) < c.global_limit
                    and self.active[lane] < getattr(c, lane + "_limit")
                    and self.users[uid] < c.per_user and self.sessions[session] < session_limit)
        async with self.condition:
            if time.monotonic() - self.last_request.get(uid, -1e20) < c.cooldown_seconds:
                raise UserError("请求过于频繁，请稍后重试。")
            self.last_request[uid] = time.monotonic()
            if len(self.last_request) > 10000:
                self.last_request = {k: v for k, v in self.last_request.items() if time.monotonic() - v < c.cooldown_seconds}
            if not available():
                if self.waiting >= c.waiting_limit or not c.wait_seconds:
                    raise UserError("同时任务已达上限，本次未生成。")
                self.waiting += 1
                try:
                    await asyncio.wait_for(self.condition.wait_for(available), c.wait_seconds)
                except asyncio.TimeoutError as exc:
                    raise UserError("排队等待超时，本次未生成。") from exc
                finally:
                    self.waiting -= 1
            self.active[lane] += 1
            self.users[uid] += 1
            self.sessions[session] += 1
        try:
            yield
        finally:
            async def release():
                async with self.condition:
                    self.active[lane] -= 1
                    self.users[uid] -= 1
                    self.sessions[session] -= 1
                    if not self.users[uid]:
                        del self.users[uid]
                    if not self.sessions[session]:
                        del self.sessions[session]
                    self.condition.notify_all()
            await finish_before_cancel(release())
