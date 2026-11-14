"""Bounded public-output gate for album tool turns using public host hooks only."""

from __future__ import annotations

import re
from hashlib import sha256
from weakref import ref

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.persona_error_reply import extract_persona_custom_error_message_from_event

AVATAR_TOOL = "album_qq_avatar"
AVATAR_RECEIPTS = {
    "confirmed": "已经换上新头像啦！",
    "failed": "这次没能换成 QQ 头像。",
    "unavailable": "QQ 连接不可用，这次没能换头像。",
    "unknown": "这次还没确认 QQ 头像是否换成功。",
}


class AlbumOutput:
    # A typical reply is far smaller. Never flush an undecided prefix on overflow.
    MAX_CHARS = 65536
    MAX_STEPS = 128
    INCOMPLETE = "模型未能完成本次回复，请稍后再试。"

    def __init__(self, context, *, image_turn=False, stream_guarded=None):
        self.context = context
        self.image_turn = image_turn
        self.done = False
        self.clean = lambda text: text
        self.final_text = ""
        self.final_signature = None
        self.has_final_body = False
        self.pending = []
        self.ready = []
        self.meme_parts = set()
        self.avatar_outcome = None
        self.avatar_receipt_emitted = False
        # None means ordinary results; False means a host stream without our wrapper.
        self.stream_guarded = stream_guarded
        self.had_meme = False
        self.size = 0
        self.overflow = False
        self.tools = set()
        self.executing = 0
        messages = getattr(context, "messages", ())
        self.cursor = len(messages)
        self.boundary = ref(messages[-1]) if messages else None
        self.steps = []
        self.steps_invalid = False
        self.had_tools = False
        self.saw_intermediate = False

    @property
    def avatar_receipt(self):
        if self.avatar_outcome is None:
            return ""
        return AVATAR_RECEIPTS.get(self.avatar_outcome, AVATAR_RECEIPTS["unknown"])

    def take_avatar_receipt(self):
        if self.avatar_receipt_emitted:
            return ""
        receipt = self.avatar_receipt
        if receipt:
            self.avatar_receipt_emitted = True
        return receipt

    @staticmethod
    def _signature(text):
        return len(text), sha256(text.encode("utf-8")).digest()

    @staticmethod
    def clean_meme_text(text):
        lines, opened = text.splitlines(keepends=True), None
        for index, line in enumerate(lines):
            match = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n)?", line)
            if not match:
                if opened is None:
                    lines[index] = re.sub(r"^[ \t]*<wait_for_user_trigger>", "", line)
                continue
            fence, tail = match.groups()
            if opened is None:
                opened = (index, fence)
            elif fence[0] == opened[1][0] and len(fence) >= len(opened[1]) and not tail.strip():
                opened = None
        text = "".join(lines)
        if (
            opened is not None
            and lines[opened[0]].strip() == opened[1]
            and not any(line.strip() for line in lines[opened[0] + 1:])
        ):
            text = "".join(lines[:opened[0]]).rstrip()
        return text if text.strip() else ""

    def observe(self):
        # Follow actual append occurrences, not provider call IDs (which may
        # repeat even with identical bodies). Keep only one weak boundary and
        # bounded signatures.
        messages = getattr(self.context, "messages", ())
        start = self.cursor
        if start:
            boundary = self.boundary() if self.boundary else None
            if boundary is None:
                start = len(messages)
                self.steps_invalid = True
            elif start > len(messages) or messages[start - 1] is not boundary:
                start = next((i + 1 for i, message in enumerate(messages) if message is boundary), None)
                if start is None:
                    start = len(messages)
                    self.steps_invalid = True
        for index in range(start, len(messages)):
            message = messages[index]
            calls = getattr(message, "tool_calls", None)
            if getattr(message, "role", "") != "assistant" or not calls:
                continue
            names = {call.function.name for call in calls}
            self.had_tools = True
            drop = self.image_turn or bool(names & {"album_generate", AVATAR_TOOL})
            meme = not drop and names == {"send_meme"}
            self.had_meme |= meme
            if len(self.steps) < self.MAX_STEPS:
                content = getattr(message, "content", None)
                text = content if isinstance(content, str) else "".join(
                    part.text for part in content or () if getattr(part, "type", "") == "text"
                )
                action = "drop" if drop else "meme" if meme else "keep"
                self.steps.append((self._signature(text), action))
            else:
                self.steps_invalid = True
            self.tools.update(names)
            self.executing = 0
            self._resolve()
        self.cursor = len(messages)
        self.boundary = ref(messages[-1]) if messages else None

    def tool_start(self, name):
        self.observe()
        self.had_tools = True
        if name == AVATAR_TOOL and self.avatar_outcome is None:
            self.avatar_outcome = "started"
        self.tools.add(name)
        self.executing += 1

    def tool_end(self):
        # Do not resolve here: another tool in this same step may be album_generate.
        self.executing = max(0, self.executing - 1)

    def _resolve(self):
        if not self.image_turn and not self.tools & {"album_generate", AVATAR_TOOL}:
            meme = self.tools == {"send_meme"}
            if meme:
                self.size -= sum(max(1, len(part.text)) for part in self.pending)
                text = self.clean_meme_text("".join(part.text for part in self.pending))
                self.pending = [Plain(text)] if text else []
                self.size += sum(max(1, len(part.text)) for part in self.pending)
                self.had_meme = True
                self.meme_parts.update(id(part) for part in self.pending)
            self.ready.extend(self.pending)
        else:
            self.size -= sum(max(1, len(part.text)) for part in self.pending)
        self.pending = []
        self.tools.clear()

    def buffer(self, parts):
        for part in parts:
            if not isinstance(part, Plain):
                continue
            if self.overflow:
                continue
            if self.size + max(1, len(part.text)) > self.MAX_CHARS:
                self.pending.clear()
                self.ready.clear()
                self.meme_parts.clear()
                self.size = 0
                self.overflow = True
                continue
            self.pending.append(Plain(part.text))
            self.size += max(1, len(part.text))

    def take_ready(self):
        if self.tools and not self.executing:
            self._resolve()
        # Keep subsequent text in order behind a held tool draft.
        end = len(self.ready)
        if not self.done:
            end = next(
                (i for i, part in enumerate(self.ready) if id(part) in self.meme_parts), end
            )
        result, self.ready = self.ready[:end], self.ready[end:]
        self.size -= sum(max(1, len(part.text)) for part in result)
        if self.done and self.had_meme:
            result = [part for part in result if not self.has_final_body or id(part) not in self.meme_parts]
            self.meme_parts.clear()
        return result

    def decorate(self, result):
        self.observe()
        if self.done:
            prefix = self.take_ready()
            self.pending.clear()
            if self.had_tools and not self.saw_intermediate:
                result.chain = self._filter_merged(result.chain)
            else:
                for part in result.chain:
                    if isinstance(part, Plain):
                        part.text = self.clean(part.text)
                if self.had_meme:
                    plain = [part for part in result.chain if isinstance(part, Plain)]
                    text = "".join(part.text for part in plain)
                    cleaned = self.clean_meme_text(text)
                    if plain and cleaned != text:
                        plain[0].text = cleaned
                        result.chain = [part for part in result.chain if not isinstance(part, Plain) or part is plain[0]]
            result.chain = prefix + result.chain
            return
        self.saw_intermediate = True
        prefix = self.take_ready()
        self.buffer(result.chain)
        result.chain = prefix + [part for part in result.chain if not isinstance(part, Plain)]

    def _filter_merged(self, chain):
        # run_agent merges complete step chains. Match their exact public text
        # against each observed occurrence; media keep their original order.
        # Only hashes/lengths are retained, never another copy of step bodies.
        expected = list(self.steps)
        if self.final_signature and self.final_signature[0]:
            expected.append((self.final_signature, "final" if self.had_meme else "keep"))
        cursor, empty_steps = 0, 0
        removed = set()
        replacements = {}
        valid = not self.steps_invalid
        for (length, fingerprint), action in expected:
            if not length:
                # Host history cannot distinguish absent text from one empty
                # Plain. Both have no public content; only this zero-length
                # ambiguity is optional when matching the next complete step.
                empty_steps += 1
                continue
            start, matched = cursor, False
            for _ in range(empty_steps + 1):
                size, digest, plain_count = 0, sha256(), 0
                for index in range(start, len(chain)):
                    part = chain[index]
                    if not isinstance(part, Plain):
                        continue
                    if plain_count:
                        digest.update(b" ")  # Host get_plain_text separator.
                        size += 1
                    digest.update(part.text.encode("utf-8"))
                    size += len(part.text)
                    plain_count += 1
                    if size >= length:
                        matched = size == length and digest.digest() == fingerprint
                        break
                if matched:
                    indices = [i for i in range(start, index + 1) if isinstance(chain[i], Plain)]
                    if action == "drop" or (action == "meme" and self.has_final_body):
                        removed.update(indices)
                    elif action in {"meme", "final"}:
                        text = "".join(chain[i].text for i in indices)
                        cleaned = self.clean_meme_text(text)
                        if cleaned != text:
                            replacements[indices[0]] = Plain(cleaned)
                            removed.update(indices[1:])
                    cursor = index + 1
                    break
                while start < len(chain) and not isinstance(chain[start], Plain):
                    start += 1
                if start == len(chain) or chain[start].text:
                    break
                start += 1
            if not matched:
                valid = False
                break
            empty_steps = 0
        if valid and all(not isinstance(part, Plain) or not part.text for part in chain[cursor:]):
            return [replacements.get(index, part) for index, part in enumerate(chain) if index not in removed]
        # Context compression or a changed chain cannot certify an old prefix.
        # Keep media and the confirmed final answer, without guessing boundaries.
        kept = [part for part in chain if not isinstance(part, Plain)]
        if self.final_text:
            kept.append(Plain(self.final_text))
        return kept

    def finish(self, response):
        self.observe()
        self.done = True
        self.final_text = response.completion_text or "".join(
            part.text for part in getattr(response.result_chain, "chain", ()) if isinstance(part, Plain)
        )
        self.final_signature = self._signature(self.final_text)
        if self.had_meme:
            self.final_text = self.clean_meme_text(self.final_text)
        # A program receipt does not replace the fallback text of a meme-only step.
        body = self.final_text.rstrip()
        if receipt := self.avatar_receipt:
            body = body.removesuffix(receipt)
        self.has_final_body = bool(body.strip())
        if (
            self.had_meme
            and response.role == "assistant"
            and not response.completion_text
            and not response.result_chain
        ):
            # Keep an empty final boundary so non-streaming releases held prose.
            response.result_chain = MessageChain(chain=[Plain("")])

    def clear(self):
        self.pending.clear()
        self.ready.clear()
        self.meme_parts.clear()
        self.tools.clear()
        self.cursor = 0
        self.boundary = None
        self.steps.clear()
        self.final_text = ""
        self.final_signature = None
        self.context = None
        self.size = 0


