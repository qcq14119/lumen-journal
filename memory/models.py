"""Private plugin-owned connections for bounded remote memory calls."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import re
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp

from ..provider import PublicResolver, validate_url
from ..store import AlbumError
from .text import clean_image_description

# Local planning bounds, below the transport's 16 MiB ceiling. Vision token
# usage varies by provider; reserve a conservative amount before sending.
IMAGE_BATCH_MAX = 8
IMAGE_REQUEST_BYTES = 8 * 1024 * 1024
IMAGE_INPUT_TOKENS = 4096
IMAGE_OUTPUT_TOKENS = 200
IMAGE_DESCRIPTION_RULE = (
    '额外返回 image_descriptions 数组，每项严格为 {"id":"image-1","description":"一句中文描述"}。'
    '每个输入图片编号恰好一次，不增不漏。描述60字以内，客观写可见内容；'
    '不猜身份，不输出网址、密钥、编码或其他字段。'
)


def parse_image_descriptions(raw, ids):
    """Validate the complete reply before cleaning/shortening any description."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    result, _ = check_image_descriptions(value, ids)
    return result


def check_image_descriptions(value, ids):
    """Same acceptance rules, with a fixed diagnostic for every rejected shape."""
    def rejected(code, actual=-1):
        return None, MemoryResponseError(code, expected_images=len(ids), actual_images=actual)

    if not isinstance(value, dict):
        return rejected("FIELD_TYPE")
    if "image_descriptions" not in value:
        return rejected("IMAGE_MISSING", 0)
    rows = value["image_descriptions"]
    if not isinstance(rows, list):
        return rejected("FIELD_TYPE")
    if len(rows) != len(ids):
        return rejected("IMAGE_COUNT", len(rows))
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "description"}:
            return rejected("FIELD_TYPE", len(rows))
        label, value = row["id"], row["description"]
        if not isinstance(label, str) or not isinstance(value, str):
            return rejected("FIELD_TYPE", len(rows))
        if label not in ids or label in result:
            return rejected("IMAGE_ID", len(rows))
        description = clean_image_description(value)
        if not description:
            return rejected("DESCRIPTION_EMPTY" if not value.strip() else "CONTENT_SAFETY", len(rows))
        result[label] = description
    return (result, None) if set(result) == set(ids) else rejected("IMAGE_ID", len(rows))


MODELS = {
    "summary": "",
    "assistant": "",
    "embedding": "BAAI/bge-m3",
    "rerank": "BAAI/bge-reranker-v2-m3",
}
ROLE_MODES = {
    "summary": {"preset"},
    "assistant": {"disabled", "summary", "preset"},
    "embedding": {"legacy", "disabled", "preset", "host"},
    "rerank": {"legacy", "disabled", "embedding", "preset", "host"},
}


def endpoint(value):
    if not isinstance(value, str) or len(value) > 500:
        raise MemoryModelError("模型接口地址格式不正确")
    value = value.strip().rstrip("/")
    try:
        validate_url(value)
        if urlsplit(value).query:
            raise ValueError
    except (AlbumError, ValueError):
        raise MemoryModelError(
            "模型接口须为公开 HTTPS 前缀，不含账号、查询参数或片段"
        ) from None
    return value


class MemoryModelError(AlbumError):
    """Safe, locally authored errors; never include remote bodies or secrets."""


class MemoryResponseError(MemoryModelError):
    """Only allowlisted reasons and integer counters can reach persisted logs."""

    REASONS = {
        "REQUEST_FAILED": "请求失败", "REQUEST_TIMEOUT": "请求超时",
        "RESPONSE_JSON": "接口外层JSON无效", "RESPONSE_FORMAT": "接口响应结构无效",
        "RESPONSE_LIMIT": "接口响应超过限制", "RESPONSE_TRUNCATED": "响应被截断",
        "RESPONSE_FINISH": "响应未正常结束", "BODY_EMPTY": "正文为空",
        "JSON_SYNTAX": "正文JSON语法错误", "JSON_FENCED": "完整JSON带代码框，联合格式未接受",
        "IMAGE_MISSING": "图片描述字段缺失", "IMAGE_COUNT": "图片描述数量不符",
        "IMAGE_ID": "图片描述编号不符或重复", "FIELD_TYPE": "字段类型或结构错误",
        "SUMMARY_MISSING": "总结字段缺失", "SUMMARY_INVALID": "总结条目无效",
        "DESCRIPTION_EMPTY": "图片描述为空", "CONTENT_SAFETY": "内容安全校验失败",
    }
    COUNTERS = ("body_chars", "expected_images", "actual_images", "invalid_items", "http_status", "response_bytes")

    def __init__(self, code, *, retryable=False, **counts):
        self.code = code if code in self.REASONS else "RESPONSE_FORMAT"
        self.counts = {key: value for key in self.COUNTERS
                       if type(value := counts.get(key)) is int and -1 <= value <= 2**31 - 1}
        self.retryable = retryable
        numbers = " ".join(f"{key}={value}" for key, value in self.counts.items())
        super().__init__(f"{self.REASONS[self.code]} [code={self.code}{' ' + numbers if numbers else ''}]")


