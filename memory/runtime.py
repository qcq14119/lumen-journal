"""Chat hooks: back up rounds, trim summarized history, inject memory temporarily.

How the chat file shrinks: AstrBot builds the model's message list from
``req.contexts`` after this hook and saves that list as the new history. Removing
summarized rounds here (keeping the latest ``keep_turns`` of them) is therefore
the only history change; the memory block is a temporary part and is never saved.
"""

from __future__ import annotations

from ..restore_guard import managed

import json
import time
import uuid

from astrbot.core.agent.message import TextPart

from ..store import AlbumError
from . import core
from .recall import Recall
from .store import identity_refs
from .text import estimate, history_turns, image_ref, image_url, legacy_block, text_content, turn_key

RUN_KEY = "lumen_memory_run"
RESTORE_TOKENS = 24000


def backup_rows(turns):
    """Complete rounds as backup rows; the newest unfinished round is skipped."""
    rows = []
    for turn in turns:
        if not turn["complete"] or turn["kind"] == "host_summary":
            continue
        rows.append(
            {
                "key": turn_key(turn),
                "checkpoint": turn.get("checkpoint", ""),
                "kind": turn["kind"],
                "user": turn["user"],
                "assistant": turn["assistant"],
                "at": turn["at"],
                "image_refs": turn.get("image_refs", []),
                "identity_refs": identity_refs(turn),
                "legacy_key": turn.get("legacy_key", ""),
                "legacy_body_digest": turn.get("legacy_body_digest", ""),
            }
        )
    return rows


