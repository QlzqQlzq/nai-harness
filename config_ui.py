"""Native MaiBot configuration presentation; canonical configuration stays unchanged."""
from copy import deepcopy
from functools import lru_cache

from maibot_sdk.config import generate_plugin_config_schema

from .config import Endpoint, KeyConfig, NaiConfig, Pool


LABELS = {
    "enabled": "启用", "model": "模型名称", "size": "图片尺寸", "negative": "默认负面词",
    "admin_qq": "全局管理员 QQ", "group_enabled": "允许群聊使用", "private_enabled": "允许私聊使用",
    "private_manage": "私聊可修改自己的模型与 NSFW 设置", "private_features": "私聊开放功能",
    "allowed_users": "允许使用的 QQ（留空不限制）", "blocked_users": "禁止使用的 QQ",
    "allowed_groups": "允许使用的群（留空不限制）", "blocked_groups": "禁止使用的群",
    "group_nsfw": "群聊默认使用 NSFW 模式", "private_nsfw": "私聊默认使用 NSFW 模式",
    "dry_run": "只解析，不生成图片", "draw": "绘图指令", "uc": "负面词指令",
    "reverse": "图片反推", "redraw": "引用重画", "styles": "个人画风", "natural_tool": "自然对话绘图入口",
    "strategy": "多个服务的使用顺序", "key_strategy": "多把密钥的使用顺序",
    "allow_paid": "允许使用标记为付费的服务", "max_attempts": "一次请求最多尝试次数",
    "total_timeout": "整次处理最长等待（秒）", "endpoints": "服务列表",
    "name": "名称（同一列表内不能重复）", "url": "完整接口地址", "kind": "接口格式",
    "keys": "密钥列表", "secret": "API 密钥", "env": "密钥环境变量（直接填密钥时留空）",
    "note": "备注", "paid": "标记为付费服务", "anonymous": "无需密钥的服务",
    "timeout_seconds": "单次等待上限（秒）", "models": "可用绘图模型（留空不限制）",
    "role_mode": "多人角色处理方式", "cross_fallback": "普通与 NSFW 服务之间的回退方向",
    "steps": "生成步数", "scale": "提示词引导强度", "cfg": "CFG 重缩放",
    "sampler": "采样器", "noise_schedule": "噪声调度", "other": "SMEA/DYN 模式",
    "allowed_models": "允许选择的绘图模型", "max_pixels": "总像素上限", "max_roles": "角色数量上限",
    "overrides": "允许用户临时调整的生成参数", "fallback_errors": "允许换备用模型的错误类型",
    "pure_tags_direct": "纯 tag 原样直出", "system_prompt": "额外解析指导",
    "temperature": "模型温度", "max_tokens": "单轮输出上限", "max_rounds": "解析轮次上限",
    "max_tool_calls": "工具调用次数上限", "skills_dir": "Skill 目录", "skills": "启用的 Skill 名称",
    "candidate_ttl": "角色候选有效期（秒）", "pending_limit": "待确认记录上限",
    "result_count": "每次搜索结果数", "max_text_chars": "搜索证据字数上限",
    "cache_seconds": "搜索缓存有效期（秒）", "cache_entries": "搜索缓存条数",
    "fetch_enabled": "额外抓取搜索结果正文", "allowed_domains": "搜索来源域名（留空不限制）",
    "tag_api_enabled": "启用 tag 查询接口", "tag_api_url": "tag 查询完整地址",
    "tag_api_timeout": "tag 查询最长等待（秒）", "tag_api_limit": "tag 查询候选上限",
    "path": "外置词库文件路径", "candidate_limit": "本地候选数量",
    "default": "默认画风 tag", "allow_personal": "允许用户保存个人画风",
    "apply_to_direct": "纯 tag 输入也追加画风", "explicit_style_wins": "用户指定的画风优先",
    "prompt": "反推提示词", "max_images": "一次反推最多图片数", "quote_first": "优先读取引用图片",
    "json_tagger_threshold": "标签置信度下限", "allow_other_users": "允许重画同群其他用户的作品",
    "allow_last_without_quote": "未引用时重画自己最近一张", "inherit_parameters": "继承原图生成参数",
    "inherit_seed": "继承原图种子", "inherit_model": "继承原图模型",
    "description": "自然绘图工具描述", "confirmation_seconds": "自然绘图确认有效期（秒）",
    "blocked_terms": "所有模式禁止的词", "nsfw_terms": "普通模式禁止的词", "wordlist_path": "附加 NSFW 词表",
    "user_limit": "每人每周期本地点数", "group_limit": "每群每周期本地点数",
    "reset_hours": "本地点数重置周期（小时）", "refund_delivery_failure": "图片发送失败退本地点数",
    "crash_recovery": "异常退出后如何处理预占点数", "global_limit": "同时处理任务总数",
    "private_limit": "所有私聊同时处理任务数", "per_session": "同一会话同时处理任务数",
    "per_user": "同一用户同时处理任务数", "waiting_limit": "等待队列容量",
    "wait_seconds": "最长排队时间（秒）", "cooldown_seconds": "同一用户提交间隔（秒）",
    "task_timeout": "整个任务最长时间（秒）", "mode": "成功后的回复内容",
    "quote_reply": "回复时引用原消息", "progress": "发送开始处理提示", "progress_text": "开始提示文字",
    "show_tags": "回执显示最终 tag", "show_route": "回执显示服务与密钥名称",
    "send_retries": "消息发送重试次数", "send_interval": "消息发送重试间隔（秒）",
    "save_images": "保存生成图片", "image_days": "图片保留天数", "history_seconds": "引用记录有效期（秒）",
    "history_limit": "最多引用记录数", "watermark": "水印文字", "metadata": "图片内保存生成参数",
    "logs_enabled": "保存运行日志", "log_prompts": "日志记录用户提示词", "log_days": "日志保留天数",
    "cleanup_seconds": "清理间隔（秒）", "max_image_files": "最多保存图片数",
    "max_image_disk_bytes": "图片占用空间上限（字节）", "persist_key_health": "重启后保留密钥冷却与停用状态",
    "proxy": "网络代理（留空直连）", "connect_timeout": "建立连接最长等待（秒）",
    "max_connections": "HTTP 连接数量上限", "max_image_bytes": "单张图片大小上限（字节）",
    "image_download_hosts": "允许下载图片的域名", "max_response_bytes": "上游响应大小上限（字节）",
    "download_timeout": "下载图片最长等待（秒）", "max_input_chars": "用户输入字数上限",
    "command_names": "绘图命令别名", "uc_names": "负面词命令别名", "prefix": "命令前缀",
}