class MemoryModels:
    """Keep keys in a dedicated table, outside portable memory and host settings."""

    def __init__(self, store):
        self.store = store
        self.cache = {}
        self.testing = False
        with store.transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS memory_model_settings "
                "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, data TEXT NOT NULL)"
            )

    def private(self):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT revision,data FROM memory_model_settings WHERE id=1"
            ).fetchone()
        defaults = {
            "revision": 0,
            "shared": {"base_url": "https://api.siliconflow.cn/v1", "api_key": ""},
            **{
                role: {
                    "model": model,
                    "custom": False,
                    "base_url": "",
                    "api_key": "",
                    **({"dimensions": 1024} if role == "embedding" else {}),
                    **(
                        {"mode": "current" if role == "summary" else "summary"}
                        if role in ("summary", "assistant")
                        else {}
                    ),
                }
                for role, model in MODELS.items()
            },
        }
        if not row:
            return defaults
        saved = json.loads(row["data"])
        # Preserve the previous explicitly selected connection on upgrade. It is
        # now a separate story API, never silently changed to the chat provider.
        old_summary = saved.get("summary", {})
        if old_summary and "mode" not in old_summary:
            if not old_summary.get("custom"):
                old_summary.update(saved.get("shared", {}))
            old_summary.update(mode="custom", custom=True)
        return {**defaults, **saved, "revision": row["revision"]}

    def public(self):
        data = self.private()
        for role in ("shared", *MODELS):
            data[role]["key_configured"] = bool(data[role].pop("api_key", ""))
        for item in data.get("presets", []):
            item["key_configured"] = bool(item.pop("api_key", ""))
        return data

    def selection(self, role, settings):
        """Resolve old routing without changing a saved conversation on read."""
        if role not in MODELS:
            raise MemoryModelError("不支持的模型用途")
        explicit = settings.get("model_bindings", {}).get(role)
        if explicit:
            return dict(explicit)
        if role == "assistant":
            if not settings.get("assistant_enabled"):
                return {"mode": "disabled"}
            return {
                "mode": "legacy"
                if self.private()[role]["mode"] == "custom"
                else "summary"
            }
        provider = settings.get(f"{role}_provider_id", "")
        if provider == "__plugin__":
            if role == "summary" and self.private()[role]["mode"] == "current":
                return {"mode": "current"}
            return {"mode": "legacy"}
        return (
            {"mode": "host", "provider_id": provider}
            if provider
            else {"mode": "current" if role == "summary" else "disabled"}
        )

    def _legacy_config(self, role, data=None):
        data = data or self.private()
        config = data[role]
        connection = (
            config
            if role in ("summary", "assistant") or config.get("custom")
            else data["shared"]
        )
        return {**config, **connection, "model": config["model"]}

    def validate_bindings(self, bindings, data=None):
        data = data or self.private()
        if not isinstance(bindings, dict) or set(bindings) - set(MODELS):
            raise MemoryModelError("模型用途格式不正确")
        for role, value in bindings.items():
            if not isinstance(value, dict) or set(value) - {
                "mode",
                "preset_id",
                "provider_id",
                "model",
            }:
                raise MemoryModelError("模型用途字段不正确")
            mode = value.get("mode")
            if mode not in ROLE_MODES[role]:
                if role in ("summary", "assistant"):
                    raise MemoryModelError("总结与检查助手请使用独立API连接")
                raise MemoryModelError("模型用途与连接方式不匹配")
            if mode == "preset":
                if role == "summary" and value == {"mode": "preset", "preset_id": ""}:
                    continue
                item = next(
                    (
                        p
                        for p in data.get("presets", [])
                        if p["id"] == value.get("preset_id")
                    ),
                    None,
                )
                if not item or item["type"] != (
                    "chat" if role in ("summary", "assistant") else role
                ):
                    raise MemoryModelError("连接不存在或类型与用途不匹配")
            if mode == "host" and (
                not isinstance(value.get("provider_id"), str)
                or not 1 <= len(value["provider_id"]) <= 200
                or value["provider_id"] == "__plugin__"
            ):
                raise MemoryModelError("宿主连接格式不正确")
            if mode == "embedding":
                model = value.get("model", MODELS["rerank"])
                if (
                    not isinstance(model, str)
                    or not 1 <= len(model) <= 200
                    or any(c.isspace() for c in model)
                ):
                    raise MemoryModelError("精排模型名称格式不正确")
            allowed = {"mode"} | (
                {"preset_id"}
                if mode == "preset"
                else {"provider_id"}
                if mode == "host"
                else {"model"}
                if mode == "embedding"
                else set()
            )
            if set(value) - allowed:
                raise MemoryModelError("模型用途包含不适用字段")

    def _used_presets(self, settings, data):
        used = set()
        for role in MODELS:
            binding = self.selection(role, settings)
            if binding["mode"] == "preset":
                used.add(binding["preset_id"])
            elif binding["mode"] == "legacy" and data[role].get("preset_id"):
                used.add(data[role]["preset_id"])
        return used

    def usage(self, data=None):
        data = data or self.private()
        result = {
            p["id"]: {
                "usage_count": 0,
                "global_roles": [
                    r for r in MODELS if data[r].get("preset_id") == p["id"]
                ],
            }
            for p in data.get("presets", [])
        }
        for identifier, count in self.store.chats_using_preset().items():
            if identifier in result:
                result[identifier]["usage_count"] += count
        album = self.store.restore_guard.store
        binding = album.settings.get("plan_recovery", {}).get("assistant_binding", {})
        if binding.get("mode") == "preset" and binding.get("preset_id") in result:
            result[binding["preset_id"]]["global_roles"].append("plan_recovery")
        binding = album.settings.get("memory_maintenance", {}).get("binding", {})
        if binding.get("mode") == "preset" and binding.get("preset_id") in result:
            result[binding["preset_id"]]["global_roles"].append("memory_maintenance")
        return result

    def workspace(self, settings):
        data = self.public()
        usage = self.usage()
        presets = [{**p, **usage[p["id"]]} for p in data.get("presets", [])]
        legacy = {}
        for role in MODELS:
            if role in ("summary", "assistant"):
                continue
            config = self._legacy_config(role)
            config["key_configured"] = bool(config.pop("api_key", ""))
            legacy[role] = config
        bindings = {role: self.selection(role, settings) for role in MODELS}
        for role in ("summary", "assistant"):
            if bindings[role]["mode"] not in ROLE_MODES[role]:
                bindings[role] = {"mode": "preset", "preset_id": ""}
        return {
            "models_revision": data["revision"],
            "presets": presets,
            "legacy": legacy,
            "bindings": bindings,
        }

    def stage_presets(self, changes, revision):
        data = self.private()
        if type(revision) is not int or revision != data["revision"]:
            raise MemoryModelError("模型配置版本已变化，请重新打开设置")
        if not isinstance(changes, list) or len(changes) > 48:
            raise MemoryModelError("模型草稿数量不正确")
        if not changes:
            return data, {}
        presets, aliases, touched = data.setdefault("presets", []), {}, set()
        for change in changes:
            if not isinstance(change, dict) or set(change) - {
                "operation",
                "id",
                "draft_id",
                "source_role",
                "config",
            }:
                raise MemoryModelError("模型草稿字段不正确")
            operation, identifier = change.get("operation"), change.get("id")
            if operation not in ("save", "copy", "delete"):
                raise MemoryModelError("不支持的模型草稿操作")
            if identifier and identifier in touched and operation != "copy":
                raise MemoryModelError("同一连接不能重复修改")
            if identifier and operation != "copy":
                touched.add(identifier)
            old = next((p for p in presets if p["id"] == identifier), None)
            if identifier and not old:
                raise MemoryModelError("连接不存在")
            if operation == "delete":
                if not old:
                    raise MemoryModelError("连接不存在")
                presets.remove(old)
                continue
            values = dict(change)
            if operation == "copy":
                if not old:
                    raise MemoryModelError("复制来源不存在")
                values["config"] = {
                    "name": old["name"][:55] + " 副本",
                    **change.get("config", {}),
                }
            item = self.preset("draft", values, revision)
            if operation == "copy":
                item["id"] = "model_" + uuid.uuid4().hex
            draft_id = change.get("draft_id")
            if draft_id:
                if (
                    not isinstance(draft_id, str)
                    or len(draft_id) > 100
                    or draft_id in aliases
                    or any(p["id"] == draft_id for p in presets)
                ):
                    raise MemoryModelError("模型草稿标识不正确")
                aliases[draft_id] = item["id"]
            presets[:] = [p for p in presets if p["id"] != item["id"]] + [item]
        if len(presets) > 24:
            raise MemoryModelError("最多保存24个模型连接")
        return data, aliases

    def commit_presets(self, data, revision):
        """Called inside the workspace transaction after its chat bindings are saved."""
        previous = self.private()
        if type(revision) is not int or previous["revision"] != revision:
            raise MemoryModelError("模型配置版本已变化，请重新打开设置")
        available = {p["id"]: p for p in data.get("presets", [])}
        usage = self.usage(previous)
        for old in previous.get("presets", []):
            item = available.get(old["id"])
            active = usage[old["id"]]
            if (active["usage_count"] or active["global_roles"]) and (
                not item or item["type"] != old["type"]
            ):
                raise MemoryModelError("此连接仍被使用，请先更换用途绑定")
            if item:
                for role in active["global_roles"]:
                    if role == "plan_recovery":
                        continue
                    data[role].update(
                        {
                            k: v
                            for k, v in item.items()
                            if k not in ("id", "name", "type")
                        }
                    )
        if data == previous:
            return
        saved = {k: v for k, v in data.items() if k != "revision"}
        self.store.db.execute(
            "INSERT INTO memory_model_settings VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
            (revision + 1, json.dumps(saved, ensure_ascii=False)),
        )
        self.cache.clear()

    def preset(self, action, values, revision):
        """Named connection lifecycle. Binding copies a validated connection to its role."""
        with self.store.transaction():
            data = self.private()
            if type(revision) is not int or revision != data["revision"]:
                raise MemoryModelError("模型配置版本已变化，请重新打开设置")
            presets = data.setdefault("presets", [])
            item = next((p for p in presets if p["id"] == values.get("id")), None)
            if values.get("id") and not item:
                raise MemoryModelError("连接不存在")
            if values.get("source_role"):
                role = values["source_role"]
                if role not in MODELS or item or action not in ("draft", "key"):
                    raise MemoryModelError("原连接用途不正确")
                config = self._legacy_config(role, data)
                item = {
                    k: v
                    for k, v in config.items()
                    if k
                    in (
                        "base_url",
                        "model",
                        "api_key",
                        "dimensions",
                        "max_context_tokens",
                    )
                }
                item.update(
                    type="chat" if role in ("summary", "assistant") else role,
                    name="原有连接",
                )
            if action == "key":
                if not item or item["base_url"] != values.get("base_url"):
                    raise MemoryModelError("连接已变化，请重新打开")
                return {"value": item["api_key"]}
            if action in ("save", "draft"):
                if action == "save" and not item and len(presets) >= 24:
                    raise MemoryModelError("最多保存24个模型连接")
                old = item or {}
                draft = values.get("config", {})
                if not isinstance(draft, dict) or set(draft) - {
                    "name",
                    "type",
                    "base_url",
                    "model",
                    "api_key",
                    "clear_key",
                    "dimensions",
                    "max_context_tokens",
                }:
                    raise MemoryModelError("连接字段不正确")
                item = {
                    **old,
                    **{
                        k: v
                        for k, v in draft.items()
                        if k not in ("api_key", "clear_key")
                    },
                }
                item.setdefault("id", "model_" + uuid.uuid4().hex)
                if item.get("type") not in ("chat", "embedding", "rerank"):
                    raise MemoryModelError("请选择模型类型")
                if (
                    not isinstance(item.get("name"), str)
                    or not 1 <= len(item["name"].strip()) <= 60
                ):
                    raise MemoryModelError("连接名称须为1至60字")
                role = "summary" if item["type"] == "chat" else item["type"]
                item["name"] = item["name"].strip()
                if role != "embedding":
                    item.pop("dimensions", None)
                config = {
                    k: item[k]
                    for k in ("base_url", "model", "dimensions", "max_context_tokens")
                    if k in item
                }
                config.update(
                    custom=True, **({"mode": "custom"} if role == "summary" else {})
                )
                # Validate against the preset's own previous key/address, not the active role.
                config["api_key"] = draft.get("api_key", "")
                config["clear_key"] = draft.get("clear_key", False)
                if not config["api_key"] and not config["clear_key"]:
                    if (
                        old.get("api_key")
                        and item["base_url"].rstrip("/") != old["base_url"]
                    ):
                        raise MemoryModelError(
                            "接口地址已变化，请重新填写 Key 或勾选清除"
                        )
                    config["api_key"] = old.get("api_key", "")
                    config["clear_key"] = not bool(config["api_key"])
                checked = self.save({role: config}, revision, preview=True)[role]
                item.update(
                    {
                        k: checked[k]
                        for k in (
                            "base_url",
                            "model",
                            "api_key",
                            "dimensions",
                            "max_context_tokens",
                        )
                        if k in checked
                    }
                )
                if action == "draft":
                    return item
                presets[:] = [p for p in presets if p["id"] != item["id"]] + [item]
                if (
                    old
                    and old.get("type") != item["type"]
                    and any(self.usage(data).get(item["id"], {}).get(key) for key in ("usage_count", "global_roles"))
                ):
                    raise MemoryModelError("此连接正在使用，不能更改模型类型")
                for target in MODELS:
                    if data[target].get("preset_id") == item["id"]:
                        expected = (
                            "chat" if target in ("summary", "assistant") else target
                        )
                        if item["type"] != expected:
                            raise MemoryModelError("此连接正在使用，不能更改模型类型")
                        data[target] = {
                            **data[target],
                            **{
                                k: v
                                for k, v in item.items()
                                if k not in ("id", "name", "type")
                            },
                            "mode": "custom",
                            "custom": True,
                        }
            elif action == "bind":
                role = values.get("role")
                if (
                    not item
                    or role not in MODELS
                    or item["type"]
                    != ("chat" if role in ("summary", "assistant") else role)
                ):
                    raise MemoryModelError("连接类型与用途不匹配")
                data[role] = {
                    **data[role],
                    **{
                        k: v for k, v in item.items() if k not in ("id", "name", "type")
                    },
                    "preset_id": item["id"],
                    "custom": True,
                    "mode": "custom",
                }
            elif action == "delete":
                if not item:
                    raise MemoryModelError("连接不存在")
                if (
                    any(data[r].get("preset_id") == item["id"] for r in MODELS)
                    or any(self.usage(data)[item["id"]][key] for key in ("usage_count", "global_roles"))
                ):
                    raise MemoryModelError("此连接仍被使用，请先更换用途绑定")
                presets.remove(item)
            elif action == "copy":
                if not item or len(presets) >= 24:
                    raise MemoryModelError("连接不存在或数量已达上限")
                presets.append(
                    {
                        **item,
                        "id": "model_" + uuid.uuid4().hex,
                        "name": item["name"][:55] + " 副本",
                    }
                )
            else:
                raise MemoryModelError("不支持的连接操作")
            data.pop("revision")
            self.store.db.execute(
                "INSERT INTO memory_model_settings VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                (revision + 1, json.dumps(data, ensure_ascii=False)),
            )
        self.cache.clear()
        return self.public()

    def reveal(self, role, revision, base_url):
        data = self.private()
        if role not in ("shared", *MODELS):
            raise MemoryModelError("不支持的模型用途")
        if type(revision) is not int or revision != data["revision"]:
            raise MemoryModelError("模型配置版本已变化，请重新打开设置")
        if base_url != data[role]["base_url"]:
            raise MemoryModelError("接口地址已变化，请先保存连接")
        return {"value": data[role]["api_key"]}

    def save(self, patch, revision, *, preview=False):
        if not isinstance(patch, dict) or set(patch) - {"shared", *MODELS}:
            raise MemoryModelError("模型配置字段不正确")
        with self.store.transaction():
            previous = self.private()
            if type(revision) is not int or revision != previous["revision"]:
                raise MemoryModelError("模型配置版本已变化，请重新打开设置")
            data = copy.deepcopy(previous)
            for role, changes in patch.items():
                allowed = {"base_url", "api_key", "clear_key"}
                if role != "shared":
                    allowed |= {"model", "custom"}
                    allowed.add("max_context_tokens")
                if role == "embedding":
                    allowed.add("dimensions")
                if role in ("summary", "assistant"):
                    allowed.add("mode")
                if not isinstance(changes, dict) or set(changes) - allowed:
                    raise MemoryModelError("模型配置字段不正确")
                item, old = data[role], previous[role]
                if (
                    any(
                        k not in ("api_key", "clear_key") and old.get(k) != v
                        for k, v in changes.items()
                    )
                    or changes.get("api_key")
                    or changes.get("clear_key")
                ):
                    item.pop("preset_id", None)
                if "max_context_tokens" in changes and (
                    type(changes["max_context_tokens"]) is not int
                    or not 1024 <= changes["max_context_tokens"] <= 2000000
                ):
                    raise MemoryModelError("模型窗口须为1024至2000000 token")
                for name in ("custom", "clear_key"):
                    if name in changes and type(changes[name]) is not bool:
                        raise MemoryModelError("模型开关格式不正确")
                item.update(
                    {
                        k: v
                        for k, v in changes.items()
                        if k not in ("api_key", "clear_key")
                    }
                )
                story = role in ("summary", "assistant")
                if story and item.get("mode") not in (
                    "custom",
                    "current" if role == "summary" else "summary",
                ):
                    raise MemoryModelError("请选择有效的模型连接方式")
                if (
                    role == "shared"
                    or item.get("base_url")
                    or (item.get("mode") == "custom" if story else item.get("custom"))
                ):
                    item["base_url"] = endpoint(item["base_url"])
                key = changes.get("api_key", "")
                if (
                    not isinstance(key, str)
                    or len(key) > 2000
                    or any(ord(c) < 33 or ord(c) > 126 for c in key)
                ):
                    raise MemoryModelError("API Key 格式不正确")
                if key and changes.get("clear_key"):
                    raise MemoryModelError("不能同时填写和清除 Key")
                if key:
                    item["api_key"] = key
                elif changes.get("clear_key"):
                    item["api_key"] = ""
                elif item["base_url"] != old["base_url"] and old["api_key"]:
                    raise MemoryModelError(
                        "接口地址已变化，请重新填写对应 Key 或勾选清除"
                    )
                if role != "shared" and (not story or item.get("mode") == "custom"):
                    model = item["model"]
                    if (
                        not isinstance(model, str)
                        or not 1 <= len(model) <= 200
                        or any(c.isspace() for c in model)
                    ):
                        raise MemoryModelError("模型名称格式不正确")
                if role == "embedding" and (
                    type(item["dimensions"]) is not int
                    or not 1 <= item["dimensions"] <= 4096
                ):
                    raise MemoryModelError("向量维度须为1至4096")
            if preview:
                return data
            data.pop("revision")
            self.store.db.execute(
                "INSERT INTO memory_model_settings VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                (revision + 1, json.dumps(data, ensure_ascii=False)),
            )
        self.cache.clear()
        return self.public()

    def _selected_config(self, role, settings, data):
        binding = self.selection(role, settings)
        mode = binding["mode"]
        if mode == "summary":
            return self._selected_config("summary", settings, data)
        if mode == "embedding":
            config = self._selected_config("embedding", settings, data)
            return {**config, "model": binding.get("model", MODELS["rerank"])}
        if mode == "preset":
            self.validate_bindings({role: binding}, data)
            return next(
                dict(p)
                for p in data.get("presets", [])
                if p["id"] == binding["preset_id"]
            )
        if mode == "legacy":
            if role == "assistant" and data[role]["mode"] == "summary":
                return self._selected_config("summary", settings, data)
            if role == "summary" and data[role]["mode"] == "current":
                raise MemoryModelError("此模型使用当前聊天连接")
            return self._legacy_config(role, data)
        raise MemoryModelError("此用途未选择插件内的独立连接")

    def resolve_plan_assistant(self, scope, cid, config=None):
        """Resolve only this task's saved binding; no ensure, history or I/O."""
        album = self.store.restore_guard.store
        with self.store.lock:
            policy = copy.deepcopy(config if config is not None else album.settings.get("plan_recovery", {}))
            if policy.get("assistant_enabled") is not True:
                raise MemoryModelError("ASSISTANT_DISABLED")
            binding = policy.get("assistant_binding", {"mode": "summary"})
            settings = {"model_bindings": {"summary": binding}}
            if binding == {"mode": "summary"}:
                row = self.store.db.execute("SELECT settings FROM mem_chats WHERE scope=? AND cid=?", (scope, cid)).fetchone()
                if row is None:
                    raise MemoryModelError("ASSISTANT_UNAVAILABLE")
                settings = json.loads(row[0])
            try:
                effective = settings.get("model_bindings", {}).get("summary", {})
                if effective.get("mode") != "preset" or not effective.get("preset_id"):
                    raise MemoryModelError("ASSISTANT_UNAVAILABLE")
                selected = self._selected_config("summary", settings, self.private())
                if not all(selected.get(key) for key in ("base_url", "model", "api_key")):
                    raise MemoryModelError("ASSISTANT_UNAVAILABLE")
                endpoint(selected["base_url"])
            except (MemoryModelError, KeyError, TypeError, ValueError):
                raise MemoryModelError("ASSISTANT_UNAVAILABLE") from None
            identity = [scope, cid, binding, effective,
                        {key: selected.get(key) for key in ("base_url", "model", "api_key", "max_context_tokens")}]
            signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            return MemoryModel("assistant", selected), signature

    def plan_assistant_signature(self, scope, cid):
        return self.resolve_plan_assistant(scope, cid)[1]

    def resolve_maintenance(self, scope, cid, config=None, *, manual=False):
        """Freeze a shared text connection for one exact existing chat, read-only."""
        from ..store import memory_maintenance_settings

        album = self.store.restore_guard.store
        with self.store.lock:
            policy = memory_maintenance_settings(config if config is not None else album.settings.get("memory_maintenance"))
            if not policy["enabled"]:
                raise MemoryModelError("MAINTENANCE_DISABLED")
            row = self.store.db.execute("SELECT enabled,settings FROM mem_chats WHERE scope=? AND cid=?", (scope, cid)).fetchone()
            if row is None or not row["enabled"]:
                raise MemoryModelError("MAINTENANCE_DISABLED")
            chat = json.loads(row["settings"])
            if not manual and (not policy["auto_after_summary"] or chat.get("maintenance_paused", False)):
                raise MemoryModelError("MAINTENANCE_PAUSED")
            binding = policy["binding"]
            effective = chat.get("model_bindings", {}).get("summary", {}) if binding == {"mode": "summary"} else binding
            try:
                if effective.get("mode") != "preset" or not effective.get("preset_id"):
                    raise MemoryModelError("MAINTENANCE_UNAVAILABLE")
                selected = self._selected_config("summary", {"model_bindings": {"summary": effective}}, self.private())
                window = selected.get("max_context_tokens", 32000)
                if not all(selected.get(key) for key in ("base_url", "model", "api_key")) or type(window) is not int or not 1024 <= window <= 2_000_000:
                    raise MemoryModelError("MAINTENANCE_UNAVAILABLE")
                endpoint(selected["base_url"])
                character = album.character_for(scope)["id"]
            except (AlbumError, KeyError, TypeError, ValueError):
                raise MemoryModelError("MAINTENANCE_UNAVAILABLE") from None
            signature_policy = {key: policy[key] for key in ("enabled", "binding", "max_calls", "unit_timeout", "manual_timeout")}
            if not manual:
                signature_policy.update(auto_after_summary=policy["auto_after_summary"], maintenance_paused=chat.get("maintenance_paused", False))
            identity = [scope, cid, character, effective, signature_policy, chat.get("summary_timeout", 180),
                {key: selected.get(key) for key in ("id", "base_url", "api_key", "model", "max_context_tokens")}]
            signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            return MemoryModel("assistant", copy.deepcopy(selected)), signature

    def maintenance_signature(self, scope, cid, *, manual=False):
        return self.resolve_maintenance(scope, cid, manual=manual)[1]

    def get(self, role, *, draft=None, settings=None):
        data = draft or self.private()
        if role not in MODELS:
            raise MemoryModelError("不支持的模型用途")
        if settings is not None:
            config = self._selected_config(role, settings, data)
            return MemoryModel(role, config)
        if draft is not None:
            config = data[role]
            connection = (
                config
                if role in ("summary", "assistant") or config["custom"]
                else data["shared"]
            )
            return MemoryModel(role, {**config, **connection, "model": config["model"]})
        if role not in self.cache or self.cache[role][0] != data["revision"]:
            config = data[role]
            if role in ("summary", "assistant") and config["mode"] != "custom":
                raise MemoryModelError("此模型使用共享聊天连接，请先选择一份聊天")
            connection = (
                config
                if role in ("summary", "assistant") or config["custom"]
                else data["shared"]
            )
            self.cache[role] = (
                data["revision"],
                MemoryModel(role, {**config, **connection, "model": config["model"]}),
            )
        return self.cache[role][1]

    def signature(self, role, *, settings=None):
        """Compare only the effective connection; unrelated saved roles do not cancel work."""
        data = self.private()
        if settings is not None:
            binding = self.selection(role, settings)
            if binding["mode"] == "summary":
                return self.signature("summary", settings=settings)
            if binding["mode"] == "embedding":
                value = [
                    self.signature("embedding", settings=settings),
                    binding.get("model", MODELS["rerank"]),
                ]
            elif binding["mode"] in ("current", "host", "disabled"):
                value = binding
            elif binding["mode"] == "legacy":
                if role == "assistant" and data[role]["mode"] == "summary":
                    return self.signature("summary", settings=settings)
                return self.signature(role)
            else:
                config = self._selected_config(role, settings, data)
                value = {
                    k: config.get(k)
                    for k in (
                        "base_url",
                        "api_key",
                        "model",
                        "dimensions",
                        "max_context_tokens",
                    )
                }
            return hashlib.sha256(
                json.dumps(value, sort_keys=True).encode()
            ).hexdigest()
        config = data[role]
        if role == "assistant" and config.get("mode") == "summary":
            return self.signature("summary")
        connection = (
            config
            if role in ("summary", "assistant") or config.get("custom")
            else data["shared"]
        )
        return hashlib.sha256(
            json.dumps(
                [
                    {k: v for k, v in c.items() if k != "preset_id"}
                    for c in (config, connection)
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()


class MemoryModel:
    def __init__(self, role, config):
        self.role, self.config = role, config
        self.provider_config = {
            "max_context_tokens": config.get("max_context_tokens", 32000)
        }
        self.identity = hashlib.sha256(
            json.dumps(
                [
                    role,
                    config["base_url"],
                    config["model"],
                    config.get("dimensions", 0),
                    "memory-text-v2",
                ]
            ).encode()
        ).hexdigest()

    async def request(self, path, payload, timeout, *, method="POST"):
        base = endpoint(self.config["base_url"])
        if not self.config["api_key"]:
            raise MemoryModelError("请在拾忆的模型连接中填写 API Key")
        raw = json.dumps(payload, ensure_ascii=False).encode()
        # Sanity bound only; batch size is decided by the configured model window.
        if len(raw) > 16 * 1024 * 1024:
            raise MemoryModelError("本次记忆请求超过16 MiB，请检查模型窗口设置")
        connector = aiohttp.TCPConnector(
            resolver=PublicResolver(urlsplit(base).hostname), limit=1
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout),
            trust_env=False,
        ) as session:
            send = session.get if method == "GET" else session.post
            async with send(
                base + path,
                **({"data": raw} if method == "POST" else {}),
                headers={
                    "Authorization": "Bearer " + self.config["api_key"],
                    "Content-Type": "application/json",
                },
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    error = MemoryResponseError("REQUEST_FAILED", http_status=response.status)
                    error.status = response.status
                    error.headers = {
                        "Retry-After": response.headers.get("Retry-After", "")
                    }
                    raise error
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise MemoryResponseError("RESPONSE_LIMIT", response_bytes=size)
                    chunks.append(chunk)
                try:
                    result = json.loads(b"".join(chunks))
                except (ValueError, UnicodeError):
                    raise MemoryResponseError("RESPONSE_JSON", response_bytes=size) from None
                if not isinstance(result, dict):
                    raise MemoryResponseError("RESPONSE_FORMAT", response_bytes=size)
                return result

    async def list_models(self):
        result = await self.request("/models", {}, 15, method="GET")
        rows = result.get("data")
        if not isinstance(rows, list):
            raise MemoryModelError("此接口未提供模型列表，可直接填写模型名称")
        return sorted(
            {
                r["id"]
                for r in rows[:2000]
                if isinstance(r, dict)
                and isinstance(r.get("id"), str)
                and 0 < len(r["id"]) <= 200
            }
        )

    @staticmethod
    def _image_url_valid(value):
        """Validate a URL before putting it in a multimodal request.

        This deliberately performs syntax checks only.  The provider is the
        component that downloads a remote image; the plugin must not resolve or
        fetch it while building a summary request.
        """
        if not isinstance(value, str):
            return False
        if value.startswith("data:"):
            if len(value) > 8 * 1024 * 1024:
                return False
            try:
                return bool(
                    re.fullmatch(
                        r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/]+={0,2}",
                        value,
                    )
                    and len(value.split(",", 1)[1]) % 4 == 0
                )
            except (IndexError, TypeError):
                return False
        try:
            parsed = urlsplit(value)
            host, port = parsed.hostname or "", parsed.port
            authority_host = f"[{host}]" if ":" in host else host
            valid = (
                len(value) <= 8192 and parsed.scheme in ("http", "https")
                and host and "%" not in host
                and parsed.username is None and parsed.password is None
                and (port is None or 1 <= port <= 65535)
                and re.fullmatch(re.escape(authority_host) + r"(?::[0-9]+)?", parsed.netloc, re.I)
                and not any(c.isspace() or ord(c) < 32 for c in value)
            )
            if not valid:
                return False
            try:
                ipaddress.ip_address(host)
            except ValueError:
                domain = host.removesuffix(".").encode("idna").decode("ascii")
                valid = (
                    len(domain) <= 253 and not re.fullmatch(r"[0-9.]+", domain)
                    and all(
                        re.fullmatch(
                            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label
                        )
                        for label in domain.split(".")
                    )
                )
            return valid
        except (ValueError, UnicodeError):
            return False

    @classmethod
    def _image_parts(cls, images):
        """Return safe image parts and stable labels for one summary call.

        Callers pass ``[{"id": "image-1", "url": "https://..."}, ...]``.
        The URL is only ever emitted in an ``image_url`` part.  IDs are used in
        adjacent text labels so a model can return a deterministic mapping.
        """
        if images is None:
            return []
        if not isinstance(images, (list, tuple)) or len(images) > IMAGE_BATCH_MAX:
            raise MemoryModelError("图片批次格式不正确")
        parts = []
        seen = set()
        for item in images:
            if not isinstance(item, dict):
                raise MemoryModelError("图片批次格式不正确")
            label = item.get("id")
            url = item.get("url")
            if (
                not isinstance(label, str)
                or not re.fullmatch(r"image-[1-9][0-9]{0,5}", label)
                or label in seen
                or not cls._image_url_valid(url)
            ):
                raise MemoryModelError("图片批次格式不正确")
            seen.add(label)
            # Keep the media URL in this part only; it must never be copied to
            # the ordinary prompt, logs, cache, or error text.
            parts.extend(
                [
                    {"type": "text", "text": f"图片编号：{label}"},
                    {"type": "image_url", "image_url": {"url": url}},
                ]
            )
        return parts

    async def summarize(self, **kwargs):
        images = kwargs.get("images")
        image_parts = self._image_parts(images)
        user_content = kwargs["prompt"]
        if image_parts:
            user_content = [{"type": "text", "text": kwargs["prompt"]}, *image_parts]
        payload = {
            "model": self.config["model"],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": kwargs["max_tokens"],
            "messages": [
                {"role": "system", "content": kwargs["system_prompt"]},
                {"role": "user", "content": user_content},
            ],
        }
        if self.config["model"].casefold() == "qwen/qwen3-8b":
            payload["enable_thinking"] = False
        if image_parts and len(json.dumps(payload, ensure_ascii=False).encode()) > IMAGE_REQUEST_BYTES:
            raise MemoryModelError("本批图片请求超过8 MiB，原图已保留")
        result = await self.request(
            "/chat/completions", payload, kwargs.get("timeout", 180)
        )
        choices = result.get("choices") if isinstance(result, dict) else None
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], dict)
        ):
            raise MemoryResponseError("RESPONSE_FORMAT", retryable=True)
        message = choices[0].get("message", {})
        body = message.get("content") if isinstance(message, dict) else None
        body_chars = len(body) if isinstance(body, str) else -1
        finish = choices[0].get("finish_reason")
        if finish not in ("stop", "end_turn", None):
            code = "RESPONSE_TRUNCATED" if finish == "length" else "CONTENT_SAFETY" if finish == "content_filter" else "RESPONSE_FINISH"
            raise MemoryResponseError(code, retryable=True, body_chars=body_chars)
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("tool_calls")
            or not isinstance(message.get("content"), str)
        ):
            raise MemoryResponseError("FIELD_TYPE" if not isinstance(body, str) else "RESPONSE_FORMAT", body_chars=body_chars)
        return SimpleNamespace(
            role="assistant", completion_text=message["content"], tools_call_name=[]
        )

    async def describe_images(self, images, *, timeout=60, max_tokens=2048):
        response = await self.summarize(
            system_prompt="只描述本批图片，不整理剧情。只输出JSON对象。" + IMAGE_DESCRIPTION_RULE,
            prompt="请按图片编号返回完整的 image_descriptions。", images=images,
            timeout=timeout, max_tokens=max_tokens,
        )
        parsed = parse_image_descriptions(response.completion_text, [item["id"] for item in images])
        if parsed is None:
            raise MemoryModelError("图片模型未返回完整可用描述")
        return parsed

    async def describe_image(self, url, *, timeout=60):
        """One bounded vision request. Input and remote bodies never enter errors."""
        if not self._image_url_valid(url):
            raise MemoryModelError("图片地址格式不正确")
        payload = {
            "model": self.config["model"], "stream": False,
            "temperature": 0.2, "max_tokens": 200,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "用一句中文（60字以内）客观描述图片里有什么：人物外观与动作、物件、地点、文字要点。不猜测真实身份，不评价，不输出网址、密钥或图片编码。只返回描述。"},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
        }
        result = await self.request("/chat/completions", payload, min(60, max(1, timeout)))
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise MemoryModelError("图片模型未返回可用描述")
        choice = choices[0]
        message = choice.get("message")
        if (
            choice.get("finish_reason") not in ("stop", "end_turn", None)
            or not isinstance(message, dict) or message.get("role") != "assistant"
            or message.get("tool_calls")
        ):
            raise MemoryModelError("图片模型未返回可用描述")
        description = clean_image_description(message.get("content"))
        if not description:
            raise MemoryModelError("图片模型未返回可用描述")
        return description

    async def get_embeddings(self, texts):
        result = await self.request(
            "/embeddings",
            {"model": self.config["model"], "input": texts, "encoding_format": "float"},
            20,
        )
        data = result.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise MemoryModelError("向量模型返回数量不一致")
        indices = [r.get("index") if isinstance(r, dict) else None for r in data]
        if any(type(i) is not int for i in indices) or sorted(indices) != list(
            range(len(texts))
        ):
            raise MemoryModelError("向量模型返回顺序不明确")
        vectors = [r.get("embedding") for r in sorted(data, key=lambda r: r["index"])]
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != self.config["dimensions"]
                or any(
                    type(v) not in (float, int) or not math.isfinite(v) for v in vector
                )
                or not any(vector)
            ):
                raise MemoryModelError("向量数值或维度无效")
        return vectors

    async def probe_dimensions(self):
        """Connection test: the dimension the model actually returns."""
        result = await self.request(
            "/embeddings",
            {"model": self.config["model"], "input": ["连接测试"], "encoding_format": "float"},
            20,
        )
        data = result.get("data")
        vector = data[0].get("embedding") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(vector, list) or not vector or not 1 <= len(vector) <= 4096:
            raise MemoryModelError("向量模型返回格式不正确")
        return len(vector)

    async def get_embedding(self, text):
        return (await self.get_embeddings([text]))[0]

    async def rerank(self, query, documents, top_n, timeout=8):
        result = await self.request(
            "/rerank",
            {
                "model": self.config["model"],
                "query": query,
                "documents": documents,
                "top_n": top_n,
                "return_documents": False,
            },
            timeout,
        )
        rows = result.get("results")
        if not isinstance(rows, list) or not 1 <= len(rows) <= top_n:
            raise MemoryModelError("精排响应格式不正确")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise MemoryModelError("精排响应格式不正确")
            index, score = row.get("index"), row.get("relevance_score")
            if (
                type(index) is not int
                or not 0 <= index < len(documents)
                or index in seen
                or type(score) not in (int, float)
                or not math.isfinite(score)
                or not 0 <= score <= 1
            ):
                raise MemoryModelError("精排得分或编号无效")
            seen.add(index)
        return [
            SimpleNamespace(index=r["index"], relevance_score=r["relevance_score"])
            for r in rows
        ]
