"""Bounded public-output gate for album tool turns using public host hooks only."""

from __future__ import annotations

from hashlib import sha256

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.persona_error_reply import extract_persona_custom_error_message_from_event


class AlbumOutput:
    # A typical reply is far smaller. Never flush an undecided prefix on overflow.
    MAX_CHARS = 65536
    MAX_STEPS = 128
    OVERFLOW = "本轮回复过长，未发送未确认的工具前文。"
    UNVERIFIED = "本轮部分中间回复未能核对，已省略。"
    INCOMPLETE = "模型未能完成本次回复，请稍后再试。"

    def __init__(self, context, *, image_turn=False):
        self.context = context
        self.image_turn = image_turn
        self.done = False
        self.clean = lambda text: text
        self.final_text = ""
        self.pending = []
        self.ready = []
        self.size = 0
        self.overflow = False
        self.tools = set()
        self.executing = 0
        self.seen = set(self._tool_groups())
        self.steps = []
        self.steps_invalid = False
        self.had_tools = False
        self.saw_intermediate = False

    @staticmethod
    def _signature(text):
        return len(text), sha256(text.encode("utf-8")).digest()

    def _tool_groups(self):
        groups = {}
        for message in getattr(self.context, "messages", ()):
            calls = getattr(message, "tool_calls", None)
            if getattr(message, "role", "") != "assistant" or not calls:
                continue
            ids = tuple(call.id for call in calls)
            content = getattr(message, "content", None)
            text = content if isinstance(content, str) else "".join(
                part.text for part in content or () if getattr(part, "type", "") == "text"
            )
            groups[ids] = ({call.function.name for call in calls}, self._signature(text))
        return groups

    def observe(self):
        # The runner appends the whole assistant tool set only after that step.
        # Hooks retain its names if context compression removes the appended step.
        groups = self._tool_groups()
        for ids, (names, signature) in groups.items():
            if ids not in self.seen:
                self.had_tools = True
                if len(self.steps) < self.MAX_STEPS:
                    self.steps.append((signature, self.image_turn or "album_generate" in names))
                else:
                    self.steps_invalid = True
                self.tools.update(names)
                self.executing = 0
                self._resolve()
        self.seen.update(groups)

    def tool_start(self, name):
        self.observe()
        self.had_tools = True
        self.tools.add(name)
        self.executing += 1

    def tool_end(self):
        # Do not resolve here: another tool in this same step may be album_generate.
        self.executing = max(0, self.executing - 1)

    def _resolve(self):
        if not self.image_turn and "album_generate" not in self.tools:
            self.ready.extend(self.pending)
        else:
            self.size -= sum(len(part.text) for part in self.pending)
        self.pending = []
        self.tools.clear()

    def buffer(self, parts):
        for part in parts:
            if not isinstance(part, Plain):
                continue
            if self.overflow:
                continue
            if self.size + len(part.text) > self.MAX_CHARS:
                self.pending.clear()
                self.ready.clear()
                self.size = 0
                self.overflow = True
                continue
            self.pending.append(Plain(part.text))
            self.size += len(part.text)

    def take_ready(self):
        if self.tools and not self.executing:
            self._resolve()
        result, self.ready = self.ready, []
        self.size -= sum(len(part.text) for part in result)
        return result

    def decorate(self, result):
        self.observe()
        if self.done:
            prefix = self.take_ready()
            self.pending.clear()
            if self.overflow:
                prefix.append(Plain(self.OVERFLOW))
            if self.had_tools and not self.saw_intermediate:
                result.chain = self._filter_merged(result.chain)
            else:
                for part in result.chain:
                    if isinstance(part, Plain):
                        part.text = self.clean(part.text)
            result.chain = prefix + result.chain
            return
        self.saw_intermediate = True
        prefix = self.take_ready()
        self.buffer(result.chain)
        result.chain = prefix + [part for part in result.chain if not isinstance(part, Plain)]

    def _filter_merged(self, chain):
        # run_agent merges complete step chains. Match their exact public text
        # against stable tool-call identities; media keep their original order.
        # Only hashes/lengths are retained, never another copy of step bodies.
        expected = [(sig, drop) for sig, drop in self.steps if sig[0]]
        if self.final_text:
            expected.append((self._signature(self.final_text), False))
        position, size, digest, plain_count = 0, 0, sha256(), 0
        removed = set()
        valid = not self.steps_invalid
        for index, part in enumerate(chain):
            if not isinstance(part, Plain) or not part.text:
                continue
            if position >= len(expected):
                valid = False
                break
            (length, fingerprint), drop = expected[position]
            if plain_count:
                digest.update(b" ")  # Host MessageChain.get_plain_text separator.
                size += 1
            digest.update(part.text.encode("utf-8"))
            size += len(part.text)
            plain_count += 1
            if drop:
                removed.add(index)
            if size > length or (size == length and digest.digest() != fingerprint):
                valid = False
                break
            if size == length:
                position += 1
                size, digest, plain_count = 0, sha256(), 0
        if valid and position == len(expected) and not size:
            return [part for index, part in enumerate(chain) if index not in removed]
        # Context compression or a changed chain cannot certify an old prefix.
        # Keep media and the confirmed final answer, without guessing boundaries.
        kept = [part for part in chain if not isinstance(part, Plain)]
        kept.append(Plain(self.UNVERIFIED))
        if self.final_text:
            kept.append(Plain(self.final_text))
        return kept

    def finish(self, response):
        self.observe()
        self.done = True
        self.final_text = response.completion_text or "".join(
            part.text for part in getattr(response.result_chain, "chain", ()) if isinstance(part, Plain)
        )

    def clear(self):
        self.pending.clear()
        self.ready.clear()
        self.tools.clear()
        self.seen.clear()
        self.steps.clear()
        self.final_text = ""
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
            if flow.overflow:
                yield MessageChain().message(flow.OVERFLOW)
            if flow.done and flow.final_text:
                yield MessageChain().message(flow.final_text)
            elif not flow.done and not flow.overflow and not (
                event.is_stopped() or event.get_extra("agent_user_aborted")
                or event.get_extra("agent_stop_requested")
            ):
                # The public run_agent stream erases the error response type and
                # errors do not call on_agent_done. Never replay undecided text;
                # use the same configured persona error, or an accurate safe cause.
                message = extract_persona_custom_error_message_from_event(event)
                yield MessageChain().message(message or flow.INCOMPLETE)
    finally:
        if isinstance(flow, AlbumOutput):
            flow.clear()
        close = getattr(source, "aclose", None)
        if close:
            await close()
