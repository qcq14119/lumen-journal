"""Bounded, read-only diagnostic projections; never export stored records whole."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from contextlib import closing
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .store import AlbumError, now
from .memory.store import DEFAULTS
from .memory.models import MODELS, MemoryResponseError
from .plans import DAY_MODES

MAX_EVENTS = 5000
MAX_BYTES = 5 * 1024 * 1024
MAX_DAYS = 31
MAX_OBJECTS = 100
MAX_TEXT = 16000
CHAT_PREVIEW_BYTES = 2 * 1024 * 1024
CHAT_PREVIEW_CANDIDATES = 48
FIELD_LIMIT = "[字段超限未收录]"
CATEGORIES = {"runs", "images", "memory", "recall"}
COMPONENTS = {"features", "models", "plans", "characters", "persona", "summary_prompt"}
TEXT_COMPONENTS = {"plans", "characters", "persona", "summary_prompt", "models"}
STATES = {
    "queued", "preparing", "submitting", "generating", "polling", "downloading",
    "running", "ready", "failed", "cancelled", "unknown", "sent", "held", "skipped",
    "expired", "deferred", "prepared", "sending", "pending", "none", "ok", "fallback",
    "saved", "summarizing", "summarized", "summary_failed", "retrying", "stopped",
    "image_described", "image_failed", "indexed", "index_failed", "recalled",
    "recall_fallback", "recall_failed", "inject_failed", "imported", "undone", "restored",
    "copied", "cleared", "download_failed", "complete", "completed", "waiting", "abandoned",
}
PHOTO_CODES = {
    "PHOTO_DISABLED", "PHOTO_NO_SCENE", "PHOTO_ENGINE_UNAVAILABLE", "TIMER_ONLY",
    "PHOTO_SUBMIT_FAILED", "PHOTO_TASK_CREATED",
}
CODES = PHOTO_CODES | {
    "TIMEOUT", "CONNECTION", "STORAGE", "VALIDATION", "INTERNAL", "PROTOCOL_THOUGHT",
    "PROTOCOL_STRUCTURE", "PROTOCOL_JSON", "PROTOCOL_FIELDS",
}
RECALL_STATES = {"recalled", "recall_fallback", "recall_failed", "indexed", "index_failed"}
FAILED_STATES = {"failed", "summary_failed", "image_failed", "recall_failed", "recall_fallback", "index_failed", "inject_failed"}
STAGES = {"generation", "validation", "sending", "history", "photo", "follow_up"}
ATTEMPT_CODES = {"authentication", "rate_limit", "http_error", "poll_timeout", "too_large", "redirect_limit", "local_wait_cancelled", "unsupported", "failed", "TimeoutError", "OSError", "ValueError", "ClientError", "ClientConnectorError", "ClientConnectionError", "ServerDisconnectedError", "ClientPayloadError", "ClientConnectorCertificateError", "ClientConnectorSSLError", "ConnectionTimeoutError", "SocketTimeoutError", "UnidentifiedImageError", "DecompressionBombWarning", "DecompressionBombError"}
MEMORY_FEATURES = {k for k, v in DEFAULTS.items() if type(v) in (bool, int, float)}
PLAN_FEATURES = {"enabled", "timezone", "cron", "window_before", "window_after", "photo_mode", "contact_mode", "run_once", "day_mode"}
IMAGE_POLICY = {"task_timeout", "poll_interval", "max_retries", "retry_delay", "parallel_backup", "retry_unknown"}
SECRET_FIELDS = {"api_key", "key", "token", "access_token", "refresh_token", "password", "secret", "cookie", "authorization"}
STANDARD_PATHS = {"", "/", "/v1", "/v1/chat/completions", "/v1/images/generations", "/v1/images/edits", "/v1/embeddings", "/v1/rerank", "/chat/completions", "/embeddings", "/rerank"}
URL_PATTERN = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|(?<!:)//)[^\s<>\"'）)]+")
PRIVATE_PATTERN = re.compile(
    r"(?i)(?:data:[^\s,;]+(?:;[^\s,]*)?,[^\s]+|base64:(?://)?[^\s]+|"
    r"(?:bearer\s+)[A-Za-z0-9._~+/=-]+|(?:sk|rk)-[A-Za-z0-9_-]{12,}|"
    r"(?:api[_ -]?key|token|password|secret|cookie|authorization)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+)"
)


async def _offload(function, *args):
    """Keep the caller's busy lease until its bounded worker actually exits."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Retrieve any worker failure without exposing it.
        raise


class DiagnosticError(AlbumError):
    """Only locally authored selection errors may be shown by the API."""


def _enum(value, allowed, unknown="UNKNOWN"):
    return value if isinstance(value, str) and value in allowed else unknown


def _number(value):
    return value if type(value) in (int, float) and -1 <= value <= 2**53 - 1 else None


def _field(path, alias=None, column="data", limit=MAX_TEXT):
    # Only module-owned paths/columns reach SQL. Oversize fields are omitted,
    # never partially read and mistaken for complete text or JSON.
    value = f"json_extract({column},'$.{path}')"
    return f"CASE WHEN {value} IS NULL THEN NULL WHEN length({value})<={limit} THEN {value} ELSE '[字段超限未收录]' END AS {alias or path}"


def _memory_diagnostic(state, detail):
    """Accept only a complete diagnostic previously emitted by our serializer."""
    if state not in {"summary_failed", "retrying"} or not isinstance(detail, str):
        return {}
    attempt, delay = None, None
    if state == "retrying":
        retry = re.fullmatch(r"第([0-9]{1,2})次：(.*)，(?:([0-9]{1,3})秒后重发本批全部图片|整批重新生成)", detail)
        if not retry:
            return {}
        attempt, detail = int(retry[1]), retry[2]
        delay = int(retry[3]) if retry[3] else None
    match = re.fullmatch(r"[^\[\]\r\n]+ \[code=([A-Z_]+)((?: [a-z_]+=-?[0-9]{1,10})*)\]", detail)
    if not match or match[1] not in MemoryResponseError.REASONS:
        return {}
    pairs = [item.split("=", 1) for item in match[2].split()]
    if len({key for key, _ in pairs}) != len(pairs) or any(key not in MemoryResponseError.COUNTERS for key, _ in pairs):
        return {}
    counts = {key: int(value) for key, value in pairs}
    rebuilt = MemoryResponseError(match[1], **counts)
    if str(rebuilt) != detail:
        return {}
    return {"reason": rebuilt.code, "counts": rebuilt.counts, **({"attempt": attempt} if attempt is not None else {}), **({"retry_delay": delay} if delay is not None else {})}


