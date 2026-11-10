"""Authenticated management routes for chat memory."""

from __future__ import annotations

import json
import re

from astrbot.api.web import error_response, json_response, request

from ..store import AlbumError
from . import core
from .maintenance_store import fingerprint
from .models import MemoryModel, MemoryModelError
from .service import error_kind

REQUEST_LIMIT = 4 * 1024 * 1024
# Per-chat connection choices: binding role -> settings field and preset type.
PRESET_FIELDS = {"summary": "summary_preset", "embedding": "embedding_preset", "rerank": "rerank_preset"}
PRESET_TYPES = {"summary": "chat", "embedding": "embedding", "rerank": "rerank"}
PRIVATE = {"Cache-Control": "no-store"}
SETTINGS = (
    "summary_trigger_turns",
    "keep_turns",
    "memory_tokens",
    "retention_days",
    "summary_max_retries",
    "summary_timeout",
    "summary_prompt",
    "user_name",
    "auto_process",
    "inject_enabled",
    "vector_enabled",
    "query_rewrite",
    "recall_candidates",
    "recall_max",
    "recall_rerank_min",
    "recall_vector_min",
    "image_describe",
    "image_keep_turns",
    "maintenance_paused",
)


class MemoryAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.store = plugin.memory_store
        self.service = plugin.memory_service
        self.runtime = plugin.memory_runtime
        self.context = plugin.context

    def register(self, name):
        for route, method, handler in (
            ("conversations", "GET", self.conversations),
            ("follow", "GET", self.follow),
            ("binding_options", "GET", self.binding_options),
            ("bind", "POST", self.bind),
            ("state", "GET", self.state),
            ("progress", "GET", self.progress),
            ("memory", "POST", self.save_memory),
            ("action", "POST", self.action),
            ("batches", "GET", self.batches),
            ("snapshots", "GET", self.snapshots),
            ("settings", "GET", self.settings),
            ("settings", "POST", self.save_settings),
            ("model_preset", "POST", self.model_preset),
            ("model_catalog", "GET", self.model_catalog),
            ("model_key", "POST", self.model_key),
            ("diagnostics", "GET", self.diagnostics),
            ("vectors", "GET", self.vector_status),
            ("vectors", "POST", self.vector_action),
            ("images", "GET", self.image_status),
            ("images", "POST", self.image_action),
            ("export", "GET", self.export),
            ("import", "POST", self.import_memory),
            ("maintenance", "GET", self.maintenance_state),
            ("maintenance/detail", "GET", self.maintenance_detail),
            ("maintenance", "POST", self.maintenance_action),
        ):

            async def safe(handler=handler):
                if not request.username:
                    return error_response("请登录后管理记忆", status_code=401, headers=PRIVATE)
                try:
                    with self.store.restore_guard.operation():
                        return await handler()
                except (AlbumError, ValueError, TypeError, KeyError) as exc:
                    message = str(exc) if isinstance(exc, AlbumError) else "记忆请求格式不正确"
                    conflict = any(w in message for w in ("处理中", "版本", "已变化", "修改过"))
                    return error_response(message, status_code=409 if conflict else 400, headers=PRIVATE)
                except Exception:
                    return error_response("记忆操作失败，请刷新后查看当前状态", status_code=500, headers=PRIVATE)

            self.context.register_web_api(f"/{name}/memory/{route}", safe, [method], "Chat memory")

    # ------------------------------------------------------------ helpers --

    async def body(self):
        raw = await request.body()
        if len(raw) > REQUEST_LIMIT:
            raise AlbumError("请求内容过大")
        values = json.loads(raw or b"{}")
        if not isinstance(values, dict):
            raise AlbumError("请求格式不正确")
        return values

    async def target(self, values):
        scope, cid = values.get("scope", ""), values.get("cid", "")
        if not isinstance(scope, str) or not isinstance(cid, str) or not scope or not cid:
            raise AlbumError("请选择一份聊天")
        await self.runtime.conversation(scope, cid)
        return scope, cid

    def idle(self, scope, cid):
        if (scope, cid) in self.service.tasks:
            raise AlbumError("该聊天正在整理中，请等待完成或先停止")

    async def write_ready(self, scope, cid):
        await self.service.maintenance.cancel_wait(scope, cid)
        self.idle(scope, cid)

    def view(self, scope, cid):
        state = self.store.state(scope, cid)
        memory = self.store.memory(scope, cid)
        names = core.person_names(memory)
        events = []
        for event in memory["events"]:
            later = [e["id"] for e in memory["events"] if event["id"] in e.get("causes", [])]
            plans = [p["id"] for p in memory["plans"] if event["id"] in p.get("events", [])]
            events.append({**event, "date": core.event_date(event), "followups": later, "plans": plans})
        return {
            "state": state,
            "memory": {**memory, "events": events, "names": names},
            "progress": self.service.progress(scope, cid),
            "last": self.runtime.last.get((scope, cid)),
            "status_names": core.STATUS_NAMES,
            "roles": list(core.ROLES),
            "profile_kinds": core.PROFILE_KINDS,
        }

    # -------------------------------------------------------- conversations --

    async def follow(self):
        scope = self.store.view_binding(request.username)
        cid = await self.context.conversation_manager.get_curr_conversation_id(scope) if scope else None
        return json_response({"scope": scope, "cid": cid}, headers=PRIVATE)

    async def binding_options(self):
        """Bot-to-user sessions (unified_msg_origin), like AstrBot's 投递到 list."""
        scopes = set()
        conversations, _ = await self.context.conversation_manager.get_filtered_conversations(
            page=1, page_size=200, search_query="", include_history=False
        )
        scopes.update(c.user_id for c in conversations if c.user_id)
        for char in self.plugin.store.all("character"):
            scopes.update(char.get("sessions", []))
        scopes.update(getattr(self.plugin, "seen_sessions", {}))
        current = self.store.view_binding(request.username)
        if current:
            scopes.add(current)
        aliases = {}
        try:
            database = self.context.get_db()
            for alias in await database.get_umo_aliases(sorted(scopes)):
                aliases[alias.umo] = alias.user_alias or alias.auto_name or ""
        except Exception:
            pass
        seen = getattr(self.plugin, "seen_sessions", {})
        characters = {s: c.get("name", "") for c in self.plugin.store.all("character") for s in c.get("sessions", [])}
        options = []
        for scope in sorted(scopes):
            parts = scope.split(":", 2)
            if len(parts) != 3:
                continue
            platform, kind, number = parts
            options.append(
                {
                    "scope": scope,
                    "platform": platform,
                    "kind": kind,
                    "number": number,
                    "name": aliases.get(scope) or seen.get(scope, {}).get("name", "") or number,
                    "character": characters.get(scope, ""),
                }
            )
        # Real chat platforms first; WebUI test sessions last.
        options.sort(key=lambda o: (o["platform"] == "webchat", o["kind"] != "FriendMessage", o["name"]))
        return json_response(
            {"options": options, "bots": self.plugin.plan_bots(), "current": current},
            headers=PRIVATE,
        )

    async def bind(self):
        data = await self.body()
        scope = data.get("scope", "")
        recipient = data.get("recipient")
        if recipient is not None:
            bots = [b["id"] for b in self.plugin.plan_bots()]
            bot = recipient.get("bot") if isinstance(recipient, dict) else None
            if not bot and len(bots) == 1:
                bot = bots[0]
            if (
                not isinstance(recipient, dict)
                or not bot
                or recipient.get("kind", "FriendMessage") not in {"FriendMessage", "GroupMessage"}
                or not re.fullmatch(r"[1-9][0-9]{4,11}", str(recipient.get("number", "")))
            ):
                raise AlbumError("请填写正确的 QQ 号或群号" if bots else "没有可用的QQ平台，请从已有会话里选择")
            scope = f"{bot}:{recipient.get('kind', 'FriendMessage')}:{recipient['number']}"
        if not isinstance(scope, str) or len(scope) > 512:
            raise AlbumError("请选择要绑定的会话")
        if scope and len(scope.split(":", 2)) != 3:
            raise AlbumError("会话对象格式不正确")
        self.store.bind_view(request.username, scope)
        return await self.follow()

    async def conversations(self):
        page = max(1, int(request.query.get("page", "1")))
        query = request.query.get("query", "")[:200]
        conversations, total = await self.context.conversation_manager.get_filtered_conversations(
            page=page, page_size=20, search_query=query, include_history=False
        )
        items = []
        for conv in conversations:
            current = await self.context.conversation_manager.get_curr_conversation_id(conv.user_id)
            state = self.store.state(conv.user_id, conv.cid)
            items.append(
                {
                    "scope": conv.user_id,
                    "cid": conv.cid,
                    "title": conv.title or "未命名聊天",
                    "current": current == conv.cid,
                    "enabled": state["enabled"],
                    "events": len(self.store.memory(conv.user_id, conv.cid)["events"]),
                }
            )
        return json_response({"items": items, "total": total}, headers=PRIVATE)

    # --------------------------------------------------------------- read --

    async def state(self):
        scope, cid = await self.target(request.query)
        return json_response(self.view(scope, cid), headers=PRIVATE)

    async def progress(self):
        scope, cid = await self.target(request.query)
        state = self.store.state(scope, cid)
        return json_response(
            {"revision": state["revision"], "progress": self.service.progress(scope, cid), "counts": state["counts"]},
            headers=PRIVATE,
        )

    async def batches(self):
        scope, cid = await self.target(request.query)
        return json_response({"items": self.store.batches(scope, cid)}, headers=PRIVATE)

    async def snapshots(self):
        scope, cid = await self.target(request.query)
        return json_response({"items": self.store.snapshots(scope, cid)}, headers=PRIVATE)

    async def diagnostics(self):
        return json_response(
            self.store.logs(
                request.query.get("scope", ""),
                request.query.get("cid", ""),
                failed=request.query.get("status", "") == "failed",
                query=request.query.get("query", "")[:200],
                offset=max(0, int(request.query.get("offset", "0"))),
            ),
            headers=PRIVATE,
        )

    # -------------------------------------------------------------- edits --

    async def save_memory(self):
        """User edits of one item: edit/delete an event, person, plan, overview, keywords."""
        data = await self.body()
        scope, cid = await self.target(data)
        await self.write_ready(scope, cid)
        memory = self.store.memory(scope, cid)
        kind, item = data.get("kind"), data.get("item") or {}
        if kind == "overview":
            text = item.get("text", "")
            if not isinstance(text, str) or len(text) > 6000:
                raise AlbumError("概况最多6000字")
            memory["overview"] = text.strip()
        elif kind == "keywords":
            words = item.get("words", [])
            if not isinstance(words, list) or len(words) > 12:
                raise AlbumError("关键词最多12个")
            memory["keywords"] = [
                {"text": str(w).strip()[:30], "weight": "一般"} for w in words if str(w).strip()
            ]
        elif kind == "profile" and not item.get("id"):
            text = str(item.get("text", "")).strip()[:600]
            owner = next((p for p in memory["people"] if p["id"] == item.get("person")), None)
            if not owner or not text:
                raise AlbumError("请选择档案属于谁并填写内容")
            if item.get("kind") not in core.PROFILE_KINDS:
                raise AlbumError("档案类型不正确")
            memory["seq"]["pf"] += 1
            memory["profiles"].append(
                {
                    "id": f"pf{memory['seq']['pf']}",
                    "person": owner["id"],
                    "kind": item["kind"],
                    "text": text,
                    "until": str(item.get("until", "")).strip()[:100],
                    "batch": None,
                }
            )
        elif kind in ("event", "person", "plan", "profile"):
            collection = {"event": "events", "person": "people", "plan": "plans", "profile": "profiles"}[kind]
            identifier = item.get("id")
            index = next((i for i, x in enumerate(memory[collection]) if x["id"] == identifier), None)
            if index is None:
                raise AlbumError("这条记忆已不存在，请刷新")
            if data.get("delete"):
                memory[collection].pop(index)
                if kind == "event":
                    for event in memory["events"]:
                        event["causes"] = [c for c in event.get("causes", []) if c != identifier]
                    for plan in memory["plans"]:
                        plan["events"] = [x for x in plan.get("events", []) if x != identifier]
                if kind == "person":
                    for person in memory["people"]:
                        person["relationships"] = [r for r in person["relationships"] if r["to"] != identifier]
                    memory["profiles"] = [p for p in memory["profiles"] if p.get("person") != identifier]
            else:
                current = memory[collection][index]
                if kind == "event":
                    for field in ("title", "summary", "time"):
                        if field in item:
                            value = str(item[field]).strip()
                            if field != "time" and not value:
                                raise AlbumError("标题和正文不能为空")
                            current[field] = value[:6000]
                    if "participants" in item:
                        current["participants"] = [str(x).strip() for x in item["participants"] if str(x).strip()][:20]
                    if "causes" in item:
                        known = {e["id"] for e in memory["events"]}
                        current["causes"] = [c for c in item["causes"] if c in known and c != identifier][: core.MAX_CAUSES]
                    if "role" in item:
                        current["role"] = item["role"] if item["role"] in core.ROLES else ""
                elif kind == "person":
                    for field in ("name", "description"):
                        if field in item:
                            current[field] = str(item[field]).strip()[:600]
                    if not current["name"]:
                        raise AlbumError("人物名称不能为空")
                    if "aliases" in item:
                        current["aliases"] = [str(x).strip() for x in item["aliases"] if str(x).strip()][:20]
                    if "relationships" in item:
                        # [{to, text}]: changed text keeps the old one in history; blank removes.
                        known = {p["id"] for p in memory["people"]} - {identifier}
                        old = {r["to"]: r for r in current["relationships"]}
                        updated = []
                        for rel in item["relationships"][:30]:
                            to, text = rel.get("to"), str(rel.get("text", "")).strip()[:200]
                            if to not in known or not text or any(r["to"] == to for r in updated):
                                continue
                            previous = old.get(to)
                            if previous is None:
                                updated.append({"to": to, "text": text, "history": [], "batch": None})
                            elif previous["text"] != text:
                                history = [*previous.get("history", []), {"text": previous["text"], "batch": previous.get("batch")}]
                                updated.append({**previous, "text": text, "history": history})
                            else:
                                updated.append(previous)
                        current["relationships"] = updated
                elif kind == "profile":
                    if "text" in item:
                        current["text"] = str(item["text"]).strip()[:600]
                        if not current["text"]:
                            raise AlbumError("档案内容不能为空")
                    if "until" in item:
                        current["until"] = str(item["until"]).strip()[:100]
                    if "kind" in item:
                        if item["kind"] not in core.PROFILE_KINDS:
                            raise AlbumError("档案类型不正确")
                        current["kind"] = item["kind"]
                    if "person" in item:
                        if not any(p["id"] == item["person"] for p in memory["people"]):
                            raise AlbumError("所选人物不存在")
                        current["person"] = item["person"]
                else:
                    for field in ("summary", "time", "reason"):
                        if field in item:
                            current[field] = str(item[field]).strip()[:2000]
                    if "status" in item:
                        if item["status"] not in core.STATUS_NAMES:
                            raise AlbumError("约定状态不正确")
                        current["status"] = item["status"]
                    if "events" in item:
                        known = {e["id"] for e in memory["events"]}
                        current["events"] = [x for x in dict.fromkeys(item["events"]) if x in known][:20]
        else:
            raise AlbumError("不支持的修改")
        self.store.save_memory(scope, cid, memory, data.get("revision"))
        self.service.refresh_vectors(scope, cid)
        return json_response(self.view(scope, cid), headers=PRIVATE)

    async def action(self):
        data = await self.body()
        name = data.get("action")
        scope, cid = await self.target(data)
        revision = data.get("revision")
        if name == "enable":
            await self.service.maintenance.cancel_wait(scope, cid)
            self.store.configure(scope, cid, {"enabled": bool(data.get("enabled"))}, revision)
        elif name == "summarize":
            state = self.store.state(scope, cid)
            if not state["enabled"]:
                raise AlbumError("请先启用此聊天的记忆")
            self.service.model(state["settings"])
            self.service.failed.pop((scope, cid), None)
            if not self.service.request(scope, cid, manual=True):
                raise AlbumError(self.service.plan(scope, cid, manual=True).get("reason") or "当前没有可整理的内容")
        elif name == "stop":
            self.service.stop(scope, cid)
        elif name == "undo":
            await self.write_ready(scope, cid)
            self.store.undo_last(scope, cid, revision, force=data.get("force") is True)
        elif name == "resummarize":
            await self.write_ready(scope, cid)
            if data.get("confirmed") is not True:
                raise AlbumError("请确认要清空记忆并从备份重新整理")
            self.store.reset_for_resummary(scope, cid, revision)
            self.service.failed.pop((scope, cid), None)
            self.service.request(scope, cid, manual=True)
        elif name == "restore":
            await self.write_ready(scope, cid)
            self.store.restore_snapshot(scope, cid, data.get("id"), revision)
        elif name == "copy_to":
            await self.write_ready(scope, cid)
            target = data.get("target") or {}
            target_scope, target_cid = await self.target(target)
            await self.write_ready(target_scope, target_cid)
            self.store.copy_to(scope, cid, target_scope, target_cid, target.get("revision"))
        elif name == "clear":
            await self.write_ready(scope, cid)
            if data.get("confirmed") is not True:
                raise AlbumError("请确认清空记忆")
            with self.store.transaction():
                self.store._snapshot(scope, cid, "清空前")
            self.store.save_memory(scope, cid, core.empty(), revision, replace=True)
        else:
            raise AlbumError("不支持的操作")
        if name in ("undo", "restore", "clear", "resummarize"):
            self.service.refresh_vectors(scope, cid)
        elif name == "copy_to":
            self.service.refresh_vectors(target_scope, target_cid)
        return json_response(self.view(scope, cid), headers=PRIVATE)

    # ------------------------------------------------------------ settings --

    def _settings_view(self, scope, cid):
        state = self.store.state(scope, cid)
        models = self.service.models.workspace(state["settings"])

        def chosen(role):
            binding = models["bindings"].get(role) or {}
            return binding.get("preset_id", "") if binding.get("mode") == "preset" else ""

        return {
            "revision": state["revision"],
            "settings": {k: state["settings"][k] for k in SETTINGS},
            "presets": [p for p in models["presets"] if p.get("type") in PRESET_TYPES.values()],
            "models_revision": models["models_revision"],
            **{field: chosen(role) for role, field in PRESET_FIELDS.items()},
            "default_prompt": core.WRITING_RULES,
            "output_format": core.OUTPUT_FORMAT,
        }

    async def settings(self):
        scope, cid = await self.target(request.query)
        return json_response(self._settings_view(scope, cid), headers=PRIVATE)

    async def model_catalog(self):
        """Shared text connections, without selecting or creating a chat."""
        catalog = self.service.models.workspace({})
        return json_response({"models_revision": catalog["models_revision"],
            "presets": [p for p in catalog["presets"] if p["type"] == "chat"]}, headers=PRIVATE)

    async def save_settings(self):
        data = await self.body()
        scope, cid = await self.target(data)
        patch = data.get("settings", {})
        if not isinstance(patch, dict) or set(patch) - set(SETTINGS):
            raise AlbumError("设置字段不正确")
        if any(field in data for field in PRESET_FIELDS.values()):
            presets = self.service.models.private().get("presets", [])
            bindings = dict(self.store.state(scope, cid)["settings"].get("model_bindings") or {})
            for role, field in PRESET_FIELDS.items():
                preset = data.get(field)
                if preset is None:
                    continue
                if not isinstance(preset, str):
                    raise AlbumError("所选连接不正确")
                known = {p["id"] for p in presets if p.get("type") == PRESET_TYPES[role]}
                if preset and preset not in known:
                    raise AlbumError("所选连接不存在")
                if preset:
                    bindings[role] = {"mode": "preset", "preset_id": preset}
                else:
                    bindings.pop(role, None)
            patch = {**patch, "model_bindings": bindings}
        self.store.configure(scope, cid, patch, data.get("revision"))
        self.service.failed.pop((scope, cid), None)
        # Turning vectors on or switching the embedding connection fills what is missing.
        self.service.refresh_vectors(scope, cid)
        return json_response(self._settings_view(scope, cid), headers=PRIVATE)

    async def model_preset(self):
        """Create/edit/test/delete named chat connections (window is set here)."""
        data = await self.body()
        operation = data.get("operation")
        models = self.service.models
        if operation in ("list", "test", "save"):
            fields = data.get("config", {})
            if not isinstance(fields, dict):
                raise AlbumError("连接字段不正确")
            kind = fields.get("type") or "chat"
            if kind not in PRESET_TYPES.values():
                raise AlbumError("连接类型不正确")
            data = {**data, "config": {**fields, "type": kind}}
        if operation in ("list", "test"):
            fields = data["config"]
            draft = {**data, "config": {**fields, "name": fields.get("name") or "临时连接"}}
            if operation == "list":
                draft["config"]["model"] = fields.get("model") or "list-models"
            config = models.preset("draft", draft, data.get("revision"))
            kind = fields["type"]
            model = MemoryModel("summary" if kind == "chat" else kind, config)
            try:
                if operation == "list":
                    return json_response({"items": await model.list_models()}, headers=PRIVATE)
                if kind == "embedding":
                    # The page compares this with the entered dimension and fixes it.
                    return json_response({"ok": True, "dimensions": await model.probe_dimensions()}, headers=PRIVATE)
                elif kind == "rerank":
                    await model.rerank("连接测试", ["测试文档一", "测试文档二"], 2)
                else:
                    await model.summarize(prompt="请回复OK", system_prompt="连接测试", max_tokens=16, timeout=60)
                return json_response({"ok": True}, headers=PRIVATE)
            except MemoryModelError as error:
                if getattr(error, "status", None) is None and not getattr(error, "retryable", False):
                    raise  # local validation such as wrong dimensions: show as is
                raise AlbumError(error_kind(error)[0]) from None
            except Exception as error:
                raise AlbumError(error_kind(error)[0]) from None
        result = models.preset(operation, data, data.get("revision"))
        if operation == "key":
            return json_response(result, headers=PRIVATE)
        return json_response(
            {"presets": [p for p in models.workspace({})["presets"] if p.get("type") in PRESET_TYPES.values()],
             "models_revision": models.public()["revision"]},
            headers=PRIVATE,
        )

    async def model_key(self):
        data = await self.body()
        return json_response(
            self.service.models.preset("key", data, data.get("revision")), headers=PRIVATE
        )

    # ------------------------------------------------------------- vectors --

    def _vector_view(self, scope, cid):
        settings = self.store.state(scope, cid)["settings"]
        model = self.service.optional_model("embedding", settings)
        status = self.service.vectors.status(scope, cid, model.identity if model else "")
        return {
            **status,
            "enabled": bool(settings.get("vector_enabled")),
            "configured": model is not None,
            "model": model.config.get("model", "") if model else "",
            "dimensions": model.config.get("dimensions", 0) if model else 0,
        }

    async def vector_status(self):
        scope, cid = await self.target(request.query)
        return json_response(self._vector_view(scope, cid), headers=PRIVATE)

    async def vector_action(self):
        """补齐缺漏 / 完整重建 / 清空 for this chat. Paid calls only on explicit request."""
        data = await self.body()
        scope, cid = await self.target(data)
        name = data.get("action")
        if name == "clear":
            if (scope, cid) in self.service.vectors.running:
                raise AlbumError("向量正在生成，请稍后再清空")
            self.service.vectors.clear(scope, cid)
            self.store.log(scope, cid, "indexed", "已清空此聊天的向量")
        elif name in ("fill", "rebuild"):
            settings = self.store.state(scope, cid)["settings"]
            if not settings.get("vector_enabled"):
                raise AlbumError("请先在向量召回里启用向量检索")
            if self.service.optional_model("embedding", settings) is None:
                raise AlbumError("请先在模型连接里选择向量模型")
            if not self.service.refresh_vectors(scope, cid, rebuild=name == "rebuild", manual=True):
                raise AlbumError("向量正在生成中")
        else:
            raise AlbumError("不支持的向量操作")
        return json_response(self._vector_view(scope, cid), headers=PRIVATE)

    # -------------------------------------------------------------- images --

    def _image_options(self, scope, cid):
        state = self.store.state(scope, cid)
        settings = state["settings"]
        return {
            "enabled": state["enabled"] and bool(settings.get("image_describe")),
            "configured": self.service.optional_model("summary", settings) is not None,
        }

    async def image_status(self):
        scope, cid = await self.target(request.query)
        # Polling never parses/hashes the large chat file. An explicit preview
        # refreshes the available set, without registering or calling a model.
        status = (
            self.service.images.status(scope, cid)
            if request.query.get("poll") == "1"
            else await self.service.images.preview(scope, cid, window=self.service.window(self.store.state(scope, cid)["settings"])[0])
        )
        return json_response({**status, **self._image_options(scope, cid)}, headers=PRIVATE)

    async def image_action(self):
        data = await self.body()
        scope, cid = await self.target(data)
        refs = data.get("refs")
        if (
            data.get("action") != "fill"
            or data.get("confirmed") is not True
            or not isinstance(refs, list)
            or not 1 <= len(refs) <= 100
            or any(not isinstance(ref, str) or re.fullmatch(r"[a-f0-9]{32}", ref) is None for ref in refs)
        ):
            raise AlbumError("请确认本次要补描述的图片")
        state = self.store.state(scope, cid)
        if not state["enabled"]:
            raise AlbumError("请先启用此聊天的记忆")
        if not state["settings"].get("image_describe"):
            raise AlbumError("请先保存开启“识别图片内容”的设置")
        if self.service.optional_model("summary", state["settings"]) is None:
            raise AlbumError("请先在模型连接里选择总结 API")
        conv = await self.runtime.conversation(scope, cid)
        self.runtime.capture(scope, cid, json.loads(conv.history or "[]"))
        del conv
        if not self.service.describe_images(scope, cid, refs=list(dict.fromkeys(refs)), manual=True):
            raise AlbumError("图片正在处理中，或当前没有可处理的图片")
        return json_response(
            {**self.service.images.status(scope, cid), **self._image_options(scope, cid)},
            headers=PRIVATE,
        )

    # ---------------------------------------------------------- import/export --

    async def export(self):
        scope, cid = await self.target(request.query)
        memory = self.store.memory(scope, cid)
        return json_response(
            {"format": "lumen-memory", "version": 3, "memory": memory},
            headers={**PRIVATE, "Content-Disposition": 'attachment; filename="lumen-memory.json"'},
        )

    async def import_memory(self):
        data = await self.body()
        scope, cid = await self.target(data)
        await self.write_ready(scope, cid)
        payload = data.get("payload")
        if not isinstance(payload, dict) or payload.get("format") != "lumen-memory" or not isinstance(payload.get("memory"), dict):
            raise AlbumError("请选择拾忆回响导出的记忆文件")
        memory = core.normalized(payload["memory"])
        with self.store.transaction():
            self.store._snapshot(scope, cid, "导入前")
        self.store.save_memory(scope, cid, memory, data.get("revision"), replace=True)
        self.service.refresh_vectors(scope, cid)
        return json_response(self.view(scope, cid), headers=PRIVATE)

    @staticmethod
    def _maintenance_selected(data, *, query=False):
        selected = data.get("selected_batches", "[]" if query else [])
        if query:
            if not isinstance(selected, str) or len(selected) > 4096:
                raise AlbumError("PROTOCOL")
            try:
                selected = json.loads(selected)
            except json.JSONDecodeError:
                raise AlbumError("PROTOCOL") from None
        if (not isinstance(selected, list) or len(selected) > 120
                or any(type(i) is not int or not 0 < i <= 2**63 - 1 for i in selected)):
            raise AlbumError("PROTOCOL")
        return sorted(set(selected))

    def _maintenance_view(self, scope, cid, selected_batches=()):
        engine, data = self.service.maintenance, self.store.maintenance
        policy = engine.policy()
        configured, error_code, connection_signature = True, "", ""
        if policy.get("enabled"):
            try:
                _, connection_signature = engine.configured(scope, cid)
            except AlbumError:
                configured, error_code = False, "CONNECTION_MISSING"
        with self.store.lock:
            snapshot = data.items(scope, cid, readonly=True)
            batches = self.store.batches(scope, cid)
            unavailable = []
            if selected_batches:
                marks = ",".join("?" for _ in selected_batches)
                selected = {row["id"]: dict(row) for row in self.store.db.execute(
                    f"SELECT id,first,last,state FROM mem_batches WHERE scope=? AND cid=? AND id IN ({marks})",
                    (scope, cid, *selected_batches)).fetchall()}
                known = {batch["id"] for batch in batches}
                for batch_id in selected_batches:
                    batch = selected.get(batch_id)
                    if not batch or batch["state"] != "applied":
                        unavailable.append({"id": batch_id, "state": batch["state"] if batch else "missing"})
                    elif batch_id not in known:
                        batches.append(batch)
            latest = self.store.db.execute("SELECT id,first,last,state FROM mem_batches "
                "WHERE scope=? AND cid=? AND state='applied' ORDER BY id DESC LIMIT 1", (scope, cid)).fetchone()
            current = {(item["collection"], item["id"]) for item in snapshot["items"]}
            def batch_keys(batch):
                return [item for item in data.changed(scope, cid, [batch]) if (item["collection"], item["id"]) in current]
            for batch in batches:
                batch["keys"] = batch_keys(batch["id"])
            latest = dict(latest) | {"keys": batch_keys(latest["id"])} if latest else None
        items = []
        for item in snapshot["items"]:
            value = item["value"]
            label = (value.get("title") or value.get("name") or value.get("summary") or value.get("text") or item["id"]) if isinstance(value, dict) else item["collection"]
            items.append({k: item[k] for k in ("collection", "id", "version", "hash", "protected", "legacy_unverified", "source_complete")} | {"label": str(label)[:100]})
        runs = data.list_runs(scope, cid)
        return {"enabled": bool(policy.get("enabled")), "auto_after_summary": bool(policy.get("auto_after_summary")),
                "revision": snapshot["revision"], "epoch": snapshot["epoch"], "paused": snapshot["paused"],
                "policy_signature": fingerprint([policy, connection_signature]),
                "configured": configured, "error_code": error_code,
                "running": (scope, cid) in engine.tasks, "task": engine.active.get((scope, cid)),
                "items": items, "batches": batches, "latest_batch": latest, "unavailable_batches": unavailable, "runs": runs}

    async def maintenance_state(self):
        scope, cid = self.maintenance_target(request.query)
        selected = self._maintenance_selected(request.query, query=True)
        return json_response(self._maintenance_view(scope, cid, selected), headers=PRIVATE)

    async def maintenance_detail(self):
        data = request.query
        scope, cid = self.maintenance_target(data)
        version = data.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", version):
            raise AlbumError("PROTOCOL")
        result = self.store.maintenance.detail(scope, cid, data.get("collection"), data.get("id"),
            epoch=data.get("epoch"), version=int(version), content_hash=data.get("hash"))
        return json_response(result, headers=PRIVATE)

    def maintenance_target(self, data):
        scope, cid = data.get("scope"), data.get("cid")
        if not isinstance(scope, str) or not isinstance(cid, str) or not scope or not cid:
            raise AlbumError("请选择一份聊天")
        self.service.maintenance.host_check(scope, cid)
        return scope, cid

    async def maintenance_action(self):
        data = await self.body()
        scope, cid = self.maintenance_target(data)
        selected = self._maintenance_selected(data)
        engine, store = self.service.maintenance, self.store.maintenance
        op = data.get("op")
        if op == "preview":
            result = engine.preview(scope, cid, data.get("keys", []), data.get("batches", []),
                apply=data.get("apply", False), allow_protected=data.get("allow_protected", False))
            return json_response(result, headers=PRIVATE)
        if op == "start":
            self.idle(scope, cid)
            engine.start(scope, cid, data.get("token"), data.get("request_id"))
        elif op == "stop":
            engine.cancel(scope, cid, data.get("task_id"))
        elif op == "undo":
            await self.write_ready(scope, cid)
            store.undo(scope, cid, data.get("id"))
            self.service.refresh_vectors(scope, cid)
        elif op == "protect":
            await self.write_ready(scope, cid)
            store.protect(scope, cid, data.get("collection"), data.get("id"), data.get("version"), data.get("protected"), epoch=data.get("epoch"))
        elif op == "resolve":
            store.resolve(scope, cid, data.get("id"))
        elif op == "pause":
            if type(data.get("paused")) is not bool:
                raise AlbumError("暂停状态格式不正确")
            if engine.active.get((scope, cid), {}).get("trigger") == "auto":
                await engine.cancel_wait(scope, cid)
            self.store.configure(scope, cid, {"maintenance_paused": data["paused"]}, data.get("revision"))
        else:
            raise AlbumError("不支持的记忆维护操作")
        return json_response(self._maintenance_view(scope, cid, selected), headers=PRIVATE)
