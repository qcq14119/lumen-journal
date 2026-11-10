"""Background summarizing: batch by the configured window, call, retry, apply."""

from __future__ import annotations

from ..restore_guard import managed

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from contextlib import nullcontext
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..store import AlbumError
from . import core
from .images import ImageIndex, UNSAFE_REASON
from .models import (
    MemoryModel, MemoryModelError, MemoryModels, MemoryResponseError, IMAGE_BATCH_MAX, IMAGE_REQUEST_BYTES,
    IMAGE_INPUT_TOKENS, IMAGE_OUTPUT_TOKENS, IMAGE_DESCRIPTION_RULE, check_image_descriptions,
)
from .text import estimate, IMAGE_PRIVATE
from .vectors import VectorIndex

DEFAULT_WINDOW = 32000


def capacity(window: int) -> tuple[int, int]:
    """(input tokens, output tokens) from the user's configured window only."""
    output = min(8192, max(1024, window // 5))
    return max(0, window - output - math.ceil(window * 0.05)), output


def error_kind(error: Exception) -> tuple[str, bool, int]:
    """Readable reason, whether a retry may help, and a delay in seconds."""
    status = getattr(error, "status_code", getattr(error, "status", None))
    delay = 5
    headers = getattr(error, "headers", None) or {}
    value = headers.get("Retry-After", "") if isinstance(headers, dict) else ""
    if value:
        try:
            delay = math.ceil(float(value))
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(str(value))
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                delay = math.ceil((target - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    delay = max(5, min(delay, 600))
    if status in (401, 403):
        return "模型服务鉴权失败，请检查 API Key", False, delay
    if status in (400, 404, 422):
        return "模型或请求配置不可用，请检查模型名称和窗口设置", False, delay
    if status == 429:
        return "模型服务限流", True, delay
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "模型服务超时", True, delay
    if isinstance(status, int) and status >= 500:
        return "模型服务暂时不可用", True, delay
    if isinstance(error, MemoryModelError):
        return str(error), bool(getattr(error, "retryable", False)), delay
    if isinstance(error, AlbumError):
        return str(error), False, delay
    if isinstance(error, (ConnectionError, OSError)):
        return "模型连接失败", True, delay
    return "本地处理失败", False, delay


def diagnostic(error):
    if isinstance(error, MemoryResponseError):
        return str(error)
    frames, trace = [], error.__traceback__
    while trace:
        path = Path(trace.tb_frame.f_code.co_filename)
        if path.parent == Path(__file__).parent:
            frames.append(f"{path.name}:{trace.tb_lineno}")
        trace = trace.tb_next
    kind = re.sub(r"[^a-zA-Z0-9_]", "", type(error).__name__)[:48]
    return f"{error_kind(error)[0][:160]} [diag {kind} {' > '.join(frames[-2:])}]"[:300]


def response_failure(error, expected_images):
    """Do not copy exception text, JSON errors, provider labels or payloads."""
    if isinstance(error, MemoryResponseError):
        return MemoryResponseError(error.code, retryable=error.retryable,
                                   **{**error.counts, "expected_images": expected_images})
    status = getattr(error, "status_code", getattr(error, "status", None))
    return MemoryResponseError(
        "REQUEST_TIMEOUT" if isinstance(error, (asyncio.TimeoutError, TimeoutError)) else "REQUEST_FAILED",
        expected_images=expected_images, http_status=status,
    )


def parse_summary_response(raw, labels=None):
    """Validate a full joint response; keep the legacy text-only parser separate."""
    counts = {"body_chars": len(raw) if isinstance(raw, str) else -1,
              "expected_images": len(labels or ())}

    def reject(code, **extra):
        return None, None, None, MemoryResponseError(code, **{**counts, **extra})

    if labels is None:
        parsed = core.parse_result(raw)
        if parsed is not None:
            return parsed, {}, raw, None
    if isinstance(raw, str) and not raw.strip():
        return reject("BODY_EMPTY")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        # Unwrap only one complete joint-response fence, never surrounding prose.
        fence = (re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*", raw, re.S | re.I)
                 if labels is not None and isinstance(raw, str) else None)
        if not fence:
            return reject("JSON_SYNTAX" if isinstance(raw, str) else "FIELD_TYPE")
        try:
            value = json.loads(fence[1])
        except (ValueError, TypeError):
            return reject("JSON_SYNTAX")
    if not isinstance(value, dict):
        return reject("FIELD_TYPE")
    if labels is None:
        return reject("SUMMARY_INVALID")
    rows = value.get("image_descriptions")
    counts["actual_images"] = len(rows) if isinstance(rows, list) else -1
    # Scan the entire decoded object, including unknown fields and escaped tails.
    if IMAGE_PRIVATE.search(json.dumps(value, ensure_ascii=False)):
        return reject("CONTENT_SAFETY")
    descriptions, issue = check_image_descriptions(value, labels)
    if issue:
        return reject(issue.code, **issue.counts)
    fields = ("events", "people", "profiles", "plans", "overview", "keywords")
    if not any(k in value for k in fields):
        return reject("SUMMARY_MISSING")
    clean = {key: value[key] for key in fields if key in value}
    if any(not isinstance(value, str if key == "overview" else list) for key, value in clean.items()):
        return reject("FIELD_TYPE")
    parsed = core.parse_result(clean)
    if parsed is None or parsed["skipped"]:
        return reject("SUMMARY_INVALID", invalid_items=parsed["skipped"] if parsed else -1)
    clean["image_descriptions"] = [{"id": label, "description": text} for label, text in descriptions.items()]
    return parsed, descriptions, json.dumps(clean, ensure_ascii=False), None


class MemoryService:
    """One summary task per chat; the chat itself is never blocked by this work."""

    def __init__(self, store, context, *, transport=None):
        self.store = store
        self.context = context
        self.models = MemoryModels(store)
        self.transport = transport  # tests: async callable(system, prompt, max_tokens, timeout)
        self.busy: dict[tuple[str, str], dict] = {}
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.failed: dict[tuple[str, str], str] = {}
        self.character_name = lambda scope: ""  # set by the plugin
        self._closed = False
        self._cleanup = None
        self._backup_handles = set()
        self._backup_tasks = set()
        self.vectors = VectorIndex(store)
        self.images = ImageIndex(store, context)
        from .maintenance import MemoryMaintenance
        self.maintenance = MemoryMaintenance(self)
        self.summary_intents = {}
        self._summary_handoffs = {}

    async def names(self, scope, settings=None):
        try:
            character = self.character_name(scope) or "角色"
        except Exception:
            character = "角色"
        return character, ((settings or {}).get("user_name") or "用户")

    async def start(self):
        self._closed = False
        self._cleanup = asyncio.create_task(self._cleanup_loop())

    async def close(self):
        self._closed = True
        self.summary_intents.clear()
        await self.maintenance.close()
        handoffs = list(self._summary_handoffs.values())
        for task in handoffs:
            task.cancel()
        await asyncio.gather(*handoffs, return_exceptions=True)
        self._summary_handoffs.clear()
        for handle in self._backup_handles:
            handle.cancel()
        self._backup_handles.clear()
        for task in self._backup_tasks:
            task.cancel()
        await asyncio.gather(*self._backup_tasks, return_exceptions=True)
        await self.images.close()
        tasks = [*self.tasks.values(), *([self._cleanup] if self._cleanup else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        await self.vectors.close()

    def defer_backup(self, callback):
        """The host saves after on_agent_done; shutdown owns this delayed work."""
        if self._closed:
            return
        self.store.restore_guard.check()
        epoch = self.store.restore_guard.epoch

        def start():
            self._backup_handles.discard(handle)
            if self._closed:
                return
            try:
                self.store.restore_guard.check_epoch(epoch)
            except Exception:
                return
            task = asyncio.create_task(callback())
            self._backup_tasks.add(task)
            task.add_done_callback(self._backup_tasks.discard)

        handle = asyncio.get_running_loop().call_later(3, start)
        self._backup_handles.add(handle)

    @managed
    def describe_images(self, scope, cid, *, refs, manual=False):
        """Only the caller's frozen image set can start work; never drain a backlog."""
        self.store.restore_guard.check_chat(scope, cid)
        self.store.restore_guard.external_allowed()
        if self._closed or (scope, cid) in self.tasks or (scope, cid) in self.busy:
            return False
        settings = self.store.state(scope, cid)["settings"]
        return self.images.start(scope, cid, refs, self.model, manual=manual, window=self.window(settings)[0])

    # ----------------------------------------------------------- vectors ----

    def vectors_on(self, settings):
        return bool(settings.get("vector_enabled")) and self.optional_model("embedding", settings) is not None

    @managed
    def refresh_vectors(self, scope, cid, *, rebuild=False, manual=False):
        """Bring this chat's vectors up to date in the background.

        Automatic refreshes (after a summary or an edit) never retry on their own;
        a failure is logged once and the user can press 补齐 later.
        Returns False when vectors are off or a refresh is already running.
        """
        self.store.restore_guard.check_chat(scope, cid)
        self.store.restore_guard.external_allowed()
        if self._closed:
            return False
        settings = self.store.state(scope, cid)["settings"]
        if not self.vectors_on(settings):
            return False
        model = self.optional_model("embedding", settings)

        def done(result, error):
            if error is not None:
                self.store.log(scope, cid, "index_failed", diagnostic(error))
            elif manual or result["added"] or result["removed"]:
                self.store.log(
                    scope, cid, "indexed",
                    f"向量{'重建' if rebuild else '更新'}：新增{result['added']}条，删除{result['removed']}条，共{result['total']}条记忆",
                )

        return self.vectors.start(scope, cid, model, rebuild=rebuild, on_done=done, coalesce=True)

    async def _cleanup_loop(self):
        while not self._closed:
            try:
                await asyncio.to_thread(self.store.cleanup)
            except Exception:
                pass
            await asyncio.sleep(3600)

    # ----------------------------------------------------------- model ----

    def model(self, settings):
        binding = settings.get("model_bindings", {}).get("summary") or {}
        if binding.get("mode") != "preset" or not binding.get("preset_id"):
            raise MemoryModelError("请先在总结设置里选择总结 API 连接")
        return self.models.get("summary", settings=settings)

    def optional_model(self, role, settings):
        """The chat's embedding/rerank connection, or None when not chosen."""
        binding = (settings.get("model_bindings") or {}).get(role) or {}
        if binding.get("mode") != "preset" or not binding.get("preset_id"):
            return None
        try:
            return self.models.get(role, settings=settings)
        except MemoryModelError:
            return None

    def window(self, settings):
        try:
            model = self.model(settings)
        except MemoryModelError:
            return DEFAULT_WINDOW, False
        configured = model.config.get("max_context_tokens")
        return (int(configured), True) if configured else (DEFAULT_WINDOW, False)

    async def _call(self, model, system, prompt, max_tokens, timeout, images=None):
        if self.transport:
            extra = {"images": images} if images else {}
            return await self.transport(system=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout, **extra)
        if not isinstance(model, MemoryModel):
            raise MemoryModelError("总结连接类型不正确")
        response = await model.summarize(
            system_prompt=system, prompt=prompt, max_tokens=max_tokens, timeout=timeout, images=images
        )
        return response.completion_text

    # ---------------------------------------------------------- planning ----

    def plan(self, scope, cid, *, manual=False, through=None):
        """Next batch of pending rounds, or a reason why nothing will run now.

        Args:
            through: finish rounds up to this ordinal even below the trigger
                (the remainder of a range already started in this run).
        """
        state = self.store.state(scope, cid)
        settings = state["settings"]
        pending = [t for t in self.store.pending_turns(scope, cid) if t["kind"] != "host_summary"]
        if through is not None:
            pending = [t for t in pending if t["ordinal"] <= through]
            manual = True
        if not pending:
            return {"turns": [], "reason": "没有待整理的对话"}
        if not manual and len(pending) < settings["summary_trigger_turns"]:
            return {"turns": [], "reason": f"待整理{len(pending)}轮，满{settings['summary_trigger_turns']}轮后自动整理"}
        window, _ = self.window(settings)
        limit, output = capacity(window)
        memory = self.store.memory(scope, cid)
        fixed = estimate(core.system_prompt(settings.get("summary_prompt", ""))) + 400
        # Old memory may shrink to titles; it never takes more than half the room.
        memory_budget = max(800, (limit - fixed) // 2)
        memory_cost = estimate(core.memory_for_prompt(memory, memory_budget))
        room = limit - fixed - memory_cost
        batch, used = [], 0
        target = len(pending) if manual else settings["summary_trigger_turns"]
        # Freeze the description projection before planning. Later image results
        # belong to the next batch, so they cannot enlarge this request mid-flight.
        projected = self.images.project(scope, cid, pending[: max(target, 1)])
        for turn in projected:
            if turn.get("_image_unsafe"):
                if not batch:
                    return {"turns": [], "reason": UNSAFE_REASON, "blocked": True}
                break
            cost = estimate(turn["user"]) + estimate(turn["assistant"]) + 40
            if batch and used + cost > room:
                break
            batch.append(turn)
            used += cost
        if batch and used > room:
            return {"turns": [], "reason": "单轮对话超过所选模型窗口，请在连接里调大模型窗口", "blocked": True}
        try:
            character = self.character_name(scope) or "角色"
        except Exception:
            character = "角色"
        # Validate the actual prompt, including headings and image descriptions.
        # This is also the exact prompt sent by _summarize and hashed for reuse.
        while batch:
            system, prompt = core.build_prompt(
                memory, batch, character=character,
                user=settings.get("user_name") or "用户", memory_budget=memory_budget,
                extra=settings.get("summary_prompt", ""),
            )
            if estimate(system) + estimate(prompt) <= limit:
                break
            batch.pop()
        if not batch:
            return {"turns": [], "reason": "单轮对话超过所选模型窗口，请在连接里调大模型窗口", "blocked": True}
        return {
            "turns": batch,
            "system": system,
            "prompt": prompt,
            "memory_budget": memory_budget,
            "output": output,
            "window": window,
            "split": len(batch) < min(target, len(pending)),
        }

    def _summary_signature(self, settings):
        selected = {k: settings.get(k) for k in (
            "image_describe", "summary_prompt", "user_name", "summary_timeout", "summary_max_retries",
        )}
        selected["model"] = "test" if self.transport else self.models.signature("summary", settings=settings)
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()

    def _image_prompt(self, plan, group, descriptions):
        if not plan["media"]:
            return plan["system"], plan["prompt"]
        lines = []
        for item in plan["media"]:
            ref = item["ref"]
            if ref in descriptions:
                lines.append(f"{item['id']} ({item['at']}): {descriptions[ref]}")
            elif ref in group:
                lines.append(f"{item['id']} ({item['at']}): 本次原图")
        system = plan["system"] + "\n\n" + IMAGE_DESCRIPTION_RULE
        prompt = plan["prompt"] + (
            "\n\n图片与原轮次位置对应如下。结合已验证描述与本次原图整理本批完整剧情；"
            "不要猜测尚未展示的图片。image_descriptions只返回本次原图编号。\n" + "\n".join(lines)
        )
        return system, prompt

    async def _prepare_summary(self, scope, cid, plan, settings):
        """Freeze evidence and split before loading any bounded media payload.

        Prefer whole turns. A single image-heavy turn may span several requests;
        only the last summary, informed by all preceding validated descriptions,
        can be committed with every description in one transaction.
        """
        binding = settings.get("model_bindings", {}).get("summary") or {}
        empty = {**plan, "media": [], "groups": [[]]}
        if not settings.get("image_describe") or binding.get("mode") != "preset" or not binding.get("preset_id"):
            return empty
        candidates = self.images.batch_refs(scope, cid, plan["turns"])
        if not candidates:
            return empty
        metadata = await self.images.inventory(scope, cid, plan["turns"])
        metadata = {ref: metadata[ref] for ref in candidates if ref in metadata}
        if not metadata:
            return empty
        limit, output = capacity(self.window(settings)[0])
        memory = self.store.memory(scope, cid)
        character, user = await self.names(scope, settings)
        for count in range(len(plan["turns"]), 0, -1):
            turns = plan["turns"][:count]
            refs = [ref for ref in self.images.batch_refs(scope, cid, turns) if ref in metadata]
            system, prompt = core.build_prompt(memory, turns, character=character, user=user,
                                              memory_budget=plan["memory_budget"], extra=settings.get("summary_prompt", ""))
            media = []
            ordinals = {t["ordinal"] for t in turns}
            with self.store.lock:
                for index, ref in enumerate(refs, 1):
                    locations = self.store.db.execute(
                        "SELECT t.ordinal,l.side,l.position FROM mem_turn_images l JOIN mem_turns t ON t.id=l.turn_id "
                        "WHERE t.scope=? AND t.cid=? AND l.ref=? ORDER BY t.ordinal,l.side,l.position", (scope, cid, ref)
                    ).fetchall()
                    at = "、".join(f"#{row['ordinal']} {row['side']} 图片位置{row['position']}" for row in locations if row['ordinal'] in ordinals)
                    media.append({"id": f"image-{index}", "ref": ref, "at": at, **metadata[ref]})
            prepared = {**plan, "turns": turns, "system": system, "prompt": prompt,
                        "media": media, "output": output, "split": plan["split"] or count < len(plan["turns"])}

            def fits(group, prior):
                system, prompt = self._image_prompt(prepared, group, prior)
                size = len(json.dumps([system, prompt], ensure_ascii=False).encode()) + sum(metadata[r]["bytes"] for r in group) + 16384
                return (len(group) <= IMAGE_BATCH_MAX and output - len(group) * IMAGE_OUTPUT_TOKENS >= 1024
                        and estimate(system) + estimate(prompt) + len(group) * IMAGE_INPUT_TOKENS <= limit
                        and size <= IMAGE_REQUEST_BYTES)

            if fits(refs, {}):
                return {**prepared, "groups": [refs]}
            if count > 1:
                continue
            # Preflight every slice with the maximum accepted prior description
            # length. No model call is made if even one image cannot fit safely.
            groups, group, prior = [], [], {}
            for ref in refs:
                if not fits([*group, ref], prior) and group:
                    groups.append(group)
                    prior.update({r: "图" * 120 for r in group})
                    group = []
                if not fits([*group, ref], prior):
                    raise MemoryModelError("单张图片或本轮文字超过请求限制，原图已保留")
                group.append(ref)
            if group:
                groups.append(group)
            return {**prepared, "groups": groups, "split": True}
        raise MemoryModelError("本批内容超过请求限制，原图已保留")

    def _turns_valid(self, scope, cid, turns):
        expected = {turn["id"]: turn["digest"] for turn in turns}
        current = {turn["id"]: turn["digest"] for turn in self.store.pending_turns(scope, cid)}
        return all(current.get(key) == value for key, value in expected.items())

    # ------------------------------------------------------------- work ----

    @managed
    def request(self, scope, cid, *, manual=False):
        """Start background summarizing if due. Returns immediately."""
        self.store.restore_guard.check_chat(scope, cid)
        self.store.restore_guard.external_allowed()
        key = (scope, cid)
        if self._closed or key in self.tasks or key in self.images.tasks:
            return False
        state = self.store.state(scope, cid)
        if not state["enabled"] or (not manual and not state["settings"]["auto_process"]):
            return False
        if not manual and key in self.failed:
            return False
        # A normal chat save is not itself a summary intent. Only eligible
        # work may interrupt maintenance; handoff calls request again so this
        # plan is never reused after the cancelled worker releases ownership.
        if not self.plan(scope, cid, manual=manual)["turns"]:
            return False
        if key in self.maintenance.tasks or key in self._summary_handoffs:
            self.summary_intents[key] = self.summary_intents.get(key, False) or manual
            self.maintenance.cancel(scope, cid)
            if key not in self._summary_handoffs:
                async def handoff():
                    await self.maintenance.cancel_wait(scope, cid)
                task = asyncio.create_task(handoff())
                self._summary_handoffs[key] = task
                def finished(done):
                    # stop() may cancel before handoff() enters its finally.
                    # A late callback must not release a newer handoff.
                    if self._summary_handoffs.get(key) is not done:
                        return
                    self._summary_handoffs.pop(key, None)
                    intent = self.summary_intents.pop(key, None)
                    if intent is not None and not self._closed:
                        self.request(scope, cid, manual=intent)
                task.add_done_callback(finished)
            return True
        task = asyncio.create_task(self._run(scope, cid, manual))
        self.tasks[key] = task
        task.add_done_callback(lambda _: self.tasks.pop(key, None))
        return True

    def stop(self, scope, cid):
        self.summary_intents.pop((scope, cid), None)
        handoff = self._summary_handoffs.get((scope, cid))
        if handoff:
            handoff.cancel()
        task = self.tasks.get((scope, cid))
        if task:
            task.cancel()
            return True
        return False

    @managed
    async def _run(self, scope, cid, manual):
        key = (scope, cid)
        self.failed.pop(key, None)
        batches = []
        try:
            # Freeze the range now: rounds arriving during the run wait for later.
            pending = self.store.pending_turns(scope, cid)
            if not pending:
                return
            settings = self.store.state(scope, cid)["settings"]
            if manual:
                through = pending[-1]["ordinal"]
            else:
                trigger = settings["summary_trigger_turns"]
                through = pending[(len(pending) // trigger) * trigger - 1]["ordinal"]
            while not self._closed:
                plan = self.plan(scope, cid, through=through)
                if not plan["turns"]:
                    if plan.get("blocked"):
                        raise MemoryModelError(plan["reason"])
                    break
                batches.append(await self._summarize(scope, cid, plan))
                del plan
        except asyncio.CancelledError:
            self.store.log(scope, cid, "stopped", "整理已停止，未保存的批次保留为待整理")
            raise
        except Exception as error:
            self.failed[key] = error_kind(error)[0]
            self.store.log(scope, cid, "summary_failed", diagnostic(error))
        else:
            # Only metadata crosses this boundary. A failed/cancelled summary
            # never launches maintenance from its finally block.
            if batches and not self._closed:
                del pending, settings
                try:
                    self.maintenance.after_summary(scope, cid, batches)
                except Exception as exc:
                    from .maintenance_protocol import CODES, packed
                    code = getattr(exc, "code", "")
                    self.store.log(scope, cid, "maintenance_failed", packed({"code": code if code in CODES else "STORAGE"}))
        finally:
            self.busy.pop(key, None)

    async def _summarize(self, scope, cid, plan):
        state = self.store.state(scope, cid)
        settings = state["settings"]
        signature = self._summary_signature(settings)
        plan = await self._prepare_summary(scope, cid, plan, settings)
        turns, media = plan["turns"], plan["media"]
        first, last = turns[0]["ordinal"], turns[-1]["ordinal"]
        model = None if self.transport else self.model(settings)
        model_name = "test" if self.transport else model.config.get("model", "")
        retries = settings["summary_max_retries"]
        self.busy[(scope, cid)] = {
            "stage": "summarizing", "first": first, "last": last,
            "attempt": 1, "attempts": retries + 1, "split": plan["split"],
        }
        self.store.log(scope, cid, "summarizing", f"整理第{first}—{last}轮（{len(turns)}轮，{len(media)}张图片，{len(plan['groups'])}批请求）")

        def valid_state():
            current = self.store.state(scope, cid)
            return (not self._closed and current["enabled"]
                    and self._summary_signature(current["settings"]) == signature
                    and self._turns_valid(scope, cid, turns))

        async def valid():
            if not valid_state():
                return False
            if media and not await self.images.batch_valid(scope, cid, media):
                return False
            # Host IO yields control: settings/turns must be rechecked afterward.
            return valid_state()

        def parse(raw, group):
            labels = [item["id"] for item in media if item["ref"] in group] if media else None
            return parse_summary_response(raw, labels)

        descriptions, parsed = {}, None
        for group in plan["groups"]:
            system, prompt = self._image_prompt(plan, group, descriptions)
            fingerprint = hashlib.sha256(json.dumps([
                [[t["id"], t["digest"]] for t in turns], media, group, descriptions,
                system, prompt, signature,
            ], sort_keys=True).encode()).hexdigest()
            raw = self.store.saved_result(scope, cid, fingerprint)
            parsed, group_descriptions, safe_raw, issue = parse(raw, group) if raw else (None, None, None, None)
            # Share the manual worker's media gate BEFORE loading URL/Base64.
            # Waiting chats retain only metadata, never a queued image payload.
            async with self.images._gate if group else nullcontext():
                if not await valid():
                    raise MemoryModelError("整理期间本批内容或设置已变化，结果未保存")
                images = []
                if parsed is None and group:
                    metadata = {item["ref"]: {k: item[k] for k in ("bytes", "locations", "version")} for item in media}
                    request = await self.images.current_image_batch(scope, cid, group, metadata=metadata)
                    if request["overflow"] or request["skipped"]:
                        raise MemoryModelError("整理期间图片范围已变化，结果未保存")
                    images = request.pop("images")
                    labels = {item["ref"]: item["id"] for item in media}
                    for item in images:
                        item["id"] = labels[item["ref"]]
                    del request
                try:
                    attempt = 0
                    while parsed is None:
                        if not await valid():
                            raise MemoryModelError("整理期间本批内容或设置已变化，结果未保存")
                        attempt += 1
                        self.busy[(scope, cid)]["attempt"] = attempt
                        try:
                            raw = await asyncio.wait_for(
                                self._call(model, system, prompt, plan["output"], settings["summary_timeout"], images),
                                timeout=settings["summary_timeout"] + 15,
                            )
                        except Exception as error:
                            _, retryable, delay = error_kind(error)
                            issue = response_failure(error, len(group))
                            if not retryable or attempt > retries:
                                raise issue from None
                            self.store.log(scope, cid, "retrying", f"第{attempt}次：{issue}，{delay}秒后重发本批全部图片")
                            await asyncio.sleep(delay)
                            continue
                        parsed, group_descriptions, safe_raw, issue = parse(raw, group)
                        if parsed is None:
                            if attempt > retries:
                                raise issue from None
                            self.store.log(scope, cid, "retrying", f"第{attempt}次：{issue}，整批重新生成")
                    if not await valid():
                        raise MemoryModelError("整理期间本批内容或设置已变化，结果未保存")
                    self.store.save_result(scope, cid, fingerprint, safe_raw, model_name)
                    descriptions.update({item["ref"]: group_descriptions[item["id"]] for item in media if item["ref"] in group})
                finally:
                    images.clear()
                    # Avoid retaining the loop's last media object into the next IO.
                    item = None
        if not await valid():
            raise MemoryModelError("整理期间本批内容或设置已变化，结果未保存")
        self.busy[(scope, cid)]["stage"] = "saving"
        with self.store.transaction():
            if not valid_state() or (media and not self.images.commit_image_batch(scope, cid, media, descriptions)):
                raise MemoryModelError("整理期间图片版本或设置已变化，结果未保存")
            batch, stats = self.store.apply_batch(scope, cid, turns, parsed, model_name)
        detail = f"第{first}—{last}轮：新增{stats['events']}段剧情"
        if stats["plans"]:
            detail += f"，更新{stats['plans']}项约定"
        if stats.get("relations"):
            detail += f"，记录{stats['relations']}处关系变化"
        if stats.get("profiles"):
            detail += f"，更新{stats['profiles']}条生活小档案"
        if stats["skipped"]:
            detail += f"，跳过{stats['skipped']}项无法使用的内容"
        self.store.log(scope, cid, "summarized", detail)
        self.refresh_vectors(scope, cid)
        return batch

    def progress(self, scope, cid):
        key = (scope, cid)
        state = self.store.state(scope, cid)
        plan = self.plan(scope, cid, manual=True)
        return {
            "running": key in self.tasks,
            "stage": self.busy.get(key),
            "failed": self.failed.get(key, ""),
            "pending": state["counts"]["pending"],
            "trigger": state["settings"]["summary_trigger_turns"],
            "next_batch": len(plan["turns"]),
            "window": plan.get("window"),
            "window_configured": self.window(state["settings"])[1],
            "blocked": plan.get("reason", "") if plan.get("blocked") else "",
            "images": self.images.status(scope, cid),
        }