FEATURES = ["draw", "uc", "reverse", "redraw", "styles", "model", "nsfw", "status"]
ERRORS = ["quota", "auth", "rate_limit", "timeout", "server", "invalid_response", "bad_request", "unconfigured", "budget"]


@lru_cache(maxsize=None)
def scalar_schema(model):
    schema = generate_plugin_config_schema(model)
    return schema["sections"].get("general", {}).get("fields", {})


def field_schema(model, name):
    field = deepcopy(scalar_schema(model)[name])
    field["label"] = LABELS.get(name, field["description"].split("；")[0])
    field["hint"] = field["description"]
    if model.__name__ == "Queue" and name == "group_limit":
        field["label"] = "所有群聊同时处理任务数"
    if name == "strategy" or name == "key_strategy":
        field["hint"] = "ordered：从第一项开始；round_robin：每次请求轮换起点。"
    if name == "private_features":
        field.update(ui_type="select", choices=FEATURES, multiple=True)
    elif name == "overrides":
        field.update(ui_type="select", choices=["size", "seed", "steps", "scale", "cfg", "sampler", "noise_schedule", "other"], multiple=True)
    elif name == "fallback_errors":
        field.update(ui_type="select", choices=ERRORS, multiple=True)
    if name in ("system_prompt", "prompt", "negative", "default", "description"):
        field.update(ui_type="textarea", rows=3)
    return field


def list_item(model, name):
    field = field_schema(model, name)
    result = {key: deepcopy(field[key]) for key in ("label", "default", "placeholder", "choices", "min", "max")}
    # Older native list editors only accept 'number', not 'integer'.
    result["type"] = "number" if field["type"] == "integer" else field["type"]
    if result["type"] == "array":
        result["item_type"] = field.get("item_type") or "string"
    return result


