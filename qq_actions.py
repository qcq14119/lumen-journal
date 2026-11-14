"""Account-scoped QQ actions and durable status timers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import warnings
from collections import OrderedDict

from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable
from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from PIL import Image as PillowImage, ImageOps, UnidentifiedImageError

from .restore_guard import managed
from .store import MAX_BYTES, MAX_PIXELS, AlbumError, now, text_value


QQ_TOOLS = frozenset(
    {
        "album_qq_profile",
        "album_qq_status",
        "album_qq_status_read",
        "album_qq_poke",
        "album_qq_avatar",
    }
)
STATUS_KIND = "qq_account_status"
STATUS = {
    "online": (10, 0),
    "qme": (60, 0),
    "away": (30, 0),
    "busy": (50, 0),
    "dnd": (70, 0),
    "invisible": (40, 0),
    "listening": (10, 1028),
    "sleeping": (10, 1016),
    "studying": (10, 1018),
}


class QQConnectionUnavailable(AlbumError):
    pass


def qq_id(value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= len(value) <= 16
        or int(value) == 0
    ):
        raise AlbumError("QQ 账号或会话无效")
    return int(value)


def avatar_image(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise AlbumError("头像需小于 20 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            with PillowImage.open(io.BytesIO(raw)) as source:
                if (
                    source.format not in ("PNG", "JPEG", "WEBP")
                    or getattr(source, "n_frames", 1) != 1
                    or source.width * source.height > MAX_PIXELS
                ):
                    raise AlbumError("请选择尺寸有效的静态 PNG、JPEG 或 WebP 图片")
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((1024, 1024))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=90)
    except (
        UnidentifiedImageError,
        OSError,
        PillowImage.DecompressionBombError,
        PillowImage.DecompressionBombWarning,
    ) as exc:
        raise AlbumError("无法读取头像图片") from exc
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


class QQActions:
    def __init__(self, plugin, config):
        self.plugin = plugin
        self.store = plugin.store
        self.config = config
        self.lock = asyncio.Lock()
        self.seen = OrderedDict()
        self.observed_accounts = OrderedDict()
        self.worker = None

    def enabled(self):
        return self.config.get("qq_actions_enabled") is True

    def web_settings(self):
        grants = self.config.get("qq_actions_sessions", [])
        return {
            "enabled": self.enabled(),
            "sessions": [item for item in grants if isinstance(item, str)]
            if isinstance(grants, list)
            else [],
        }

    def is_qq_scope(self, scope):
        platform = self.plugin.context.get_platform_inst(scope.partition(":")[0])
        return platform is not None and platform.meta().name == "aiocqhttp"

    def observe_account(self, event):
        if event.get_platform_name() != "aiocqhttp":
            return
        scope = event.unified_msg_origin
        try:
            account = str(qq_id(event.get_self_id()))
            session = MessageSession.from_str(scope)
        except (AlbumError, ValueError):
            return
        if (
            session.platform_id != event.get_platform_id()
            or session.message_type != event.get_message_type()
            or session.message_type not in (MessageType.FRIEND_MESSAGE, MessageType.GROUP_MESSAGE)
        ):
            return
        accounts = self.observed_accounts.setdefault(scope, set())
        if len(accounts) < 32:
            accounts.add(account)
        self.observed_accounts.move_to_end(scope)
        if len(self.observed_accounts) > 100:
            self.observed_accounts.popitem(last=False)

    def accounts_for_scope(self, scope):
        if not self.is_qq_scope(scope):
            raise AlbumError("请选择已启用的 OneBot QQ 会话")
        self.store.character_for(scope)
        return sorted(self.observed_accounts.get(scope, ()), key=int)

    def save_web_settings(self, data):
        previous = self.web_settings()
        if data.get("expected") != previous:
            raise AlbumError("QQ 设置已在别处修改，请刷新后再保存")
        enabled, sessions = data.get("enabled"), data.get("sessions")
        if (
            type(enabled) is not bool
            or not isinstance(sessions, list)
            or len(sessions) > 100
        ):
            raise AlbumError("QQ 设置格式不正确，最多允许 100 条授权")
        if not all(isinstance(item, str) and 0 < len(item) <= 300 for item in sessions):
            raise AlbumError("QQ 授权会话格式不正确")
        sessions = list(dict.fromkeys(sessions))
        for item in sessions:
            if item in previous["sessions"]:
                continue
            parts = item.split(":", 3)
            if len(parts) != 4 or not parts[0]:
                raise AlbumError("QQ 授权会话格式不正确")
            platform, account, kind, session_id = parts
            qq_id(account)
            session = MessageSession.from_str(f"{platform}:{kind}:{session_id}")
            if not self.is_qq_scope(str(session)):
                raise AlbumError("请选择已启用的 OneBot QQ 会话")
            if session.message_type == MessageType.FRIEND_MESSAGE:
                qq_id(session_id)
            elif session.message_type == MessageType.GROUP_MESSAGE:
                for identifier in session_id.split("_", 1):
                    qq_id(identifier)
            else:
                raise AlbumError("请授权 QQ 私聊或群聊")
            self.store.character_for(str(session))
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            raise AlbumError("当前宿主无法保存 QQ 设置")
        keys = ("qq_actions_enabled", "qq_actions_sessions")
        original = {key: self.config[key] for key in keys if key in self.config}
        try:
            save(dict(zip(keys, (enabled, sessions))))
        except OSError:
            for key in keys:
                if key in original:
                    self.config[key] = original[key]
                else:
                    self.config.pop(key, None)
            raise
        return self.web_settings()

    def client(self, platform_id):
        platform = self.plugin.context.get_platform_inst(platform_id)
        if platform is None or platform.meta().name != "aiocqhttp":
            raise QQConnectionUnavailable("QQ 连接不可用")
        return platform.get_client()

    def authorize(self, event, *, admin=False):
        if not self.enabled() or event.get_platform_name() != "aiocqhttp":
            raise AlbumError("本会话未开放 QQ 资料与互动")
        scope = event.unified_msg_origin
        session = MessageSession.from_str(scope)
        if (
            session.platform_id != event.get_platform_id()
            or session.message_type != event.get_message_type()
            or session.message_type
            not in (MessageType.FRIEND_MESSAGE, MessageType.GROUP_MESSAGE)
        ):
            raise AlbumError("此 QQ 会话不支持资料与互动")
        if admin and not self.admin_private(event, session):
            raise AlbumError("请在管理员 QQ 私聊中操作")
        account = qq_id(event.get_self_id())
        character = self.authorized_character(session.platform_id, account, scope)
        return session, account, character

    @staticmethod
    def admin_private(event, session):
        return (
            session.message_type == MessageType.FRIEND_MESSAGE
            and session.session_id == event.get_sender_id()
            and event.is_admin()
        )

    def authorized_character(self, platform, account, scope):
        self.store.restore_guard.external_allowed()
        session = MessageSession.from_str(scope)
        grants = self.config.get("qq_actions_sessions", [])
        grant = (
            f"{platform}:{account}:{session.message_type.value}:{session.session_id}"
        )
        if (
            not self.enabled()
            or session.platform_id != platform
            or not isinstance(grants, list)
            or grant not in grants
        ):
            raise AlbumError("此 QQ 账号未获当前会话的动作授权")
        return self.store.character_for(scope)["id"]

    def claim(self, event, action):
        key = self.plugin.event_key(event, f"qq:{event.get_self_id()}:{action}")
        if key in self.seen:
            raise AlbumError("本条消息已处理过此 QQ 动作")
        self.seen[key] = None
        if len(self.seen) > 1024:
            self.seen.popitem(last=False)

    @staticmethod
    async def call(client, account, action, **params):
        try:
            await asyncio.wait_for(
                client.call_action(action, self_id=account, **params), 12
            )
        except ApiNotAvailable:
            return "unavailable"
        except ActionFailed:
            return "failed"
        except Exception:
            return "unknown"
        return "confirmed"

    @managed
    async def profile(self, event, nickname, signature):
        session, account, _ = self.authorize(event, admin=True)
        nickname, signature = text_value(nickname, 60), text_value(signature, 120)
        if not nickname and not signature:
            raise AlbumError("请提供要修改的昵称或签名")
        client = self.client(session.platform_id)
        self.claim(event, "profile")
        fields = {}
        if nickname:
            fields["nickname"] = await self.call(
                client, account, "set_qq_profile", nickname=nickname
            )
        if signature:
            fields["signature"] = await self.call(
                client, account, "set_self_longnick", longNick=signature
            )
        states = set(fields.values())
        return {
            "status": states.pop() if len(states) == 1 else "partial",
            "fields": fields,
        }

    @managed
    async def poke(self, event):
        session, account, _ = self.authorize(event)
        sender = qq_id(event.get_sender_id())
        if sender == account:
            raise AlbumError("不能戳机器人自己")
        if session.message_type == MessageType.FRIEND_MESSAGE:
            if sender != qq_id(session.session_id):
                raise AlbumError("当前好友与会话不一致")
            action, params = "friend_poke", {"user_id": sender}
        else:
            group_id = event.get_group_id()
            target = qq_id(group_id)
            if session.session_id not in (group_id, f"{sender}_{group_id}"):
                raise AlbumError("当前群与会话不一致")
            action, params = "group_poke", {"user_id": sender, "group_id": target}
        client = self.client(session.platform_id)
        self.claim(event, "poke")
        return {"status": await self.call(client, account, action, **params)}

    @managed
    async def avatar(self, event, asset_id):
        session, account, character = self.authorize(event, admin=True)
        asset_id = text_value(asset_id, 100)
        asset = self.store.authorized_asset(asset_id, event.unified_msg_origin, "chat")
        path = self.store.asset_path(asset, "delivery")
        if path.stat().st_size > MAX_BYTES:
            raise AlbumError("头像需小于 20 MB")
        raw = await asyncio.to_thread(path.read_bytes)
        file = await asyncio.to_thread(avatar_image, raw)
        if self.authorize(event, admin=True) != (session, account, character):
            raise AlbumError("QQ 会话已变化")
        if (
            self.store.authorized_asset(asset_id, event.unified_msg_origin, "chat")
            != asset
        ):
            raise AlbumError("相册图片已变化，请重新选择")
        client = self.client(session.platform_id)
        self.claim(event, "avatar")
        return {"status": await self.call(client, account, "set_qq_avatar", file=file)}

    def status_record(self, platform, account):
        key = hashlib.sha256(f"{platform}:{account}".encode()).hexdigest()
        try:
            return self.store.get(STATUS_KIND, key)
        except AlbumError:
            return {
                "id": key,
                "platform": platform,
                "account": account,
                "current": "unknown",
                "ends": 0,
            }

    def save_status(self, row, **updates):
        return self.store.save(
            STATUS_KIND, row["id"], row | updates, row.get("revision")
        )

    @managed
    async def set_status(self, event, status, duration, delay):
        if (
            not isinstance(status, str)
            or status not in STATUS
            or type(duration) is not int
            or not 1 <= duration <= 1440
            or type(delay) is not int
            or not 0 <= delay <= 1440
        ):
            raise AlbumError(
                "请选择支持的状态，持续 1 至 1440 分钟，延后 0 至 1440 分钟"
            )
        async with self.lock:
            session, account, character = self.authorize(event, admin=True)
            client = self.client(session.platform_id)
            self.claim(event, "status")
            row = self.status_record(session.platform_id, account)
            owner = {"scope": str(session), "character": character}
            scheduled = {
                "status": status,
                "duration": duration,
                "at": now() + delay * 60,
                "owner": owner,
            }
            if delay:
                self.save_status(row, scheduled=scheduled)
                return {"status": "scheduled", "at": scheduled["at"]}
            return await self.apply_status(row, client, status, duration, owner)

    @managed
    async def read_status(self, event):
        session, account, _ = self.authorize(event, admin=True)
        row = self.status_record(session.platform_id, account)
        pending = row.get("scheduled")
        recovery = row.get("recovery", "pending" if row["ends"] else "")
        uncertain = bool(row.get("inflight") or row.get("uncertain"))
        scheduled_state = ""
        if pending:
            scheduled_state = (
                pending.get("state", "pending")
                if self.timer_authorized(row, pending.get("owner"))
                else "unauthorized"
            )
            if scheduled_state == "pending" and not uncertain and pending["at"] <= now():
                try:
                    self.client(row["platform"])
                except QQConnectionUnavailable:
                    scheduled_state = "waiting_connection"
        if row["ends"] and (uncertain or not row.get("recovery_owner")):
            recovery = "verify"
        elif (
            row["ends"]
            and recovery in ("pending", "waiting_connection")
            and not self.timer_authorized(row, row.get("recovery_owner"))
        ):
            recovery = "unauthorized"
        return {
            "status": "unknown" if uncertain else row["current"],
            "last_confirmed": row["current"],
            "ends": row["ends"],
            "scheduled": {
                k: v for k, v in pending.items() if k not in {"owner", "state"}
            }
            if pending
            else None,
            "scheduled_state": scheduled_state,
            "operation": "unknown" if row.get("inflight") else row.get("outcome", ""),
            "recovery": recovery,
        }

    def timer_authorized(self, row, owner):
        if not isinstance(owner, dict) or not owner.get("scope"):
            return False
        try:
            scope = owner["scope"]
            session = MessageSession.from_str(scope)
            if session.message_type != MessageType.FRIEND_MESSAGE:
                return False
            if session.session_id not in self.plugin.context.get_config(umo=scope).get(
                "admins_id", []
            ):
                return False
            return self.authorized_character(
                row["platform"], row["account"], scope
            ) == owner.get("character")
        except (AlbumError, ValueError):
            return False

    async def apply_status(
        self, row, client, status, duration, owner, *, operation="immediate"
    ):
        restoring = operation == "restore"
        row = self.save_status(row, inflight=operation)
        primary, extended = STATUS[status]
        result = await self.call(
            client,
            row["account"],
            "set_online_status",
            status=primary,
            ext_status=extended,
            battery_status=0,
        )
        if result == "confirmed":
            ends = now() + duration * 60 if status != "online" else 0
            self.save_status(
                row,
                inflight=False,
                current=status,
                ends=ends,
                outcome=result,
                scheduled=row.get("scheduled") if restoring else None,
                recovery="pending" if ends else "",
                recovery_owner=owner if ends else None,
                uncertain=False,
            )
            return {"status": result, "current": status, "ends": ends}
        updates = {"inflight": False, "outcome": result}
        if result == "unknown":
            updates.update(uncertain=True, recovery="verify" if row["ends"] else "")
        if restoring and result in ("failed", "unavailable"):
            updates["recovery"] = (
                "failed" if result == "failed" else "waiting_connection"
            )
        if operation == "scheduled":
            updates["scheduled"] = (
                row["scheduled"] | {"state": "waiting_connection"}
                if result == "unavailable"
                else None
            )
        self.save_status(row, **updates)
        return {"status": result}

    @managed
    async def tick(self):
        if not self.enabled():
            return
        self.store.restore_guard.external_allowed()
        async with self.lock:
            for row in self.store.all(STATUS_KIND):
                if not self.enabled():
                    return
                if row.get("inflight"):
                    interrupted = row["inflight"]
                    self.save_status(
                        row,
                        inflight=False,
                        uncertain=True,
                        recovery="verify" if row["ends"] else "",
                        scheduled=None
                        if interrupted in (True, "scheduled")
                        else row.get("scheduled"),
                        outcome="unknown",
                    )
                    continue
                scheduled = row.get("scheduled")
                timestamp = now()
                due = (
                    scheduled
                    and scheduled["at"] <= timestamp
                    and self.timer_authorized(row, scheduled.get("owner"))
                )
                restoring = (
                    not due
                    and 0 < row["ends"] <= timestamp
                    and row.get("recovery", "pending")
                    in ("pending", "waiting_connection")
                    and self.timer_authorized(row, row.get("recovery_owner"))
                )
                if not (due or restoring):
                    continue
                try:
                    client = self.client(row["platform"])
                except QQConnectionUnavailable:
                    if restoring and row.get("recovery") != "waiting_connection":
                        self.save_status(
                            row, recovery="waiting_connection", outcome="unavailable"
                        )
                    elif due and scheduled.get("state") != "waiting_connection":
                        self.save_status(
                            row,
                            scheduled=scheduled | {"state": "waiting_connection"},
                            outcome="unavailable",
                        )
                    continue
                status, duration = (
                    ("online", 0)
                    if restoring
                    else (scheduled["status"], scheduled["duration"])
                )
                await self.apply_status(
                    row,
                    client,
                    status,
                    duration,
                    row.get("recovery_owner") if restoring else scheduled["owner"],
                    operation="restore" if restoring else "scheduled",
                )

    async def run(self):
        while True:
            try:
                await self.tick()
            except AlbumError:
                pass
            except Exception:
                logger.warning("QQ status timer could not be checked")
            await asyncio.sleep(5)

    def start(self):
        if self.enabled() and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self.run())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