class Diagnostics:
    def __init__(self, store, context=None):
        self.store, self.context = store, context

    def _table(self, name):
        return bool(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def _secrets(self, scopes=()):
        """Read only credential fields; the values never leave this request."""
        values = set()

        def collect(secret):
            if not isinstance(secret, str) or not secret:
                return
            values.add(secret)
            bearer = re.fullmatch(r"(?i)\s*Bearer\s+(\S+)\s*", secret)
            if bearer:
                values.add(bearer[1])

        def walk(value, parent=""):
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = key.lower().replace("-", "_")
                    is_secret = normalized in SECRET_FIELDS or normalized == "proxy_authorization" or normalized.endswith(("_key", "_token", "_secret", "_password")) or (key == "value" and parent == "api_keys")
                    if is_secret and isinstance(child, str) and child:
                        collect(child)
                    elif is_secret and isinstance(child, list):
                        for secret in child:
                            collect(secret)
                    elif isinstance(child, (dict, list)):
                        walk(child, key)
            elif isinstance(value, list):
                for child in value:
                    walk(child, parent)

        walk(self.store.settings)
        if self.context and callable(getattr(self.context, "get_config", None)):
            walk(self.context.get_config())
            for scope in scopes:
                walk(self.context.get_config(umo=scope))
        if self.context and callable(getattr(self.context, "get_all_providers", None)):
            for provider in self.context.get_all_providers():
                walk(getattr(provider, "provider_config", {}))
        for table, where in (("memory_model_settings", "memory_model_settings.id=1"), ("records", "records.kind='calendar'")):
            if self._table(table):
                secret_rows = self.store.db.execute(
                    f"SELECT atom FROM {table},json_tree({table}.data) WHERE {where} "
                    "AND lower(replace(json_tree.key,'-','_')) IN ('api_key','x_api_key','key','token','x_auth_token','access_token','refresh_token','password','secret','cookie','authorization','proxy_authorization') "
                    "AND json_tree.type='text' LIMIT 2049"
                ).fetchall()
                if len(secret_rows) > 2048:
                    raise DiagnosticError("已知秘密数量超过安全预览上限，请缩小配置后重试")
                for row in secret_rows:
                    if row[0]:
                        collect(row[0])
        if len(values) > 4096 or sum(len(value) for value in values) > 1024 * 1024:
            raise DiagnosticError("已知秘密超过安全预览上限，无法生成诊断包")
        return sorted(values, key=len, reverse=True)

    @staticmethod
    def _address(value, secrets):
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            return "[地址未收录]"
        try:
            parsed = urlsplit(value if not value.startswith("//") else "https:" + value)
            host = parsed.hostname or ""
            if parsed.scheme not in ("http", "https") or not host or any(secret.casefold() in host.casefold() for secret in secrets):
                return "[地址已遮蔽]"
            if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
                return "[地址已遮蔽]"
            port = parsed.port
            if ":" in host:
                host = "[" + host + "]"
            return parsed.scheme + "://" + host + (f":{port}" if port else "") + (parsed.path if parsed.path in STANDARD_PATHS else "")
        except ValueError:
            return "[地址未收录]"

    def _text(self, value, secrets):
        if not isinstance(value, str):
            return ""
        if len(value) > MAX_TEXT:
            return "[文字超限未收录]"
        for secret in secrets:
            value = value.replace(secret, "[秘密已遮蔽]")
        value = PRIVATE_PATTERN.sub("[凭据或媒体已遮蔽]", value)
        return URL_PATTERN.sub(lambda match: self._address(match.group(), secrets), value)

    @staticmethod
    def _chat_identity(value):
        if not isinstance(value, dict) or set(value) != {"scope", "cid"} or any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 512 for k in value):
            raise DiagnosticError("请选择有效聊天")
        return value

    def _chat_metadata(self, scope, cid, secrets, row=None, availability="METADATA_UNAVAILABLE"):
        short_id = cid[:8] + ("…" + cid[-4:] if len(cid) > 12 else "")
        item = {"kind": "chat", "scope": scope, "cid": cid, "short_id": short_id,
                "label": "未命名聊天 · " + short_id, "platform": "", "created_at": None,
                "updated_at": None, "availability": availability}
        if row is not None:
            item.update(label=self._text(row["title"], secrets) or item["label"],
                        platform=self._text(row["platform_id"], secrets), availability="AVAILABLE")
            for field in ("created_at", "updated_at"):
                try:
                    value = datetime.fromisoformat(row[field])
                    item[field] = int(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
                except (TypeError, ValueError, OverflowError):
                    pass
        return item

    def _host_path(self):
        # Reading the configured SQLite path does not initialize the host DB.
        try:
            database = self.context.get_db()
            path = getattr(database, "db_path", None)
            return Path(path).resolve().as_uri() + "?mode=ro" if isinstance(path, (str, Path)) and str(path) != ":memory:" else None
        except Exception:
            return None

    def _options_local(self):
        result, truncated = [], False
        secrets = self._secrets()
        for kind, label in (("life_plan", "plan"), ("character", "character")):
            rows = self.store.db.execute(
                f"SELECT id,{_field('name')},{_field('scope')} FROM records WHERE kind=? "
                "AND json_extract(data,'$.deleted') IS NOT 1 ORDER BY rowid DESC LIMIT ?", (kind, MAX_OBJECTS + 1)
            ).fetchall()
            truncated |= len(rows) > MAX_OBJECTS
            for row in rows[:MAX_OBJECTS]:
                item = {"kind": label, "id": row["id"], "label": self._text(row["name"], secrets) or "未命名"}
                if label == "plan":
                    item["scope"] = row["scope"] or ""
                result.append(item)
        if self._table("mem_chats"):
            chats = self.store.db.execute("SELECT scope,cid FROM mem_chats ORDER BY updated DESC LIMIT ?", (MAX_OBJECTS + 1,)).fetchall()
            truncated |= len(chats) > MAX_OBJECTS
            result.extend(self._chat_metadata(row["scope"], row["cid"], secrets) for row in chats[:MAX_OBJECTS])
        return result, secrets, truncated

    def _options_host(self, address, result, secrets, current):
        chats = {(row["scope"], row["cid"]): row for row in result if row["kind"] == "chat"}
        if current:
            chats.setdefault((current["scope"], current["cid"]), self._chat_metadata(current["scope"], current["cid"], secrets))
        truncated = False
        try:
            if not address:
                raise ValueError("Unsupported metadata interface")
            with closing(sqlite3.connect(address, uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute("SELECT user_id,conversation_id,CASE WHEN length(title)<=255 THEN title END AS title,substr(platform_id,1,100) AS platform_id,created_at,updated_at FROM conversations ORDER BY updated_at DESC,inner_conversation_id DESC LIMIT ?", (MAX_OBJECTS + 1,)).fetchall()
                truncated = len(rows) > MAX_OBJECTS
                for row in rows[:MAX_OBJECTS]:
                    key = (row["user_id"], row["conversation_id"])
                    chats[key] = self._chat_metadata(*key, secrets, row)
                # Existing memory chats and the exact entry may lie beyond the page.
                for key, value in list(chats.items()):
                    if value["availability"] == "AVAILABLE":
                        continue
                    row = db.execute("SELECT CASE WHEN length(title)<=255 THEN title END AS title,substr(platform_id,1,100) AS platform_id,created_at,updated_at FROM conversations WHERE user_id=? AND conversation_id=? LIMIT 1", key).fetchone()
                    chats[key] = self._chat_metadata(*key, secrets, row, "CHAT_UNAVAILABLE")
        except (sqlite3.Error, ValueError, OSError):
            pass
        for index, (key, row) in enumerate(chats.items()):
            row["id"] = f"chat-option-{index}"
            row["current"] = bool(current and key == (current["scope"], current["cid"]))
        return [row for row in result if row["kind"] != "chat"] + list(chats.values()), truncated

    async def options(self, current=None):
        if current is not None:
            self._chat_identity(current)
        result, secrets, truncated = await _offload(self._read_local, "_options_local")
        result, host_truncated = await _offload(self._options_host, self._host_path(), result, secrets, current)
        return {"objects": result, "limits": {"max_days": MAX_DAYS, "max_events": MAX_EVENTS, "max_bytes": MAX_BYTES, "max_objects": MAX_OBJECTS}, "truncated": truncated or host_truncated}

    def _preview_text(self, value, secrets):
        if not isinstance(value, str) or len(value) > 4096:
            return ""
        value = re.sub(r"(?is)<(think(?:ing)?|analysis|system_reminder|tool_result)\b[^>]*>.*?</\1\s*>", "", value)
        if re.search(r"(?i)</?(?:think|thinking|analysis|system_reminder|tool_result)\b|\[Tool result for ", value):
            return ""
        value = self._text(value, secrets)
        value = URL_PATTERN.sub("[链接]", value)
        value = re.sub(r"(?i)(?:www\.|(?<![\w-])(?:[a-z0-9-]+\.)+(?:com|net|org|cn|io|test|invalid)\b)[^\s<>]*", "[链接]", value)
        value = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9])", "[媒体]", value)
        # Keep one extra character inside SQLite to report display truncation.
        return value.strip()[:201]

    def _chat_preview_readonly(self, address, chat, secrets):
        metadata = self._chat_metadata(chat["scope"], chat["cid"], secrets)
        result = {"metadata": metadata, "messages": [], "availability": "CONTENT_UNAVAILABLE",
                  "limits": {"messages": 6, "chars_per_message": 200, "total_chars": 2000}, "truncated": False}
        if not address:
            return result
        try:
            with closing(sqlite3.connect(address, uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN")
                row = db.execute("SELECT CASE WHEN length(title)<=255 THEN title END AS title,substr(platform_id,1,100) AS platform_id,created_at,updated_at FROM conversations WHERE user_id=? AND conversation_id=? LIMIT 1", (chat["scope"], chat["cid"])).fetchone()
                if row is None:
                    result["metadata"]["availability"] = result["availability"] = "CHAT_UNAVAILABLE"
                    return result
                result["metadata"] = self._chat_metadata(chat["scope"], chat["cid"], secrets, row)
                # octet_length(column) reads the record length without loading
                # a giant text value. Older SQLite keeps metadata-only preview.
                size = db.execute("SELECT octet_length(content) FROM conversations WHERE user_id=? AND conversation_id=? LIMIT 1", (chat["scope"], chat["cid"])).fetchone()[0]
                if size and size > CHAT_PREVIEW_BYTES:
                    result.update(availability="CONTENT_TOO_LARGE", truncated=True)
                    return result
                db.create_function("diagnostic_preview_text", 1, lambda value: self._preview_text(value, secrets), deterministic=True)
                # SQLite projects only bounded text and image sentinels. Full
                # message JSON, image URLs and the content column never enter Python.
                rows = db.execute("""
                    WITH candidates AS MATERIALIZED (
                      SELECT CAST(m.key AS INTEGER) AS position, json_extract(m.value,'$.role') AS role,
                        json_type(m.value,'$.content') AS content_type, json_extract(m.value,'$.content') AS body
                      FROM conversations c,json_each(CASE WHEN json_valid(c.content) THEN c.content ELSE '[]' END) m
                      WHERE c.user_id=? AND c.conversation_id=? AND m.type='object'
                        AND json_extract(m.value,'$.role') IN ('user','assistant')
                        AND (json_type(m.value,'$.tool_calls') IS NULL OR json_type(m.value,'$.tool_calls')='null'
                          OR (json_type(m.value,'$.tool_calls')='array' AND json_array_length(json_extract(m.value,'$.tool_calls'))=0))
                        AND coalesce(json_extract(m.value,'$._no_save'),0)!=1
                      ORDER BY position DESC LIMIT ?
                    ), projected AS MATERIALIZED (
                      SELECT position,role,CASE WHEN content_type='array' AND json_array_length(body)>12 THEN 1 ELSE 0 END AS too_many_parts,
                        CASE WHEN content_type='text' THEN body
                          WHEN content_type='array' AND json_array_length(body)<=12 THEN (
                            SELECT group_concat(piece,'') FROM (
                              SELECT CASE WHEN json_extract(p.value,'$.type')='image_url' THEN '[图片]'
                                WHEN json_extract(p.value,'$.type')='text' AND json_type(p.value,'$.text')='text'
                                  THEN json_extract(p.value,'$.text') ELSE '' END AS piece
                              FROM json_each(body) p WHERE p.type='object'
                                AND coalesce(json_extract(p.value,'$._no_save'),0)!=1
                                AND coalesce(json_extract(p.value,'$.thought'),0)!=1
                              ORDER BY CAST(p.key AS INTEGER)
                            )) ELSE '' END AS body
                      FROM candidates WHERE position IN (SELECT position FROM candidates ORDER BY position DESC LIMIT ?)
                    ), cleaned AS MATERIALIZED (
                      SELECT position,role,CASE WHEN length(body)<=4096 THEN diagnostic_preview_text(body) ELSE '' END AS speech,
                        CASE WHEN length(body)>4096 OR too_many_parts THEN 1 ELSE 0 END AS limited FROM projected
                    )
                    SELECT role,substr(speech,1,200) AS speech,NULL AS examined,NULL AS usable,NULL AS limited
                      FROM (SELECT role,speech FROM cleaned WHERE speech!='' ORDER BY position DESC LIMIT 6)
                    UNION ALL SELECT NULL,NULL,(SELECT count(*) FROM candidates),
                      (SELECT count(*) FROM cleaned WHERE speech!=''),
                      (SELECT coalesce(sum(limited OR length(speech)>200),0) FROM cleaned)
                """, (chat["scope"], chat["cid"], CHAT_PREVIEW_CANDIDATES + 1, CHAT_PREVIEW_CANDIDATES)).fetchall()
                statistics = rows.pop()
                result["messages"] = [{"role": r["role"], "text": r["speech"]} for r in reversed(rows)]
                result["availability"] = "AVAILABLE" if rows else "EMPTY"
                result["truncated"] = statistics["examined"] > CHAT_PREVIEW_CANDIDATES or statistics["usable"] > 6 or bool(statistics["limited"])
        except (sqlite3.Error, ValueError, TypeError, OSError):
            result["availability"] = "CONTENT_UNAVAILABLE"
        return result

    async def chat_preview(self, payload):
        chat = self._chat_identity(payload)
        secrets = await _offload(self._read_local, "_secrets", (chat["scope"],))
        return await _offload(self._chat_preview_readonly, self._host_path(), chat, secrets)

    def _selection(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"categories", "start", "end", "objects", "include", "filters"}:
            raise DiagnosticError("诊断选择格式不正确")
        categories, include = payload.get("categories"), payload.get("include", [])
        if not isinstance(categories, list) or not categories or len(categories) > 4 or any(not isinstance(x, str) or x not in CATEGORIES for x in categories):
            raise DiagnosticError("请选择有效的日志类别")
        if not isinstance(include, list) or len(include) > 6 or any(not isinstance(x, str) or x not in COMPONENTS for x in include):
            raise DiagnosticError("附带信息选择不正确")
        start, end = payload.get("start"), payload.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 2**40 or end - start > MAX_DAYS * 86400:
            raise DiagnosticError("请选择不超过31天的有效时间范围")
        objects = payload.get("objects")
        if not isinstance(objects, dict) or set(objects) - {"all", "plans", "characters", "chats"} or type(objects.get("all", False)) is not bool:
            raise DiagnosticError("对象选择不正确")
        objects = {"all": False, "plans": [], "characters": [], "chats": [], **objects}
        for key in ("plans", "characters"):
            if not isinstance(objects[key], list) or len(objects[key]) > MAX_OBJECTS or any(not isinstance(x, str) or not 1 <= len(x) <= 256 for x in objects[key]):
                raise DiagnosticError("所选对象数量或编号不正确")
            objects[key] = list(dict.fromkeys(objects[key]))
        if not isinstance(objects["chats"], list) or len(objects["chats"]) > MAX_OBJECTS:
            raise DiagnosticError("所选聊天数量不正确")
        for chat in objects["chats"]:
            if not isinstance(chat, dict) or set(chat) != {"scope", "cid"} or any(not isinstance(chat[k], str) or not 1 <= len(chat[k]) <= 512 for k in chat):
                raise DiagnosticError("请选择有效聊天")
        count = sum(len(objects[k]) for k in ("plans", "characters", "chats"))
        if count > MAX_OBJECTS or (not objects["all"] and not count):
            raise DiagnosticError("请明确选择全部对象或不超过100个指定对象")
        if include and not count:
            raise DiagnosticError("请选择附带信息对应的计划、角色或聊天")
        for component, key in (("plans", "plans"), ("characters", "characters"), ("persona", "chats"), ("summary_prompt", "chats")):
            if component in include and not objects[key]:
                raise DiagnosticError("请勾选附带文字对应的具体对象")
        filters = payload.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - {"status", "query"}:
            raise DiagnosticError("日志筛选不正确")
        status, query = filters.get("status", ""), filters.get("query", "")
        if not isinstance(status, str) or (status and status not in STATES):
            raise DiagnosticError("日志状态不正确")
        if not isinstance(query, str) or len(query) > 100:
            raise DiagnosticError("搜索文字最多100字")
        return set(categories), set(include), start, end, objects, status, query

    async def preview(self, payload):
        self._selection(payload)
        document, secrets, objects, ref = await _offload(self._collect_readonly, payload)
        configuration = document["current_configuration"]
        include = document["selection"]["include"]
        for entry in configuration:
            scope = entry.pop("_host_scope", "")
            if "host_model" in entry:
                entry["host_model"] = await self._host_model(scope, secrets)
        if "persona" in include:
            for chat in objects["chats"]:
                configuration.append(await self._persona(chat, secrets, ref))
        return await _offload(self._serialize, document)

    def _collect_readonly(self, payload):
        return self._read_local("_collect", payload)

    def _read_local(self, method, *args):
        # Store settings are replaced atomically on save. Freeze that reference;
        # never borrow the writer connection or hold its lock while exporting.
        snapshot = copy.copy(self.store)
        snapshot.settings = copy.deepcopy(self.store.settings)
        snapshot.lock = threading.RLock()
        address = (self.store.root / "album.sqlite3").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(address, uri=True, timeout=1)) as database:
            database.row_factory = sqlite3.Row
            snapshot.db = database
            database.execute("BEGIN")
            return getattr(Diagnostics(snapshot, self.context), method)(*args)

    def _collect(self, payload):
        categories, include, start, end, objects, status, query = self._selection(payload)
        aliases, alias_counts, events, snapshots, missing, omitted = {}, {}, [], [], set(), set()
        truncated = False

        def ref(kind, value):
            if not value:
                return None
            key = (kind, value if isinstance(value, str) else tuple(value))
            if key not in aliases:
                alias_counts[kind] = alias_counts.get(kind, 0) + 1
                aliases[key] = f"{kind}_{alias_counts[kind]}"
            return aliases[key]

        def rows(sql, args=()):
            nonlocal truncated
            limit = max(0, MAX_EVENTS - len(events))
            cursor = self.store.db.execute(sql + " LIMIT ?", (*args, limit + 1))
            try:
                for index, row in enumerate(cursor):
                    if index == limit:
                        truncated = True
                        break
                    if FIELD_LIMIT in row:
                        truncated = True
                        missing.add("FIELD_LIMIT")
                    yield row
            finally:
                cursor.close()

        def matching(scope_col, cid_col, character_col=None, plan_col=None):
            if objects["all"]:
                return "1", []
            clauses, args = [], []
            for chat in objects["chats"]:
                clauses.append(f"({scope_col}=? AND {cid_col}=?)")
                args.extend((chat["scope"], chat["cid"]))
            if character_col and objects["characters"]:
                clauses.append(f"{character_col} IN ({','.join('?' for _ in objects['characters'])})")
                args.extend(objects["characters"])
            if plan_col and objects["plans"]:
                clauses.append(f"{plan_col} IN ({','.join('?' for _ in objects['plans'])})")
                args.extend(objects["plans"])
            return "(" + " OR ".join(clauses) + ")" if clauses else "0", args

        def filtered(state_col, search_cols, memory=False):
            clauses, args = [], []
            if status:
                if memory and status == "failed":
                    clauses.append(f"{state_col} IN ({','.join('?' for _ in FAILED_STATES)})")
                    args.extend(sorted(FAILED_STATES))
                else:
                    clauses.append(f"{state_col}=?")
                    args.append(status)
            if query:
                clauses.append("(" + " OR ".join(f"instr(lower(coalesce({col},'')),lower(?))>0" for col in search_cols) + ")" if search_cols else "0")
                args.extend([query] * len(search_cols))
            return (" AND " + " AND ".join(clauses) if clauses else ""), args

        def stages(value):
            result = []
            try:
                raw = json.loads(value or "[]")
            except (ValueError, TypeError):
                raw = []
            if not isinstance(raw, list):
                return result
            for stage in raw[:20]:
                if not isinstance(stage, dict) or stage.get("stage") not in STAGES:
                    continue
                result.append({"stage": stage["stage"], "status": _enum(stage.get("status"), STATES), "code": _enum(stage.get("code"), CODES, "NOT_RECORDED"), "at": _number(stage.get("at"))})
            return result

        stamp = now()
        with self.store.lock:
            scopes = {chat["scope"] for chat in objects["chats"]}
            for identifier in objects["plans"]:
                row = self.store.db.execute("SELECT json_extract(data,'$.scope') FROM records WHERE kind='life_plan' AND id=?", (identifier,)).fetchone()
                if row and isinstance(row[0], str):
                    scopes.add(row[0])
            secrets = self._secrets(scopes)
            run_jobs, selected_jobs, run_origins = set(), set(), set()
            run_owners = {}
            if "runs" in categories or ("images" in categories and objects["plans"]):
                character_filter = "coalesce(json_extract(data,'$.character_id'),(SELECT json_extract(p.data,'$.character_id') FROM records p WHERE p.kind='life_plan' AND p.id=json_extract(records.data,'$.plan_id')))"
                condition, args = matching("json_extract(data,'$.scope')", "json_extract(data,'$.conversation_id')", character_filter, "json_extract(data,'$.plan_id')")
                extra, fargs = filtered("json_extract(data,'$.state')", ["id", "json_extract(data,'$.name')", "json_extract(data,'$.error')"])
                fields = ["scope", "conversation_id", "character_id", "plan_id", "at", "state", "job_id", "archived", "stages", "photo_mode"]
                for row in rows("SELECT id," + ",".join(_field(k, limit=8000) for k in fields) + " FROM records WHERE kind='life_run' AND json_extract(data,'$.at') BETWEEN ? AND ? AND " + condition + extra + " ORDER BY json_extract(data,'$.at') DESC,rowid DESC", (start, end, *args, *fargs)):
                    identifier = ref("run", row["id"])
                    item = {"category": "runs", "kind": "run", "id": identifier, "plan": ref("plan", row["plan_id"]), "chat": ref("chat", (row["scope"], row["conversation_id"])) if row["scope"] and row["conversation_id"] else None, "state": _enum(row["state"], STATES), "at": _number(row["at"]), "archived": row["archived"] == 1, "stages": stages(row["stages"]), "photo_reason": "NOT_RECORDED"}
                    for stage in item["stages"]:
                        if stage["stage"] == "photo":
                            item["photo_reason"] = stage["code"]
                            if stage["status"] == "ok":
                                item["photo_result"] = "TASK_CREATED"
                    if row["job_id"]:
                        run_jobs.add(row["job_id"])
                        item["job"] = ref("job", row["job_id"])
                    run_origins.add("life:" + row["id"])
                    run_owners[row["id"]] = (row["scope"], row["conversation_id"])
                    if "runs" in categories:
                        snapshots.append({"event": identifier, "photo_mode": _enum(row["photo_mode"], {"auto", "always", "never"}, "NOT_RECORDED")})
                        events.append(item)
            if "images" in categories:
                condition, args = matching("scope", "json_extract(data,'$.conversation_id')", "json_extract(data,'$.character_id')")
                extra, fargs = filtered("state", ["id", "json_extract(data,'$.scene')", "json_extract(data,'$.error')", "json_extract(data,'$.character_name')"])
                # Explicit run -> job links bypass the time and status window.
                related_runs = sorted(run_owners)
                related_clause = (
                    " OR EXISTS(SELECT 1 FROM records r WHERE r.kind='life_run' AND r.id IN (" + ",".join("?" for _ in related_runs) + ") "
                    "AND json_extract(r.data,'$.scope')=jobs.scope "
                    "AND coalesce(json_extract(r.data,'$.conversation_id'),'')=coalesce(json_extract(jobs.data,'$.conversation_id'),'') "
                    "AND (json_extract(r.data,'$.job_id')=jobs.id OR 'life:'||r.id=json_extract(jobs.data,'$.origin_proactive_id')))"
                    if related_runs else ""
                )
                fields = ["created", "character_id", "conversation_id", "origin_proactive_id", "archived", "delivery", "qzone_delivery", "notification_state", "caption_delivery", "attempts"]
                found_jobs = set()
                for row in rows("SELECT id,scope,state,updated," + ",".join(_field(k, limit=12000) for k in fields) + " FROM jobs WHERE ((json_extract(data,'$.created') BETWEEN ? AND ? AND " + condition + extra + ")" + related_clause + ") ORDER BY updated DESC,rowid DESC", (start, end, *args, *fargs, *related_runs)):
                    found_jobs.add(row["id"])
                    selected_jobs.add(row["id"])
                    item = {"category": "images", "kind": "job", "id": ref("job", row["id"]), "chat": ref("chat", (row["scope"], row["conversation_id"])) if row["conversation_id"] else None, "character": ref("character", row["character_id"]), "state": _enum(row["state"], STATES), "at": _number(row["created"]), "updated": _number(row["updated"]), "archived": row["archived"] == 1, "linked_outside_window": (row["id"] in run_jobs or row["origin_proactive_id"] in run_origins) and not start <= (row["created"] or 0) <= end, "reason": "NOT_RECORDED"}
                    if row["origin_proactive_id"] in run_origins:
                        item["origin_run"] = ref("run", row["origin_proactive_id"][5:])
                    for key in ("delivery", "qzone_delivery", "notification_state", "caption_delivery"):
                        item[key] = _enum(row[key], STATES, "NOT_RECORDED")
                    try:
                        attempts = json.loads(row["attempts"] or "[]")
                    except (ValueError, TypeError):
                        attempts = []
                    item["attempts"] = [
                        {"state": _enum(a.get("state"), STATES), "stage": _enum(a.get("stage"), {"prepare", "submit", "poll", "download", "provider"}, "NOT_RECORDED"), "code": _enum(a.get("code"), ATTEMPT_CODES, "NOT_RECORDED" if not a.get("code") else "UNKNOWN"), **{key: _number(a.get(key)) for key in ("at", "finished", "http_status", "download_attempts", "retry_after")}}
                        for a in attempts[:100] if isinstance(a, dict)
                    ] if isinstance(attempts, list) else []
                    if isinstance(attempts, list) and len(attempts) > 100:
                        item["attempts_truncated"] = True
                        truncated = True
                        missing.add("FIELD_LIMIT")
                    events.append(item)
                if run_jobs - found_jobs:
                    missing.add("LINKED_JOB_UNAVAILABLE_OR_TRUNCATED")
                condition, args = matching("json_extract(data,'$.scope')", "json_extract(data,'$.conversation_id')", "json_extract(data,'$.character_id')")
                extra, fargs = filtered("state", ["id", "json_extract(data,'$.error')"])
                related = sorted(selected_jobs)
                linked = " OR json_extract(data,'$.job_id') IN (" + ",".join("?" for _ in related) + ")" if related else ""
                fields = ["created", "job_id", "archived", "target", "message_id", "tid"]
                for row in rows("SELECT id,state," + ",".join(_field(k) for k in fields) + " FROM deliveries WHERE ((json_extract(data,'$.created') BETWEEN ? AND ? AND " + condition + extra + ")" + linked + ") ORDER BY json_extract(data,'$.created') DESC,rowid DESC", (start, end, *args, *fargs, *related)):
                    events.append({"category": "images", "kind": "delivery", "id": ref("delivery", row["id"]), "job": ref("job", row["job_id"]), "at": _number(row["created"]), "state": _enum(row["state"], STATES), "target": _enum(row["target"], {"chat", "qzone"}), "archived": row["archived"] == 1, "receipt_recorded": bool(row["message_id"] or row["tid"]), "linked_outside_window": not start <= (row["created"] or 0) <= end, "reason": "NOT_RECORDED"})
            if categories & {"memory", "recall"}:
                if not objects["all"] and not objects["chats"]:
                    missing.add("MEMORY_CHAT_NOT_SELECTED")
                if not self._table("mem_logs"):
                    missing.add("MEMORY_LOG_TABLE_UNAVAILABLE")
                else:
                    condition, args = matching("scope", "cid")
                    extra, fargs = filtered("state", ["detail"], memory=True)
                    for row in rows("SELECT id,scope,cid,at,substr(state,1,60) AS state,CASE WHEN state IN ('summary_failed','retrying') AND length(detail)<=1000 THEN detail END AS diagnostic FROM mem_logs WHERE at BETWEEN ? AND ? AND " + condition + (" AND state IN (" + ",".join("?" for _ in RECALL_STATES) + ")" if categories & {"memory", "recall"} == {"recall"} else " AND state NOT IN (" + ",".join("?" for _ in RECALL_STATES) + ")" if "recall" not in categories else "") + extra + " ORDER BY at DESC,id DESC", (start, end, *args, *(sorted(RECALL_STATES) if not {"memory", "recall"} <= categories else []), *fargs)):
                        events.append({"category": "recall" if row["state"] in RECALL_STATES else "memory", "kind": "memory_log", "id": ref("log", str(row["id"])), "chat": ref("chat", (row["scope"], row["cid"])), "at": _number(row["at"]), "state": _enum(row["state"], STATES), "reason": "NOT_RECORDED", "detail": "NOT_INCLUDED", **_memory_diagnostic(row["state"], row["diagnostic"])})
                if "memory" in categories and self._table("mem_images"):
                    condition, args = matching("scope", "cid")
                    extra, fargs = filtered("status", [], memory=True)
                    # Image descriptions are never selected; these are current
                    # index states, not a fabricated history of every attempt.
                    for row in rows("SELECT scope,cid,ref,status,attempts,updated FROM mem_images WHERE updated BETWEEN ? AND ? AND " + condition + extra + " ORDER BY updated DESC,rowid DESC", (start, end, *args, *fargs)):
                        events.append({"category": "memory", "kind": "image_status", "id": ref("image", (row["scope"], row["cid"], row["ref"])), "chat": ref("chat", (row["scope"], row["cid"])), "at": _number(row["updated"]), "state": _enum(row["status"], STATES), "attempts": _number(row["attempts"]), "observation": "CURRENT_INDEX_STATE", "reason": "NOT_RECORDED"})
            configuration = self._configuration(include, objects, secrets, ref, missing, stamp, categories)
        for component in COMPONENTS - include:
            omitted.add(component)
        counts = {key: sum(event["category"] == key for event in events) for key in sorted(categories)}
        metadata = Path(__file__).with_name("metadata.yaml")
        version = "NOT_RECORDED"
        if metadata.is_file() and metadata.stat().st_size < 16384:
            match = re.search(r"(?m)^version:\s*[\"']?([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)[\"']?\s*$", metadata.read_text(encoding="utf-8"))
            if match:
                version = match[1]
        document = {"format": "companion_album_diagnostics", "version": 1, "created_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat(), "selection": {"start": start, "end": end, "timezone": "UTC", "categories": sorted(categories), "all_objects": objects["all"], "object_counts": {k: len(objects[k]) for k in ("plans", "characters", "chats")}, "status": status or None, "has_query": bool(query)}, "events": events, "execution_snapshots": snapshots, "current_configuration": configuration, "included_components": sorted(include), "omitted_components": sorted(omitted), "missing": sorted(missing), "truncated": truncated, "limits": {"events": MAX_EVENTS, "bytes": MAX_BYTES, "days": MAX_DAYS}, "counts": counts, "notes": ["CURRENT_CONFIGURATION_IS_NOT_EXECUTION_HISTORY", "MISSING_METRICS_ARE_NOT_RECONSTRUCTED", "LINKED_IMAGE_RESULTS_MAY_BE_OUTSIDE_WINDOW"] + (["OPTIONAL_TEXT_MAY_CONTAIN_PRIVATE_FACTS"] if include & TEXT_COMPONENTS else [])}
        document["plugin_version"] = version
        document["selection"]["include"] = sorted(include)
        if objects["characters"] and "runs" in categories:
            document["notes"].append("CHARACTER_FILTER_USES_CURRENT_PLAN_WHEN_RUN_CHARACTER_NOT_RECORDED")
        if "FIELD_LIMIT" in missing or FIELD_LIMIT in json.dumps(configuration, ensure_ascii=False):
            document["truncated"] = True
            document["missing"] = sorted(set(document["missing"]) | {"FIELD_LIMIT"})
        return document, secrets, objects, ref

    @staticmethod
    def _serialize(document):
        stamp = int(datetime.fromisoformat(document["created_at"]).timestamp())
        categories = document["selection"]["categories"]
        snapshots = document["execution_snapshots"]
        raw = json.dumps(document, ensure_ascii=False, indent=2)
        # Reserve a small envelope for counts/component labels recomputed after
        # dropping whole entries; the final JSON must stay below MAX_BYTES.
        byte_budget = MAX_BYTES - 256
        # Trim whole entries by binary search, avoiding quadratic serialization
        # when a populated selection exceeds the downloadable byte budget.
        for key in ("current_configuration", "events"):
            if len(raw.encode("utf-8")) <= byte_budget:
                break
            document["truncated"] = True
            document["missing"] = sorted(set(document["missing"]) | {"BYTE_LIMIT"})
            original = document[key]
            low, high = 0, len(original)
            while low < high:
                mid = (low + high + 1) // 2
                document[key] = original[:mid]
                if len(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")) <= byte_budget:
                    low = mid
                else:
                    high = mid - 1
            document[key] = original[:low]
            raw = json.dumps(document, ensure_ascii=False, indent=2)
        kept = {event["id"] for event in document["events"]}
        document["execution_snapshots"] = [s for s in snapshots if s["event"] in kept]
        document["counts"] = {key: sum(event["category"] == key for event in document["events"]) for key in sorted(categories)}
        actual_components = set()
        for entry in document["current_configuration"]:
            for field, component in (("features", "features"), ("plan", "plans"), ("drawing", "characters"), ("summary_prompt", "summary_prompt"), ("models", "models"), ("host_model", "models"), ("sites", "models")):
                if field in entry:
                    actual_components.add(component)
            if entry.get("component") == "persona":
                actual_components.add("persona")
        document["included_components"] = sorted(actual_components)
        document["omitted_components"] = sorted(COMPONENTS - actual_components)
        raw = json.dumps(document, ensure_ascii=False, indent=2)
        actual = {kind: set() for kind in ("plan", "character", "chat")}
        for row in (*document["events"], *document["current_configuration"]):
            for value in (row.get("object"), row.get("plan"), row.get("character"), row.get("chat")):
                if isinstance(value, str):
                    for kind in actual:
                        if re.fullmatch(kind + r"_[0-9]+", value):
                            actual[kind].add(value)
        return {"document": document, "json": raw, "bytes": len(raw.encode("utf-8")), "filename": "lumen-diagnostics-" + datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y%m%d-%H%M%S") + ".json", "summary": {"counts": document["counts"], "truncated": document["truncated"], "missing": document["missing"], "included_components": document["included_components"], "omitted_components": document["omitted_components"], "free_text_components": sorted(actual_components & TEXT_COMPONENTS), "object_counts": {kind + "s": len(values) for kind, values in actual.items()}}}

    def _configuration(self, include, objects, secrets, ref, missing, stamp, categories):
        result = []
        for identifier in objects["plans"]:
            if not include & {"features", "models", "plans"}:
                continue
            fields = sorted(PLAN_FEATURES | {"scope"} | ({"name", "instruction"} if "plans" in include else set()))
            row = self.store.db.execute("SELECT revision," + ",".join(_field(k) for k in fields) + " FROM records WHERE kind='life_plan' AND id=?", (identifier,)).fetchone()
            if row is None:
                missing.add("SELECTED_PLAN_UNAVAILABLE")
                continue
            if FIELD_LIMIT in row:
                missing.add("FIELD_LIMIT")
            item = {"object": ref("plan", identifier), "collected_at": stamp, "revision": row["revision"]}
            if "features" in include:
                item["features"] = {}
                for key in PLAN_FEATURES:
                    value = row[key]
                    if key in {"enabled", "run_once"}:
                        value = bool(value) if value in (0, 1) else None
                    elif key in {"window_before", "window_after"}:
                        value = _number(value)
                    elif key == "photo_mode":
                        value = _enum(value, {"auto", "always", "never"})
                    elif key == "contact_mode":
                        value = _enum(value, {"adaptive", "reminder"})
                    elif key == "day_mode":
                        value = _enum(value, set(DAY_MODES))
                    else:
                        value = self._text(value, secrets) if isinstance(value, str) and len(value) <= 100 else None
                    item["features"][key] = value
            if "plans" in include:
                item["plan"] = {k: self._text(row[k], secrets) for k in ("name", "instruction")}
            if "models" in include:
                item.update(host_model={}, _host_scope=row["scope"] or "")
            if len(item) > 3:
                result.append(item)
        for identifier in objects["characters"]:
            if "characters" not in include and "features" not in include:
                continue
            fields = ["appearance", "style", "failure_style", "active_version", "versions"]
            row = self.store.db.execute("SELECT revision," + ",".join(_field(k) for k in fields) + " FROM records WHERE kind='character' AND id=?", (identifier,)).fetchone()
            if row is None:
                missing.add("SELECTED_CHARACTER_UNAVAILABLE")
                continue
            if FIELD_LIMIT in row:
                missing.add("FIELD_LIMIT")
            item = {"object": ref("character", identifier), "collected_at": stamp, "revision": row["revision"]}
            if "characters" in include:
                item["drawing"] = {k: self._text(row[k], secrets) for k in ("appearance", "style", "failure_style")}
            try:
                versions = json.loads(row["versions"] or "[]")
                active = next((v for v in versions if isinstance(v, dict) and v.get("id") == row["active_version"]), {})
                refs = active.get("asset_ids", active.get("references", []))
            except (ValueError, TypeError):
                refs = []
            reference = {"reference_count": len(refs) if isinstance(refs, list) else None,
                         "reference_enabled": bool(row["active_version"])}
            if "features" in include:
                item["features"] = reference
            else:
                item["drawing"].update(reference)
            result.append(item)
        for chat in objects["chats"]:
            if not (include & {"features", "models", "summary_prompt"}):
                continue
            row = None
            if self._table("mem_chats"):
                fields = ["model_bindings", "summary_prompt", "assistant_enabled", *[r + "_provider_id" for r in MODELS], *sorted(MEMORY_FEATURES)]
                row = self.store.db.execute("SELECT revision,enabled," + ",".join(_field(k, column="settings") for k in fields) + " FROM mem_chats WHERE scope=? AND cid=?", (chat["scope"], chat["cid"])).fetchone()
                if row and FIELD_LIMIT in row:
                    missing.add("FIELD_LIMIT")
            item = {"object": ref("chat", (chat["scope"], chat["cid"])), "collected_at": stamp, "revision": row["revision"] if row else None, "settings_source": "SAVED" if row else "DEFAULTS_NO_SAVED_ROW"}
            if "features" in include:
                item["features"] = {k: row[k] if row and type(row[k]) in (int, float, bool) else DEFAULTS[k] for k in sorted(MEMORY_FEATURES)}
                item["enabled"] = bool(row["enabled"]) if row else False
            if "summary_prompt" in include:
                item["summary_prompt"] = self._text(row["summary_prompt"] if row else "", secrets)
            if "models" in include:
                try:
                    bindings = json.loads(row["model_bindings"] or "{}") if row else {}
                except (ValueError, TypeError):
                    bindings = {}
                settings = {"model_bindings": bindings, "vector_enabled": bool(row["vector_enabled"]) if row else DEFAULTS["vector_enabled"], "assistant_enabled": bool(row["assistant_enabled"]) if row else False, **{r + "_provider_id": row[r + "_provider_id"] if row else "" for r in MODELS}}
                item["models"] = self._memory_models(settings, secrets, missing)
                item["call_policy"] = {key: _number(row[key]) if row and row[key] is not None else DEFAULTS[key] for key in ("summary_timeout", "summary_max_retries")}
                item["_host_scope"] = chat["scope"]
            result.append(item)
        if "models" in include and (objects["plans"] or objects["characters"] or (objects["chats"] and "images" in categories)):
            sites = self.store.provider_configs()
            result.append({"object": "image_service", "availability": "CONFIGURED" if sites else "NO_USABLE_IMAGE_SITE", "collected_at": stamp, "revision": self.store.settings.get("settings_revision", 0), "policy": {k: self.store.settings.get(k) if type(self.store.settings.get(k)) in (bool, int, float) else None for k in sorted(IMAGE_POLICY)}, "sites": [self._model({**site, "type": "image"}, secrets) for site in sites]})
        return result

    def _model(self, config, secrets):
        return {"name": self._text(config.get("name", ""), secrets), "model": self._text(config.get("model", ""), secrets), "type": _enum(config.get("type", "chat"), {"chat", "image", "embedding", "rerank"}), "address": self._address(config.get("base_url", ""), secrets), "key_configured": bool(config.get("api_key")), **{k: _number(config[k]) if type(config[k]) in (int, float) else self._text(config[k], secrets) for k in ("size", "reference_field", "dimensions", "max_context_tokens", "timeout") if k in config}}

    def _memory_models(self, settings, secrets, missing):
        result, revision = {}, 0
        assistant = {"model": MODELS["assistant"], "mode": "summary", "base_url": "", "api_key": ""}
        if self._table("memory_model_settings"):
            saved = self.store.db.execute("SELECT revision," + _field("assistant", limit=8000) + " FROM memory_model_settings WHERE id=1").fetchone()
            if saved:
                revision = saved["revision"]
                if FIELD_LIMIT in saved:
                    missing.add("FIELD_LIMIT")
                    return {role: {"availability": "FIELD_LIMIT", "models_revision": revision} for role in MODELS}
                try:
                    value = json.loads(saved["assistant"] or "{}")
                except (ValueError, TypeError):
                    value = {}
                if isinstance(value, dict):
                    assistant.update(value)
        bindings = settings.get("model_bindings", {})
        for role in ("summary", "assistant", "embedding", "rerank"):
            binding = bindings.get(role, {}) if isinstance(bindings, dict) else {}
            if not isinstance(binding, dict):
                binding = {}
            # Summary and vector execution require an explicit preset, even
            # when legacy selection metadata still names a host connection.
            if role in {"summary", "embedding", "rerank"} and (binding.get("mode") != "preset" or not binding.get("preset_id")):
                mode = _enum(binding.get("mode"), {"disabled", "host", "legacy", "summary", "embedding", "current", "preset"}, "NOT_RECORDED")
                result[role] = {"mode": "unavailable", "binding_mode": mode, "availability": "DISABLED" if mode == "disabled" else "NOT_CONFIGURED", "models_revision": revision}
                if role != "summary":
                    result[role]["enabled"] = False
                continue
            if not binding:
                binding = {"mode": "disabled" if not settings.get("assistant_enabled") else "legacy" if assistant.get("mode") == "custom" else "summary"}
            mode = _enum(binding.get("mode"), {"preset", "disabled", "host", "legacy", "summary", "embedding", "current"})
            item = {"mode": mode, "binding_mode": mode, "models_revision": revision}
            if mode == "preset":
                item["availability"] = "CONNECTION_UNAVAILABLE"
            if mode == "preset" and isinstance(binding.get("preset_id"), str) and self._table("memory_model_settings"):
                fields = ["name", "model", "type", "base_url", "api_key", "max_context_tokens", "dimensions"]
                row = self.store.db.execute("SELECT s.revision," + ",".join(_field(k, column="p.value") for k in fields) + " FROM memory_model_settings s,json_each(s.data,'$.presets') p WHERE s.id=1 AND json_extract(p.value,'$.id')=? LIMIT 1", (binding["preset_id"],)).fetchone()
                if row:
                    if FIELD_LIMIT in row:
                        missing.add("FIELD_LIMIT")
                        item["availability"] = "FIELD_LIMIT"
                    elif row["type"] != ("chat" if role in {"summary", "assistant"} else role) or set(binding) - {"mode", "preset_id"}:
                        item["availability"] = "CONNECTION_UNAVAILABLE"
                    else:
                        item.update(self._model(dict(row), secrets), revision=row["revision"], availability="CONFIGURED")
                else:
                    item["availability"] = "CONNECTION_UNAVAILABLE"
            elif mode == "legacy":
                if assistant.get("mode") == "summary":
                    item.update(result.get("summary", {}), binding_mode=mode, inherited_from="summary")
                else:
                    item.update(self._model({**assistant, "type": "chat"}, secrets), availability="CONFIGURED")
            elif mode == "summary":
                item.update(result.get("summary", {}), binding_mode=mode, inherited_from="summary")
            elif mode == "disabled":
                item["availability"] = "DISABLED"
            elif mode != "preset":
                item.update(mode="unavailable", availability="NOT_CONFIGURED")
            if role in {"embedding", "rerank"}:
                item["enabled"] = bool(settings.get("vector_enabled")) and item.get("availability") == "CONFIGURED"
            result[role] = item
        return result

    async def _host_model(self, scope, secrets, identifier=""):
        try:
            if not self.context:
                return {"availability": "HOST_MODEL_INTERFACE_UNAVAILABLE"}
            provider = self.context.get_provider_by_id(identifier) if identifier else await self.context.get_using_provider_async(scope)
            if provider is None:
                return {"availability": "NO_HOST_MODEL"}
            config = getattr(provider, "provider_config", {})
            if not isinstance(config, dict):
                return {"availability": "HOST_MODEL_INTERFACE_UNAVAILABLE"}
            keys = config.get("key", config.get("api_key"))
            configured = any(isinstance(key, str) and bool(key.strip()) for key in keys) if isinstance(keys, list) else isinstance(keys, str) and bool(keys.strip())
            value = {"model": provider.get_model(), "name": config.get("name", ""), "base_url": config.get("api_base", config.get("base_url", "")), "api_key": configured, "timeout": config.get("timeout")}
            return {"availability": "AVAILABLE", **self._model(value, secrets), "interface": self._text(config.get("type", ""), secrets), "request_max_retries": _number(config.get("request_max_retries")), "collected_at": now()}
        except Exception:
            return {"availability": "HOST_MODEL_INTERFACE_UNAVAILABLE"}

    async def _persona(self, chat, secrets, ref):
        item = {"object": ref("chat", (chat["scope"], chat["cid"])), "collected_at": now(), "component": "persona"}
        if not self.context or not getattr(self.context, "persona_manager", None):
            return {**item, "availability": "PERSONA_INTERFACE_UNAVAILABLE"}
        try:
            from sqlmodel import select
            from astrbot.core.db.po import ConversationV2

            database = self.context.get_db()
            async with database.get_db() as session:
                query = select(ConversationV2.user_id, ConversationV2.persona_id, ConversationV2.platform_id).where(ConversationV2.conversation_id == chat["cid"], ConversationV2.user_id == chat["scope"]).limit(1)
                row = (await session.execute(query)).first()
            if row is None:
                return {**item, "availability": "CHAT_UNAVAILABLE"}
            platform = self.context.get_platform_inst(row.platform_id)
            selected, persona, forced, special = await self.context.persona_manager.resolve_selected_persona(umo=chat["scope"], conversation_persona_id=row.persona_id, platform_name=platform.meta().name if platform else "")
            source = "FORCED" if forced else "EXPLICIT_NONE" if row.persona_id == "[%None]" else "CHAT" if row.persona_id else "DEFAULT"
            return {**item, "availability": "WEBCHAT_BUILTIN" if special else "AVAILABLE" if persona else "NO_PERSONA", "selection_source": source, "text": self._text((persona or {}).get("prompt", ""), secrets)}
        except Exception:
            return {**item, "availability": "PERSONA_INTERFACE_UNAVAILABLE"}