def service_list(path):
    field = field_schema(Pool, "endpoints")
    drawing = path.startswith("drawing.")
    reverse = path.startswith("reverse.")
    kinds = (["native", "openai_image"] if drawing else
             ["openai_chat", "json_tagger"] if reverse else
             ["json_fetch"] if path == "search.fetch_services" else
             ["deepseek_search", "tavily", "searxng"] if path == "search.services" else ["openai_chat"])
    names = ["name", "enabled", "url", "kind"]
    if drawing:
        names += ["models", "paid"]
    elif path.startswith(("parser.", "reverse.")):
        names += ["model"]
    names += ["keys", "key_strategy", "timeout_seconds"]
    items = {name: list_item(Endpoint, name) for name in names}
    items["name"].update(default="", placeholder="例如：主接口、备用接口")
    items["url"]["placeholder"] = "https://服务域名/完整接口路径"
    items["kind"].update(choices=kinds, default=kinds[0])
    items["kind"]["label"] = ("接口格式（native=NAI原生，openai_image=OpenAI图片）" if drawing else
                              "接口格式（openai_chat=视觉模型，json_tagger=标签服务）" if reverse else "接口格式")
    items["key_strategy"]["label"] = "密钥使用顺序（ordered=按顺序，round_robin=轮询）"
    keys = {name: list_item(KeyConfig, name) for name in ("name", "enabled", "secret", "env")}
    keys["name"].update(default="", placeholder="例如：主密钥、备用密钥")
    keys["env"]["placeholder"] = "通常留空，直接在上面填写 API 密钥"
    items["keys"].update(type="array", item_type="object", item_fields=keys, min_items=1,
                         default=[KeyConfig().model_dump(exclude_none=True)])
    field.update(item_type="object", item_fields=items, label="已添加的服务", hint=(
        "每项填写一个接口地址，可添加多把密钥；名称不得重复。新建服务默认使用内置失败策略。"
        "已有的高级配置会保留；模型别名、逐 key 失败规则与补购在原文配置中调整。"))
    return field