class MemoryRuntime:
    def __init__(self, store, service, context):
        self.store, self.service, self.context = store, service, context
        self.active: dict[tuple[str, str], set] = {}
        self.last: dict[tuple[str, str], dict] = {}
        self.recall = Recall(store, service)

    # ---------------------------------------------------------- helpers ----

    def busy(self, scope, cid):
        return bool(self.active.get((scope, cid)))

    def enter(self, scope, cid, key):
        self.active.setdefault((scope, cid), set()).add(key)

    def leave(self, scope, cid, key):
        keys = self.active.get((scope, cid), set())
        keys.discard(key)
        if not keys:
            self.active.pop((scope, cid), None)

    async def conversation(self, scope, cid):
        conv = await self.context.conversation_manager.get_conversation(scope, cid)
        if conv is None or conv.user_id != scope or conv.cid != cid:
            raise AlbumError("聊天不存在或不属于当前对象")
        return conv

    def capture(self, scope, cid, history):
        """Register history without starting model work (including imported history)."""
        return self.capture_snapshot(scope, cid, history)["added"]

    @managed
    def capture_snapshot(self, scope, cid, history):
        start = legacy_block(history)
        turns = history_turns(history[start:])
        with self.store.transaction():
            result = self.store.capture_turns(scope, cid, turns)
            result["fresh"] = self.service.images.register(scope, cid, turns, result)
        result["turns"] = turns
        return result

    # ------------------------------------------------------------- hooks ----

    async def begin(self, event, req):
        """on_llm_request: never raises; on any failure the reply goes on unchanged."""
        conv = getattr(req, "conversation", None)
        cid = getattr(conv, "cid", "") if conv else ""
        scope = event.unified_msg_origin
        if not cid or event.get_extra(RUN_KEY):
            return
        self.store.restore_guard.check_chat(scope, cid)
        self.store.restore_guard.check_event(event)
        generation = self.store.state(scope, cid)["generation"]
        key = "run:" + uuid.uuid4().hex
        event.set_extra(RUN_KEY, {"scope": scope, "cid": cid, "key": key, "generation": generation})
        self.enter(scope, cid, key)
        try:
            state = self.store.state(scope, cid)
            if not state["enabled"]:
                return
            history = req.contexts if isinstance(req.contexts, list) else []
            proactive = getattr(self, "proactive", None)
            if proactive:
                # This hook runs in the host session lock. A req projection or a
                # backup alone is not evidence: acknowledge the actual stored
                # marker/body before a trim can remove the only host copy.
                persisted = await self.conversation(scope, cid)
                stored_history = json.loads(persisted.history or "[]")
                if not isinstance(stored_history, list):
                    raise AlbumError("聊天历史格式不正确")
                proactive.observe_saved(scope, cid, stored_history)
                del stored_history, persisted
            captured = self.capture_snapshot(scope, cid, history)
            trimmed, info = self.trim(scope, cid, history, state["settings"], captured=captured)
            del captured
            req.contexts = trimmed
            if state["settings"]["inject_enabled"]:
                block = await self.injection(scope, cid, state, event, req)
                if block:
                    req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
                    info["memory_tokens"] = estimate(block)
            self.last[(scope, cid)] = {**info, "at": int(time.time())}
        except Exception as error:
            # Memory problems must not stop the reply; the history stays untouched.
            try:
                self.store.log(scope, cid, "inject_failed", f"本轮未加入记忆：{type(error).__name__}")
            except Exception:
                pass

    async def done(self, event):
        """on_agent_done: release the busy mark and consider summarizing."""
        run = event.get_extra(RUN_KEY)
        if not isinstance(run, dict):
            return
        scope, cid = run["scope"], run["cid"]
        self.leave(scope, cid, run["key"])
        self.store.restore_guard.check_event(event)
        if run["generation"] != self.store.state(scope, cid)["generation"]:
            return
        # The host saves history after this hook; back up the finished round then.
        self.service.defer_backup(lambda: self.after_save(scope, cid, generation=run["generation"]))

    @managed
    async def after_save(self, scope, cid, *, generation=None):
        try:
            if self.service._closed:
                return
            state = self.store.state(scope, cid)
            self.store.restore_guard.check_chat(scope, cid)
            if generation is not None and generation != state["generation"]:
                return
            if not state["enabled"] or self.busy(scope, cid):
                return
            conv = await self.conversation(scope, cid)
            history = json.loads(conv.history or "[]")
            del conv
            captured = self.capture_snapshot(scope, cid, history)
            # Registration never starts a separate vision call. Images join the
            # next configured summary batch, or an explicitly confirmed manual fill.
            del history, captured
            self.service.request(scope, cid)
        except Exception:
            pass

    # ------------------------------------------------------------ trimming --

    def trim(self, scope, cid, history, settings, *, captured=None):
        """Drop summarized rounds except the newest ``keep_turns``; add back missing pending ones."""
        start = legacy_block(history)
        body = history[start:]
        turns = captured["turns"] if captured is not None else history_turns(body)
        result = captured if captured is not None else self.store.resolve_capture(scope, cid, turns)
        resolved = result["resolved"]
        summarized = [
            i for i, t in enumerate(turns)
            if resolved.get(i, {}).get("status") == "summarized"
        ]
        keep = settings["keep_turns"]
        cut = set(summarized[:-keep] if keep else summarized)
        # A text-only or incomplete summary must never bypass media retention.
        ready_refs = self.service.images.ready(scope, cid, self.service.images.refs(turns))
        cut = {i for i in cut if self.service.images.refs([turns[i]]) <= ready_refs.keys()}
        proactive = getattr(self, "proactive", None)
        if proactive:
            cut = {i for i in cut if turns[i]["kind"] != "proactive" or proactive.trim_confirmed(scope, cid, turns[i])}
        # Only remove a contiguous summarized prefix, so order never breaks.
        removable = set()
        for index in range(len(turns)):
            if index in cut:
                removable.add(index)
            elif turns[index]["kind"] != "host_summary":
                break
        dropped_ids = {id(m) for i in removable for m in turns[i]["messages"]}
        kept = [m for m in body if id(m) not in dropped_ids]
        # Unsummarized rounds no longer in the chat file (undo, host compression)
        # come back from the backup so nothing is forgotten before summarizing.
        present = {r["id"] for r in resolved.values()} | result["uncertain_ids"]
        missing = [
            t for t in self.store.pending_turns(scope, cid)
            if t["id"] not in present and t["kind"] in ("chat", "proactive")
        ]
        missing = self.service.images.project(scope, cid, missing)
        unsafe = sum(bool(t.get("_image_unsafe")) for t in missing)
        missing = [t for t in missing if not t.get("_image_unsafe")]
        # A very large backlog must not overflow the chat window: keep the
        # newest rounds that fit, and let summarizing catch up.
        budget, used, start_at = RESTORE_TOKENS, 0, len(missing)
        for index in range(len(missing) - 1, -1, -1):
            cost = estimate(missing[index]["user"]) + estimate(missing[index]["assistant"])
            if used + cost > budget:
                break
            used += cost
            start_at = index
        skipped = start_at
        missing = missing[start_at:]
        restored = []
        for turn in missing:
            if turn["user"]:
                restored.append({"role": "user", "content": turn["user"]})
            restored.append({"role": "assistant", "content": turn["assistant"]})
            # Carry the backup identity: AstrBot saves this list, and without the
            # checkpoint the next backup would treat the round as a new one.
            restored.append({"role": "_checkpoint", "content": {"id": turn["key"]}})
        info = {
            "rounds": len(turns) - len(removable),
            "removed": len(removable),
            "restored": len(missing),
            "restore_skipped": skipped,
            "restore_unsafe": unsafe,
            "legacy_block_removed": bool(start),
        }
        # Count rounds in the retained history, including a possible unfinished
        # newest round. Only user/ordinary-assistant image parts can be replaced.
        remaining = history_turns(kept)
        image_keep = settings.get("image_keep_turns", 5)
        older = remaining[:-image_keep] if image_keep else remaining
        confirmed = {id(m) for index, turn in enumerate(turns) if index in resolved for m in turn["messages"]}
        eligible = {id(m) for t in older for m in t["messages"] if id(m) in confirmed}
        ready = self.service.images.ready(scope, cid, self.service.images.refs(older))
        replaced = 0
        changed_messages = set()
        for index, message in enumerate(kept):
            if (
                id(message) not in eligible or message.get("role") not in ("user", "assistant")
                or message.get("tool_calls") or message.get("_no_save")
                or not isinstance(message.get("content"), list)
            ):
                continue
            parts = message["content"]
            changed = None
            for position, part in enumerate(parts):
                if not isinstance(part, dict) or part.get("_no_save"):
                    continue
                url = image_url(part)
                ref = image_ref(url) if url else ""
                if ref in ready:
                    if changed is None:
                        changed = list(parts)
                    changed[position] = {"type": "text", "text": f"[图片#{ref[:12]}：{ready[ref]}]"}
                    replaced += 1
            if changed is not None:
                kept[index] = {**message, "content": changed}
                changed_messages.add(id(message))
        # Removing an old alt can erase the only distinction between two legacy
        # rounds. Carry the resolved identity before saving their text projection.
        checkpoints = {}
        for index, turn in enumerate(turns):
            row = resolved.get(index)
            if row and not turn["checkpoint"] and any(id(m) in changed_messages for m in turn["messages"]):
                checkpoints[id(turn["messages"][-1])] = row["key"]
        if checkpoints:
            original_kept = [m for m in body if id(m) not in dropped_ids]
            with_identity = []
            for original, message in zip(original_kept, kept):
                with_identity.append(message)
                if id(original) in checkpoints:
                    with_identity.append({"role": "_checkpoint", "content": {"id": checkpoints[id(original)]}})
            kept = with_identity
        info["images_replaced"] = replaced
        return restored + kept, info

    # ----------------------------------------------------------- injection --

    @managed
    async def injection(self, scope, cid, state, event, req):
        self.store.restore_guard.check_chat(scope, cid)
        self.store.restore_guard.external_allowed()
        memory = self.store.memory(scope, cid)
        if not any(memory.get(k) for k in ("overview", "events", "people", "profiles", "plans")):
            return ""
        character, user = await self.service.names(scope, state["settings"])
        message = text_content(req.prompt or event.message_str or "")
        overflow = core.timeline_overflow(
            memory,
            state["settings"]["memory_tokens"],
            character=character,
            user=user,
        )
        related = []
        info = {"mode": "skipped"}
        if overflow:
            recent = [t for t in history_turns(req.contexts or []) if t["kind"] == "chat"]
            related, info = await self.recall.find(
                scope,
                cid,
                state["settings"],
                memory,
                message,
                history=recent,
                pool=set(overflow),
            )
            self.recall.log(scope, cid, memory, info)
        self.last[(scope, cid)] = {
            **self.last.get((scope, cid), {}),
            "recall": {"mode": info["mode"], "count": len(related)},
        }
        return core.render_injection(
            memory,
            state["settings"]["memory_tokens"],
            query="" if info.get("attempted") else message,
            character=character,
            user=user,
            related=related,
        )

    # ------------------------------------------------------- plugin speech --

    @managed
    async def business(self, scope, cid, key, text, *, kind="business", status="sent", generation=None):
        """Record confirmed plugin actions (photo sent, Qzone post) as a backup round."""
        if not cid:
            return
        state = self.store.state(scope, cid)
        self.store.restore_guard.check_chat(scope, cid)
        if generation is not None and generation != state["generation"]:
            return
        if not state["enabled"]:
            return
        self.store.archive(
            scope,
            cid,
            [{"key": "biz:" + key, "kind": "business", "user": "", "assistant": text_content(text), "at": int(time.time())}],
        )
