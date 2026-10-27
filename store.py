"""Durable album, immutable reference versions and job reservations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import warnings
from pathlib import Path
from datetime import date, datetime, time as daytime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 32_000_000
SCENE_TTL = 24 * 60 * 60
DEFAULT_SETTINGS = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "size": "1024x1024",
    "reference_field": "image[]",
    "poll_interval": 5,
    "task_timeout": 900,
    "qzone_enabled": False,
    "qzone_visibility": 4,
    "generated_qzone_allowed": True,
    "generated_retention_days": 30,
    "generated_max_count": 100,
    "records_retention_days": 90,
    "records_max_count": 1000,
    "providers": [],
    "preferred_provider": "primary",
    "max_retries": 3,
    "retry_delay": 5,
    "parallel_backup": False,
    "retry_unknown": False,
    "settings_revision": 0,
    "primary_name": "",
    "primary_enabled": True,
    "provider_order": [],
}


class AlbumError(Exception):
    """An error safe to display without provider payloads or secrets."""


def now() -> int:
    return int(time.time())


def uid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:16]


def text_value(value, limit: int = 2000) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise AlbumError(f"文字长度需在 {limit} 字以内")
    return value.strip()


def strings(value, limit: int = 20) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise AlbumError("列表格式或数量不正确")
    return list(dict.fromkeys(text_value(x, 300) for x in value if x))


def credential_entries(site: dict) -> list[dict]:
    """Read private key entries, including the original single-key format."""
    if "api_keys" in site:
        return [dict(entry) for entry in site["api_keys"]]
    return (
        [{"id": "legacy", "name": "密钥 1", "value": site["api_key"]}]
        if site.get("api_key")
        else []
    )


def merge_credentials(previous: dict, patch: dict, same_address: bool) -> dict:
    """Merge one site's key edits without reusing another address's secrets.

    Args:
        previous: The site's previously saved private settings.
        patch: Submitted entries; blank values retain a matching saved key.
        same_address: Whether saved credentials may be reused.

    Returns:
        Private entries, selected identity, and the compatible active API key.
    """
    old = credential_entries(previous) if same_address else []
    active = patch.get("active_key_id", previous.get("active_key_id", ""))
    active = text_value(active, 80) or (old[0]["id"] if old else "")
    values = patch.get("api_keys", old)
    if not isinstance(values, list) or len(values) > 20:
        raise AlbumError("每个站点最多保存20把密钥")
    entries, ids = [], set()
    for item in values:
        if not isinstance(item, dict):
            raise AlbumError("密钥配置格式不正确")
        key_id = text_value(item.get("id") or uid("key_"), 80)
        if key_id in ids:
            raise AlbumError("密钥编号重复")
        ids.add(key_id)
        saved = next((k for k in old if k["id"] == key_id), None)
        secret = text_value(item.get("value", ""), 2000) or (
            saved["value"] if saved else ""
        )
        if not secret and not same_address:
            continue
        if not secret:
            raise AlbumError("请填写新增密钥；修改地址后需重新填写本站密钥")
        if saved and secret != saved["value"]:
            replacement = uid("key_")
            if active == key_id:
                active = replacement
            key_id = replacement
        entries.append(
            {
                "id": key_id,
                "name": text_value(item.get("name", ""), 80)
                or f"密钥 {len(entries) + 1}",
                "value": secret,
            }
        )
    if "api_keys" not in patch:
        if patch.get("clear_api_key") is True:
            entries = [k for k in entries if k["id"] != active]
            active = entries[0]["id"] if entries else ""
        elif patch.get("api_key"):
            secret = text_value(patch["api_key"], 2000)
            entry = next((k for k in entries if k["id"] == active), None)
            if entry is None or entry["value"] != secret:
                new = {
                    "id": "legacy"
                    if not credential_entries(previous) and "api_keys" not in previous
                    else uid("key_"),
                    "name": entry["name"] if entry else "密钥 1",
                    "value": secret,
                }
                entries = (
                    [new if k is entry else k for k in entries]
                    if entry
                    else [*entries, new]
                )
                active = new["id"]
    if not entries:
        active = ""
    elif not active:
        active = entries[0]["id"]
    selected = next((k for k in entries if k["id"] == active), None)
    if entries and selected is None:
        raise AlbumError("请选择本站当前使用的密钥")
    return {
        "api_keys": entries,
        "active_key_id": active,
        "api_key": selected["value"] if selected else "",
    }


class Store:
    """Store small records in SQLite and validated media outside the database."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("originals", "delivery", "thumbnails", "tmp"):
            (self.root / name).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "album.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                revision INTEGER NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, data TEXT NOT NULL, updated INTEGER NOT NULL,
                UNIQUE(scope,fingerprint));
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_retained ON jobs(json_extract(data,'$.archived'));
            CREATE INDEX IF NOT EXISTS deliveries_job ON deliveries(json_extract(data,'$.job_id'),state);
            CREATE INDEX IF NOT EXISTS deliveries_retained ON deliveries(json_extract(data,'$.archived'));
            CREATE INDEX IF NOT EXISTS records_retained ON records(kind,json_extract(data,'$.archived'));
            CREATE INDEX IF NOT EXISTS records_plan_run ON records(kind,json_extract(data,'$.plan_id'),json_extract(data,'$.at'));
        """)
        self.settings_path = self.root / "settings.json"
        self.settings = dict(DEFAULT_SETTINGS)
        if self.settings_path.exists():
            self.settings.update(
                json.loads(self.settings_path.read_text(encoding="utf-8"))
            )

    def close(self):
        with self.lock:
            self.db.close()

    def get(self, kind: str, key: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT data,revision FROM records WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
        if row is None:
            raise AlbumError("记录不存在")
        value = {**json.loads(row["data"]), "id": key, "revision": row["revision"]}
        if kind == "asset":
            value["allow_chat"] = value["allow_qzone"] = value["kind"] in (
                "generated",
                "imported",
            ) and not value.get("expired")
        return value

    def all(self, kind: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,data,revision FROM records WHERE kind=? AND json_extract(data,'$.archived') IS NOT 1 ORDER BY rowid DESC",
                (kind,),
            ).fetchall()
        values = [
            {**json.loads(x["data"]), "id": x["id"], "revision": x["revision"]}
            for x in rows
        ]
        if kind == "asset":
            for value in values:
                value["allow_chat"] = value["allow_qzone"] = value["kind"] in (
                    "generated",
                    "imported",
                ) and not value.get("expired")
        return values

    def save(
        self, kind: str, key: str, data: dict, revision: int | None = None
    ) -> dict:
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT revision FROM records WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            if old and revision != old["revision"]:
                raise AlbumError("内容已在另一页面更新，请刷新后再保存")
            rev = old["revision"] + 1 if old else 1
            payload = {k: v for k, v in data.items() if k not in ("id", "revision")}
            self.db.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                (kind, key, json.dumps(payload, ensure_ascii=False), rev),
            )
        return self.get(kind, key)

    def save_character(self, data: dict) -> dict:
        key = data.get("id") or uid("char_")
        old = (
            self.get("character", key)
            if data.get("id")
            else {"versions": [], "active_version": None}
        )
        name = text_value(data.get("name", ""), 80)
        if not name:
            raise AlbumError("请填写角色名称")
        payload = {
            **old,
            "name": name,
            "appearance": text_value(data.get("appearance", "")),
            "style": text_value(data.get("style", "")),
            "failure_style": text_value(data.get("failure_style", ""), 500),
            "sessions": strings(data.get("sessions", []), 100),
        }
        with self.lock:
            for char in self.all("character"):
                if char["id"] != key and set(char["sessions"]) & set(
                    payload["sessions"]
                ):
                    raise AlbumError("该会话已绑定另一角色，请先解除原绑定")
            return self.save("character", key, payload, data.get("revision"))

    def character_for(self, scope: str) -> dict:
        matches = [
            x for x in self.all("character") if scope and scope in x.get("sessions", [])
        ]
        if len(matches) != 1:
            raise AlbumError("请在相册管理页为当前会话绑定角色")
        return matches[0]

    def activate(
        self,
        character_id: str,
        asset_ids: list[str],
        revision: int,
        version_id: str = "",
    ) -> dict:
        with self.lock:
            char = self.get("character", character_id)
            if revision != char["revision"]:
                raise AlbumError("角色配置已更新，请刷新")
            if version_id:
                versions = [v for v in char["versions"] if v["id"] == version_id]
                if not versions:
                    raise AlbumError("参考版本不存在")
                ids = versions[0]["asset_ids"]
            else:
                ids = strings(asset_ids, 4)
            if not ids:
                raise AlbumError("请先上传至少一张角色参考图")
            for asset_id in ids:
                asset = self.get("asset", asset_id)
                if (
                    asset["character_id"] != character_id
                    or asset["kind"] != "reference"
                    or asset["trash"]
                ):
                    raise AlbumError("参考图不属于当前角色或已被移除")
            if not version_id:
                version_id = uid("ref_")
                char["versions"].append(
                    {"id": version_id, "asset_ids": ids, "created": now()}
                )
            char["active_version"] = version_id
            return self.save("character", character_id, char, revision)

    def import_image(
        self,
        raw: bytes,
        character_id: str,
        kind: str = "imported",
        scope: str = "",
        description: str = "",
        *,
        provenance: dict | None = None,
    ) -> dict:
        """Validate media, strip EXIF, and create bounded delivery/preview files.

        Args:
            raw: Encoded image bytes, never accepted as a filesystem path.
            character_id: Owner of the asset.
            kind: reference, scene, generated, or imported.
            scope: Empty for administrator-shared media; otherwise original session.
            description: Short picture description.
            provenance: Internal generation job and immutable reference version.
        """
        self.get("character", character_id)
        if kind not in ("reference", "scene", "generated", "imported"):
            raise AlbumError("图片用途不正确")
        if not raw or len(raw) > MAX_BYTES:
            raise AlbumError("图片需小于 20 MB")
        if shutil.disk_usage(self.root).free < 512 * 1024 * 1024:
            raise AlbumError("相册所在磁盘可用空间不足 512 MB，请先释放空间")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as source:
                    if source.width * source.height > MAX_PIXELS:
                        raise AlbumError("图片像素过大，最多 3200 万像素")
                    if (
                        source.format not in ("PNG", "JPEG", "WEBP")
                        or getattr(source, "n_frames", 1) != 1
                    ):
                        raise AlbumError("请使用静态 PNG、JPEG 或 WebP 图片")
                    source.load()
                    original = ImageOps.exif_transpose(source).convert(
                        "RGBA"
                        if "A" in source.getbands() or "transparency" in source.info
                        else "RGB"
                    )
        except (
            UnidentifiedImageError,
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise AlbumError("无法读取图片，或图片像素超过限制") from exc
        clean = io.BytesIO()
        original.info.clear()
        original.save(clean, format="PNG")
        raw = clean.getvalue()
        if len(raw) > MAX_BYTES:
            raise AlbumError("标准化后的图片超过 20 MB，请先缩小图片")
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            for asset in self.all("asset"):
                if (
                    kind != "generated"
                    and (
                        asset["hash"],
                        asset["character_id"],
                        asset["scope"],
                        asset["kind"],
                        asset["trash"],
                    )
                    == (digest, character_id, scope, kind, False)
                    and not asset.get("expired")
                ):
                    if kind == "scene":
                        asset["expires_at"] = now() + SCENE_TTL
                        return self.save("asset", asset["id"], asset, asset["revision"])
                    return asset
            paths = {
                "original": f"originals/{digest}.png",
                "delivery": f"delivery/{digest}.jpg",
                "thumbnail": f"thumbnails/{digest}.jpg",
            }
            image = Image.new("RGB", original.size, "white")
            image.paste(
                original,
                mask=original.getchannel("A") if original.mode == "RGBA" else None,
            )
            for role, relative in paths.items():
                destination = self.root / relative
                if destination.exists():
                    continue
                tmp = self.root / "tmp" / uid("write_")
                if role == "original":
                    tmp.write_bytes(raw)
                else:
                    resized = image.copy()
                    resized.thumbnail(
                        (1600, 1600) if role == "delivery" else (480, 480)
                    )
                    resized.save(
                        tmp, format="JPEG", quality=85 if role == "delivery" else 75
                    )
                os.replace(tmp, destination)
            asset = {
                "hash": digest,
                "character_id": character_id,
                "scope": scope,
                "kind": kind,
                "created": now(),
                "description": text_value(description, 1000),
                "tags": [],
                "width": original.width,
                "height": original.height,
                "bytes": len(raw),
                **paths,
                "favorite": False,
                "trash": False,
                "allow_chat": kind in ("imported", "generated"),
                "allow_qzone": kind in ("imported", "generated"),
                "provenance": provenance or {},
                **({"expires_at": now() + SCENE_TTL} if kind == "scene" else {}),
            }
            return self.save("asset", uid("img_"), asset)

    def edit_asset(self, key: str, patch: dict) -> dict:
        return self.edit_assets([{**patch, "id": key}])[0]

    def edit_assets(self, patches: list[dict]) -> list[dict]:
        """Validate every edit before committing the batch atomically.

        Args:
            patches: At most 100 unique assets with current revisions and edits.
        """
        if not isinstance(patches, list) or not 1 <= len(patches) <= 100:
            raise AlbumError("一次请选择1至100张照片")
        with self.lock:
            prepared, ids = [], set()
            for patch in patches:
                if not isinstance(patch, dict):
                    raise AlbumError("照片修改格式不正确")
                key = text_value(patch.get("id", ""), 100)
                asset = self.get("asset", key)
                if key in ids or patch.get("revision") != asset["revision"]:
                    raise AlbumError("照片重复或已更新，请刷新后重新选择")
                ids.add(key)
                if asset.get("expired"):
                    raise AlbumError("照片文件已清理，不能再修改或恢复")
                for field in ("favorite", "trash"):
                    if field in patch:
                        if not isinstance(patch[field], bool):
                            raise AlbumError("开关值必须为布尔值")
                        asset[field] = patch[field]
                if asset["kind"] in ("reference", "scene"):
                    asset["favorite"] = False
                    if asset["kind"] == "reference" and asset["trash"]:
                        char = self.get("character", asset["character_id"])
                        if any(key in v["asset_ids"] for v in char["versions"]):
                            raise AlbumError(
                                "此图片属于参考版本，不能移除；可保留旧版本并启用新参考"
                            )
                if asset["trash"]:
                    for job in self.jobs():
                        if job["state"] in (
                            "queued",
                            "submitting",
                            "running",
                            "unknown",
                            "downloading",
                            "download_failed",
                        ) and key in [
                            job.get("asset_id"),
                            *job.get("reference_ids", []),
                        ]:
                            raise AlbumError("进行中的任务正在使用此图片")
                        if (
                            job["state"] == "ready"
                            and (
                                job.get("delivery") in ("pending", "sending", "unknown")
                                or job.get("qzone_delivery")
                                in ("pending", "sending", "unknown")
                            )
                            and key == job.get("asset_id")
                        ):
                            raise AlbumError("照片尚有待处理或待核对的投递，暂不能删除")
                    for row in self.db.execute(
                        "SELECT data FROM deliveries WHERE state IN ('sending','unknown')"
                    ):
                        delivery = json.loads(row[0])
                        if key in [
                            delivery.get("asset_id"),
                            *delivery.get("asset_ids", []),
                        ]:
                            raise AlbumError("图片正在发送或发送结果待核对，暂不能删除")
                if "description" in patch:
                    asset["description"] = text_value(patch["description"], 1000)
                if "tags" in patch:
                    asset["tags"] = strings(patch["tags"])
                payload = {
                    k: v for k, v in asset.items() if k not in ("id", "revision")
                }
                prepared.append(
                    (
                        json.dumps(payload, ensure_ascii=False),
                        asset["revision"] + 1,
                        key,
                    )
                )
            with self.db:
                self.db.executemany(
                    "UPDATE records SET data=?,revision=? WHERE kind='asset' AND id=?",
                    prepared,
                )
            return [self.get("asset", patch["id"]) for patch in patches]

    def authorized_asset(self, key: str, scope: str, purpose: str) -> dict:
        char = self.character_for(scope)
        asset = self.get("asset", key)
        if (
            asset["character_id"] != char["id"]
            or asset["scope"] not in ("", scope)
            or asset["trash"]
            or asset.get("expired")
            or asset["kind"] == "scene"
            and asset.get("expires_at", asset["created"] + SCENE_TTL) <= now()
        ):
            raise AlbumError("当前会话无权使用此图片")
        if purpose in ("chat", "qzone") and (
            asset["kind"] in ("reference", "scene") or not asset.get("allow_" + purpose)
        ):
            raise AlbumError("此图片未允许用于该发送方式")
        return asset

    def cleanup_media(self) -> dict:
        """Enforce reference TTL and generated-photo retention without losing active media.

        Returns:
            Expired count and whether protected photos prevent another generation.
            Metadata remains for provenance; shared files are only unlinked once.
        """
        with self.lock:
            protected, active_jobs = set(), set()
            for job in self.jobs():
                if job["state"] in (
                    "queued",
                    "submitting",
                    "running",
                    "unknown",
                    "downloading",
                    "download_failed",
                ) or (
                    job["state"] == "ready"
                    and (
                        job.get("delivery") in ("pending", "sending", "unknown")
                        or job.get("qzone_delivery")
                        in ("pending", "sending", "unknown")
                    )
                ):
                    active_jobs.add(job["id"])
                    protected.update(job.get("reference_ids", []))
                    protected.add(job.get("asset_id", ""))
            rows = self.db.execute(
                "SELECT data FROM deliveries WHERE state IN ('sending','unknown')"
            ).fetchall()
            for row in rows:
                data = json.loads(row[0])
                protected.update(data.get("asset_ids", []))
                protected.add(data.get("asset_id", ""))
            assets = self.all("asset")
            protected.update(
                a["id"]
                for a in assets
                if a.get("provenance", {}).get("job_id") in active_jobs
            )
            generated = sorted(
                (
                    a
                    for a in assets
                    if a["kind"] == "generated" and not a.get("expired")
                ),
                key=lambda a: a["created"],
            )
            remaining = len(generated)
            expired_ids = set()
            cutoff = now() - self.settings["generated_retention_days"] * 86400
            for asset in generated:
                if asset["favorite"] or asset["id"] in protected:
                    continue
                if (
                    asset["created"] <= cutoff
                    or remaining > self.settings["generated_max_count"]
                ):
                    expired_ids.add(asset["id"])
                    remaining -= 1
            expired = 0
            for asset in assets:
                if (
                    asset.get("expired")
                    or asset["id"] in protected
                    or not (
                        asset["id"] in expired_ids
                        or asset["kind"] == "scene"
                        and asset.get("expires_at", asset["created"] + SCENE_TTL)
                        <= now()
                    )
                ):
                    continue
                shared = any(
                    other["id"] != asset["id"]
                    and not other.get("expired")
                    and other["hash"] == asset["hash"]
                    for other in assets
                )
                if not shared:
                    paths = []
                    for field, directory in (
                        ("original", "originals"),
                        ("delivery", "delivery"),
                        ("thumbnail", "thumbnails"),
                    ):
                        target = (self.root / asset[field]).resolve()
                        if not target.is_relative_to(self.root / directory):
                            raise AlbumError("临时参考路径超出相册目录，已停止清理")
                        paths.append(target)
                    for target in paths:
                        try:
                            target.unlink(missing_ok=True)
                        except OSError as exc:
                            raise AlbumError(
                                "图片清理未完成，已保留记录等待核对"
                            ) from exc
                asset.update(expired=True, allow_chat=False, allow_qzone=False)
                self.save("asset", asset["id"], asset, asset["revision"])
                expired += 1
            return {
                "expired": expired,
                "capacity_blocked": remaining >= self.settings["generated_max_count"]
                and not any(
                    not a.get("expired")
                    and not a["favorite"]
                    and a["id"] not in protected
                    for a in generated
                ),
            }

    def asset_path(self, asset: dict, variant: str = "original") -> Path:
        if asset.get("expired"):
            raise AlbumError("图片已按保留规则清理，任务元数据仍保留")
        if variant not in ("original", "delivery", "thumbnail"):
            raise AlbumError("不支持的图片版本")
        target = (self.root / asset[variant]).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            raise AlbumError("图片文件不存在")
        return target

    def list_assets(
        self,
        character_id: str = "",
        query: str = "",
        category: str = "all",
        offset: int = 0,
        limit: int = 24,
        scope: str | None = None,
        purpose: str = "chat",
        start_date: str = "",
        end_date: str = "",
        timezone: str = "Asia/Shanghai",
    ) -> dict:
        try:
            zone = ZoneInfo(text_value(timezone, 80))
            if any(
                value
                and (
                    not isinstance(value, str)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                )
                for value in (start_date, end_date)
            ):
                raise ValueError
            start = (
                datetime.combine(
                    date.fromisoformat(start_date), daytime.min, zone
                ).timestamp()
                if start_date
                else None
            )
            end = (
                datetime.combine(
                    date.fromisoformat(end_date) + timedelta(days=1), daytime.min, zone
                ).timestamp()
                if end_date
                else None
            )
            if start is not None and end is not None and start >= end:
                raise ValueError
        except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError) as exc:
            raise AlbumError(
                "日期需为YYYY-MM-DD，结束日期不早于开始日期，时区需为有效IANA名称"
            ) from exc
        rows = []
        for a in self.all("asset"):
            if (
                a.get("expired")
                or a["kind"] == "scene"
                and a.get("expires_at", a["created"] + SCENE_TTL) <= now()
            ):
                continue
            if character_id and a["character_id"] != character_id:
                continue
            if (start is not None and a["created"] < start) or (
                end is not None and a["created"] >= end
            ):
                continue
            if a["trash"] != (category == "trash"):
                continue
            if category == "favorite" and not a["favorite"]:
                continue
            if (
                category in ("reference", "scene", "generated", "imported")
                and a["kind"] != category
            ):
                continue
            if category in ("all", "favorite") and a["kind"] in ("reference", "scene"):
                continue
            if scope is not None and (
                a["scope"] not in ("", scope)
                or purpose in ("chat", "qzone")
                and (
                    a["kind"] in ("reference", "scene") or not a.get("allow_" + purpose)
                )
            ):
                continue
            haystack = (a["description"] + " " + " ".join(a["tags"])).casefold()
            if query and not all(word in haystack for word in query.casefold().split()):
                continue
            rows.append(a)
        rows.sort(key=lambda a: (a["created"], a["id"]), reverse=True)
        return {
            "items": rows[max(0, offset) : max(0, offset) + min(48, max(1, limit))],
            "total": len(rows),
        }

    def save_settings(self, patch: dict) -> dict:
        with self.lock:
            settings = dict(self.settings)
            if (
                "settings_revision" in patch
                and patch["settings_revision"] != settings["settings_revision"]
            ):
                raise AlbumError("设置已在其他页面或切站后改变，请刷新再保存")
            for key in (
                "base_url",
                "model",
                "size",
                "reference_field",
                "primary_name",
            ):
                if key in patch:
                    settings[key] = text_value(patch[key], 2000)
            settings.update(
                merge_credentials(
                    self.settings,
                    patch,
                    settings["base_url"] == self.settings["base_url"],
                )
            )
            if settings["reference_field"] not in ("image", "image[]"):
                raise AlbumError("参考图字段仅支持 image 或 image[]")
            for key in (
                "qzone_enabled",
                "generated_qzone_allowed",
                "parallel_backup",
                "retry_unknown",
                "primary_enabled",
            ):
                if key in patch:
                    if not isinstance(patch[key], bool):
                        raise AlbumError("开关值必须为布尔值")
                    settings[key] = patch[key]
            if "qzone_visibility" in patch:
                if patch["qzone_visibility"] not in (1, 4, 64):
                    raise AlbumError("请选择所有人、好友或仅自己可见")
                settings["qzone_visibility"] = patch["qzone_visibility"]
            for key, bounds in {
                "task_timeout": (60, 3600),
                "poll_interval": (3, 60),
                "generated_retention_days": (1, 365),
                "generated_max_count": (1, 1000),
                "records_retention_days": (1, 3650),
                "records_max_count": (20, 10000),
                "max_retries": (0, 10),
                "retry_delay": (1, 300),
            }.items():
                if key in patch:
                    v = patch[key]
                    if type(v) is not int or not bounds[0] <= v <= bounds[1]:
                        raise AlbumError("任务或相册保留参数超出允许范围")
                    settings[key] = v
            if "providers" in patch:
                from .provider import validate_url

                values = patch["providers"]
                if not isinstance(values, list) or len(values) > 4:
                    raise AlbumError("最多配置5个站点")
                old = {p["id"]: p for p in self.settings["providers"]}
                providers, ids = [], {"primary"}
                for value in values:
                    if not isinstance(value, dict):
                        raise AlbumError("站点配置格式不正确")
                    key = text_value(value.get("id") or uid("site_"), 80)
                    if key in ids:
                        raise AlbumError("站点编号重复")
                    ids.add(key)
                    site = {
                        k: text_value(value.get(k, default), 2000)
                        for k, default in {
                            "name": "",
                            "base_url": "",
                            "model": "",
                            "size": "1024x1024",
                            "reference_field": "image[]",
                        }.items()
                    }
                    site["base_url"] = validate_url(site["base_url"].rstrip("/"))
                    if urlsplit(site["base_url"]).query:
                        raise AlbumError("API基础地址不能带查询参数")
                    site["name"] = site["name"] or urlsplit(site["base_url"]).hostname
                    if site["reference_field"] not in ("image", "image[]"):
                        raise AlbumError("参考图字段仅支持 image 或 image[]")
                    enabled = value.get("enabled", False)
                    if not isinstance(enabled, bool):
                        raise AlbumError("站点开关必须为布尔值")
                    previous = old.get(key, {})
                    credentials = merge_credentials(
                        previous, value, previous.get("base_url") == site["base_url"]
                    )
                    if enabled and not (credentials["api_key"] and site["model"]):
                        raise AlbumError("启用站点前请填写它自己的密钥和模型")
                    providers.append(
                        {**site, "id": key, "enabled": enabled, **credentials}
                    )
                settings["providers"] = providers
            if "provider_order" in patch:
                order = strings(patch["provider_order"], 5)
                known = {"primary", *(p["id"] for p in settings["providers"])}
                if any(key not in known for key in order):
                    raise AlbumError("站点顺序包含不存在的站点")
                settings["provider_order"] = order
            if "preferred_provider" in patch:
                settings["preferred_provider"] = text_value(
                    patch["preferred_provider"], 80
                )
            eligible = {
                *(["primary"] if settings["primary_enabled"] else []),
                *(p["id"] for p in settings["providers"] if p["enabled"]),
            }
            if settings["preferred_provider"] not in eligible:
                settings["preferred_provider"] = "primary"
            settings["settings_revision"] += 1
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            os.replace(tmp, self.settings_path)
            self.settings = settings
        return self.public_settings()

    def public_settings(self) -> dict:
        return {
            **{
                k: v
                for k, v in self.settings.items()
                if k not in ("api_key", "api_keys", "providers")
            },
            "providers": [
                {
                    **{k: v for k, v in p.items() if k not in ("api_key", "api_keys")},
                    "api_keys": [
                        {"id": k["id"], "name": k["name"]}
                        for k in credential_entries(p)
                    ],
                    "active_key_id": p.get("active_key_id")
                    or ("legacy" if p.get("api_key") else ""),
                    "api_key_set": bool(p["api_key"]),
                }
                for p in self.settings["providers"]
            ],
            "api_key_set": bool(self.settings["api_key"]),
            "api_keys": [
                {"id": k["id"], "name": k["name"]}
                for k in credential_entries(self.settings)
            ],
            "active_key_id": self.settings.get("active_key_id")
            or ("legacy" if self.settings.get("api_key") else ""),
            "primary_name": self.settings["primary_name"]
            or urlsplit(self.settings["base_url"]).hostname
            or "未命名站点",
        }

    def provider_configs(self) -> list[dict]:
        """Return independent private credentials for enabled image services."""
        primary = {
            k: self.settings[k]
            for k in ("base_url", "api_key", "model", "size", "reference_field")
        }
        sites = [
            *(
                [
                    {
                        **primary,
                        "id": "primary",
                        "name": self.public_settings()["primary_name"],
                    }
                ]
                if self.settings["primary_enabled"]
                else []
            ),
            *[p for p in self.settings["providers"] if p["enabled"]],
        ]
        sites = [
            {
                **{
                    k: p[k]
                    for k in (
                        "id",
                        "name",
                        "base_url",
                        "api_key",
                        "model",
                        "size",
                        "reference_field",
                    )
                },
                "key_id": p.get("active_key_id")
                or (self.settings.get("active_key_id") if p["id"] == "primary" else "")
                or "legacy",
            }
            for p in sites
            if all(p.get(k) for k in ("base_url", "api_key", "model"))
        ]
        preferred = self.settings["preferred_provider"]
        order = self.settings["provider_order"]
        return sorted(
            sites,
            key=lambda p: (
                p["id"] != preferred,
                order.index(p["id"]) if p["id"] in order else len(order),
            ),
        )

    def reserve_job(
        self, scope: str, fingerprint: str, payload: dict
    ) -> tuple[dict, bool]:
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT id FROM jobs WHERE scope=? AND fingerprint=?",
                (scope, fingerprint),
            ).fetchone()
            if old:
                return self.job(old["id"]), False
            key = uid("job_")
            payload = {**payload, "created": now(), "scope": scope}
            self.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                (
                    key,
                    scope,
                    fingerprint,
                    "queued",
                    json.dumps(payload, ensure_ascii=False),
                    now(),
                ),
            )
        return self.job(key), True

    def job(self, key: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if not row:
            raise AlbumError("任务不存在")
        return {
            **json.loads(row["data"]),
            "id": row["id"],
            "state": row["state"],
            "updated": row["updated"],
        }

    def jobs(self) -> list[dict]:
        with self.lock:
            keys = [
                x[0]
                for x in self.db.execute(
                    "SELECT id FROM jobs WHERE json_extract(data,'$.archived') IS NOT 1 ORDER BY rowid DESC"
                )
            ]
        return [self.job(k) for k in keys]

    def update_job(self, key: str, state: str, **patch) -> dict:
        with self.lock, self.db:
            job = self.job(key)
            data = {
                k: v
                for k, v in {**job, **patch}.items()
                if k not in ("id", "state", "updated")
            }
            self.db.execute(
                "UPDATE jobs SET state=?,data=?,updated=? WHERE id=?",
                (state, json.dumps(data, ensure_ascii=False), now(), key),
            )
        return self.job(key)

    def reserve_delivery(self, key: str, payload: dict) -> bool:
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?,?,?)",
                (
                    key,
                    "sending",
                    json.dumps({**payload, "created": now()}, ensure_ascii=False),
                ),
            )
        return cursor.rowcount == 1

    def finish_delivery(self, key: str, state: str, **patch):
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT data FROM deliveries WHERE id=?", (key,)
            ).fetchone()
            if old:
                self.db.execute(
                    "UPDATE deliveries SET state=?,data=? WHERE id=?",
                    (
                        state,
                        json.dumps({**json.loads(old[0]), **patch}, ensure_ascii=False),
                        key,
                    ),
                )

    def deliveries(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM deliveries WHERE json_extract(data,'$.archived') IS NOT 1 ORDER BY rowid DESC LIMIT 200"
            ).fetchall()
        return [
            {**json.loads(x["data"]), "id": x["id"], "state": x["state"]} for x in rows
        ]

    def delivery(self, key: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM deliveries WHERE id=?", (key,)
            ).fetchone()
        if not row:
            raise AlbumError("发送记录不存在")
        return {**json.loads(row["data"]), "id": key, "state": row["state"]}

    def recover(self):
        """Mark interrupted writes unknown; never replay a POST on restart."""
        with self.lock, self.db:
            for job in self.jobs():
                if job.get("providers") and (
                    job["state"] == "running"
                    or job["state"] == "queued"
                    and job.get("attempts")
                ):
                    self.update_job(
                        job["id"],
                        "unknown",
                        error="运行曾中断；有上游编号时可继续查询，不自动重新生成",
                    )
            self.db.execute("UPDATE jobs SET state='unknown' WHERE state='submitting'")
            self.db.execute(
                "UPDATE jobs SET state='download_failed' WHERE state='downloading'"
            )
            self.db.execute(
                "UPDATE deliveries SET state='unknown' WHERE state='sending'"
            )