async def guard_stream(source, event, key):
    """Delay each text step until its full tools are known; forward media unchanged."""
    flow = None
    try:
        async for chain in source:
            flow = event.get_extra(key)
            if not isinstance(flow, AlbumOutput) or chain is None:
                yield chain
                continue
            if chain.type in {"reasoning", "break", "audio_chunk", "tool_call", "agent_stats"}:
                yield chain
                continue
            flow.observe()
            ready = flow.take_ready()
            if ready:
                yield chain.derive(ready)
            flow.buffer(chain.chain)
            media = [part for part in chain.chain if not isinstance(part, Plain)]
            if media:
                yield chain.derive(media)
        flow = event.get_extra(key)
        if isinstance(flow, AlbumOutput):
            flow.observe()
            ready = flow.take_ready()
            if ready:
                yield MessageChain(chain=ready)
            if flow.done and flow.final_text:
                yield MessageChain().message(flow.final_text)
            elif not flow.done and not (
                event.is_stopped() or event.get_extra("agent_user_aborted")
                or event.get_extra("agent_stop_requested")
            ):
                # The public run_agent stream erases the error response type and
                # errors do not call on_agent_done. Never replay undecided text;
                # use the same configured persona error, or an accurate safe cause.
                message = extract_persona_custom_error_message_from_event(event)
                chain = MessageChain().message(message or flow.INCOMPLETE)
                if receipt := flow.take_avatar_receipt():
                    chain.message("\n" + receipt)
                yield chain
    finally:
        if isinstance(flow, AlbumOutput):
            flow.clear()
        close = getattr(source, "aclose", None)
        if close:
            await close()