def build_webui_schema(base):
    schema = deepcopy(base)
    schema["sections"] = {}
    tabs = [{"id": name, "title": title, "order": index, "sections": []} for index, (name, title) in enumerate([
        ("start", "开始使用"), ("drawing", "绘图接口"), ("parsing", "提示词与联网"),
        ("features", "高级：反推与画风"), ("access", "高级：权限与用量"), ("advanced", "高级：维护与调优")])]
    by_tab = {tab["id"]: tab for tab in tabs}
    defaults = NaiConfig()
    shown = set()

    def section(tab, path, title, names, *, collapsed=False, description=""):
        value = defaults
        for part in path.split("."):
            value = getattr(value, part)
        fields = {}
        for name in names:
            field = service_list(path) if name == "endpoints" else field_schema(type(value), name)
            field["order"] = len(fields)
            fields[name] = field
            shown.add((path, name))
        key = tab + ":" + path + ":" + str(len(schema["sections"]))
        schema["sections"][key] = {"name": path, "title": title, "description": description,
            "icon": "settings", "collapsed": collapsed, "order": len(schema["sections"]), "fields": fields}
        by_tab[tab]["sections"].append(key)

    def pools(tab, prefix, purpose):
        section(tab, prefix + ".sfw", purpose + "：普通模式", ["endpoints", "strategy", "allow_paid"], description=
                "会话的 NSFW 关闭时使用这组接口。" + ("这里填写生成图片的服务。" if prefix == "drawing" else ""))
        section(tab, prefix + ".nsfw", purpose + "：NSFW 模式", ["endpoints", "strategy", "allow_paid"], collapsed=True,
                description="仅在会话开启 NSFW 时使用；可配置不同地址、密钥和模型，默认不与普通模式互相代用。")
        for lane in ("sfw", "nsfw"):
            section("advanced", prefix + "." + lane, purpose + " / " + ("普通模式" if lane == "sfw" else "NSFW 模式") + "：调用限制",
                    ["max_attempts", "total_timeout"], collapsed=True)
        section("advanced", prefix, purpose + "：跨模式回退", ["cross_fallback"], collapsed=True)

    section("start", "plugin", "插件开关", ["enabled"], description=
            "首次使用还需填写绘图接口；中文或混合输入另需填写提示词解析接口。")
    section("start", "permissions", "管理员与使用范围", ["admin_qq", "group_enabled", "private_enabled"])
    section("start", "generation", "默认图片", ["model", "size"])
    pools("drawing", "drawing", "生成图片的接口")
    section("parsing", "parser", "把描述转换成 tag", ["enabled"], description=
            "解析模型负责理解中文、混合输入和角色关系，不负责生成图片；纯 tag 默认原样直出。")
    pools("parsing", "parser.services", "理解提示词的模型")
    section("parsing", "search", "角色与 tag 查询", ["enabled", "tag_api_enabled", "tag_api_url"])
    section("parsing", "search.services", "联网搜索接口", ["endpoints", "strategy", "allow_paid"])
    section("features", "modules", "可选功能开关", list(type(defaults.modules).model_fields), collapsed=True)
    pools("features", "reverse.services", "看图提取 tag 的模型")
    section("features", "styles", "默认与个人画风", ["default", "allow_personal", "apply_to_direct", "explicit_style_wins"])
    section("features", "redraw", "引用重画", list(type(defaults.redraw).model_fields), collapsed=True)
    section("features", "reverse", "反推设置", list(scalar_schema(type(defaults.reverse))), collapsed=True)
    section("features", "natural", "自然对话确认", list(type(defaults.natural).model_fields), collapsed=True)
    section("features", "output", "图片与回执", list(type(defaults.output).model_fields), collapsed=True)
    section("access", "permissions", "私聊权限", ["private_manage", "private_features"], collapsed=True)
    section("access", "content", "默认内容模式", ["group_nsfw", "private_nsfw"], collapsed=True)
    section("access", "permissions", "群聊与用户限制", ["allowed_groups", "blocked_groups", "allowed_users", "blocked_users"], collapsed=True,
            description="留空的允许名单不限制使用。按群管理 QQ 和初始设置目前在原文配置的 permissions.group_managers / groups 中填写。")
    section("access", "content", "禁止词", ["blocked_terms", "nsfw_terms", "wordlist_path"], collapsed=True)
    section("access", "quota", "本地使用点数（可选）", ["enabled", "user_limit", "group_limit", "reset_hours", "refund_delivery_failure", "crash_recovery"], collapsed=True,
            description="限制群友使用量，不代表上游账户余额。按模型计数、特殊用户和群额度可在原文配置中覆盖。")
    section("access", "queue", "并发与排队", [name for name in type(defaults.queue).model_fields if name != "group_overrides"], collapsed=True)
    section("advanced", "generation", "生成参数", ["negative", "steps", "scale", "cfg", "sampler", "noise_schedule", "other"], collapsed=True)
    section("advanced", "generation", "模型与生成范围", ["allowed_models", "max_pixels", "max_roles", "overrides"], collapsed=True)
    section("advanced", "plugin", "解析测试", ["dry_run"], collapsed=True,
            description="开启后不生成图片；解析、联网和反推服务仍可能产生请求及费用。正常使用保持关闭。")
    section("advanced", "search", "网页正文抓取", ["fetch_enabled"], collapsed=True)
    section("advanced", "search.fetch_services", "网页正文抓取接口", ["endpoints", "strategy", "allow_paid"], collapsed=True,
            description="只有开启额外正文抓取时才使用。")
    section("advanced", "dictionary", "本地词库（可选）", ["enabled", "path", "candidate_limit"], collapsed=True)
    section("advanced", "storage", "图片与日志", list(type(defaults.storage).model_fields), collapsed=True)
    section("advanced", "network", "网络连接", list(type(defaults.network).model_fields), collapsed=True)

    # Retain scalar controls not explicitly placed above, without presenting unsupported
    # arbitrary dictionaries as text inputs (old dashboards would corrupt their types).
    for path in ("plugin", "modules", "generation", "parser", "search", "search.services", "search.fetch_services"):
        value = defaults
        for part in path.split("."):
            value = getattr(value, part)
        fields = [name for name, field in scalar_schema(type(value)).items()
                  if (path, name) not in shown and field["type"] != "object" and name != "config_version"]
        if fields:
            titles = {"plugin": "输入与命令", "modules": "基础功能开关", "generation": "备用模型触发条件",
                      "parser": "提示词解析细节", "search": "联网查询细节",
                      "search.services": "搜索服务调用限制", "search.fetch_services": "网页抓取调用限制"}
            section("advanced", path, titles[path], fields, collapsed=True)
    schema["layout"] = {"type": "tabs", "tabs": tabs}
    return schema
